import torch
import torch.nn as nn
import torch.nn.functional as F

import math



class FastSelfAttention(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config
        
        # only considering single head for now
        self.Wquery = nn.Linear(config.hidden_size, config.hidden_size, bias = False)
        self.query_att = nn.Linear(config.hidden_size, 1, bias = False)
        self.Wkeys = nn.Linear(config.hidden_size, config.hidden_size, bias = False)
        self.key_att = nn.Linear(config.hidden_size, 1, bias = False)
        #self.Wvalues = nn.Linear(config.hidden_size, config.hidden_size)
        
        self.attn_norm = nn.LayerNorm(config.hidden_size)
        self.attn_dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, hidden_states, attention_mask):
        hidden_states = self.attn_norm(hidden_states)
        
        batch_size, seq_len, d_model = hidden_states.shape
        query = self.Wquery(hidden_states)
        keys = self.Wkeys(hidden_states)
        # values = self.Wvalues(hidden_states)
        
        # parameter saving described at the end of section 3.1
        values = query
        
        attention_mask = attention_mask.to(dtype=next(self.parameters()).dtype)  # fp16 compatibility
        attention_mask = (1.0 - attention_mask) * -10000.0
        attention_mask = attention_mask.unsqueeze(2)
        
        # equations (3-4) done causally masking out pad tokens
        query_weight = self.query_att(query) / d_model**.5
        query_weight += attention_mask
        query_weight = torch.exp(query_weight)
        pooled_query = torch.cumsum(query_weight * query, dim = 1) / torch.cumsum(query_weight, dim = 1)
        
        # corresponds to "p_i = q * k_i" in paper
        mixed_keys = pooled_query * keys
        
        # equations (5-6) done causally masking out pad tokens
        keys_weight = self.key_att(mixed_keys) / d_model**.5
        keys_weight += attention_mask
        keys_weight = torch.exp(keys_weight)
        pooled_keys = torch.cumsum(keys_weight * mixed_keys, dim = 1) / torch.cumsum(keys_weight, dim = 1)
        
        # corresponds to "u_i = k * v_i" in paper
        weighted_values = pooled_keys * values
        
        # dropout last like megatron
        weighted_values = self.attn_dropout(weighted_values)
      
        return weighted_values



class CausalConvolution(nn.Module):
    def __init__(self, hidden_size, kernel_size, groups, dropout = .1):
        super().__init__()
        self.kernel_size = kernel_size
        self.convolutional_layer = nn.Conv1d(hidden_size, hidden_size, groups = groups,
                                             kernel_size = kernel_size, padding = 0, bias = False)
        self.conv_norm = nn.LayerNorm(hidden_size)
        self.gelu = nn.GELU()
        self.conv_drop = nn.Dropout(dropout)
        
        
    def forward(self, hidden_states):
        # layer norms still makes sense like "A ConvNet for the 2020s"
        mod = self.conv_norm(hidden_states)
        
        # use a gelu since this layer goes after the feedfoward layer
        # place inside conv block to not mess with residual connection
        # also after norm like resnet
        mod = self.gelu(mod)
        
        # batch len, seq len, embedding -> batch len, embedding, seq len (conv1d input format)
        mod = mod.permute(0, 2, 1)
        
        # padding to ensure causality
        mod = F.pad(mod, pad=(self.kernel_size-1, 0), mode='constant', value=0)
        mod = self.convolutional_layer(mod)
        
        # unpermute
        mod = mod.permute(0, 2, 1)
        
        mod = self.conv_drop(mod)
        
        return mod



class FastformerLayer(nn.Module):
    def __init__(self, config):
        super(FastformerLayer, self).__init__()
        
        self.convolve = config.convolve
        if config.convolve is True:
            self.convolutional_layer = CausalConvolution(
                config.hidden_size, config.kernel_size,
                config.groups, dropout = config.hidden_dropout_prob)
        
        self.attention = FastSelfAttention(config)

        self.boom = nn.Linear(config.hidden_size, config.hidden_size*4)
        self.gelu = nn.GELU()
        self.unboom = nn.Linear(4*config.hidden_size, config.hidden_size)
        self.boom_norm = nn.LayerNorm(config.hidden_size)
        self.boom_drop = nn.Dropout(config.hidden_dropout_prob)
    
    def forward(self, hidden_states, attention_mask):
        mod = hidden_states
        
        if self.convolve is True:
            mod = mod + self.convolutional_layer(mod)

        mod = mod + self.attention(mod, attention_mask)
        
        mod = mod + self.__boom(mod)
        
        return mod

    def __boom(self, hidden_states):
        mod = self.boom_norm(hidden_states)
        mod = self.boom(mod)
        mod = self.gelu(mod)
        mod = self.unboom(mod)
        
        # possible parameter saving like SHA-RNN (seems to slow down training significantly)
        # mod = torch.stack(mod.chunk(4, dim = -1), dim = -1).sum(dim = -1)

        mod = self.boom_drop(mod)

        return mod



class FastformerDecoder(nn.Module):
    def __init__(self, config):
        super(FastformerDecoder, self).__init__()
        self.config = config
        self.decoders = nn.ModuleList([FastformerLayer(config) for _ in range(config.num_hidden_layers)])
        self.position_embeddings = nn.Embedding(config.max_position_embeddings, config.hidden_size)
        self.LayerNorm = nn.LayerNorm(config.hidden_size)
        
        self.dropout = nn.Dropout(config.hidden_dropout_prob) 

    def forward(self, 
                input_embs, 
                attention_mask, 
                pooler_index=0):

        batch_size, seq_length, _ = input_embs.shape
        position_ids = torch.arange(seq_length, dtype=torch.long, device=input_embs.device)
        position_ids = position_ids.unsqueeze(0).expand(batch_size, -1)
        position_embeddings = self.position_embeddings(position_ids)

        embeddings = input_embs + position_embeddings
        
        embeddings = self.LayerNorm(embeddings)

        embeddings = self.dropout(embeddings)
        
        layer_outputs = embeddings
        for i, layer_module in enumerate(self.decoders):
            layer_outputs = layer_module(layer_outputs, attention_mask)

        return layer_outputs
    


class FastformerForCausalLM(torch.nn.Module):
    def __init__(self,config):
        super().__init__()
        self.config = config
        self.word_embedding = nn.Embedding(config.vocab_size,config.hidden_size, padding_idx=0)
        self.proj_logits = nn.Linear(config.hidden_size, config.vocab_size)
        self.fastformer_model = FastformerDecoder(config)
        self.criterion = nn.CrossEntropyLoss(label_smoothing = .1)
        self.eval_criterion = nn.CrossEntropyLoss(label_smoothing = 0)
        
        # weight tying
        self.proj_logits.weight = self.word_embedding.weight
    
    def forward(self, input_ids, labels, attention_mask):
        embds=self.word_embedding(input_ids)
        layer_outputs = self.fastformer_model(embds, attention_mask)
        logits = self.proj_logits(layer_outputs)
        
        if self.training():
            loss = self.criterion(logits.view(-1, self.config.vocab_size), labels.view(-1))
        else:
            loss = self.eval_criterion(logits.view(-1, self.config.vocab_size), labels.view(-1))

        return loss, logits
