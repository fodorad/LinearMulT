import math

import torch
from torch import nn
import torch.nn.functional as F

from fast_transformers.builders.attention_builders import AttentionBuilder
from fast_transformers.attention import AttentionLayer, LinearAttention
from fast_transformers.masking import LengthMask, FullMask

from linear_mult.models.position_embedding import SinusoidalPositionalEmbedding
from linear_mult.models.multihead_attention import MultiheadAttention


class TransformerEncoder(nn.Module):
    """
    Transformer encoder consisting of *args.encoder_layers* layers. Each layer
    is a :class:`TransformerEncoderLayer`.
    Args:
        embed_tokens (torch.nn.Embedding): input embedding
        num_heads (int): number of heads
        layers (int): number of layers
        attn_dropout (float): dropout applied on the attention weights
        relu_dropout (float): dropout applied on the first layer of the residual block
        res_dropout (float): dropout applied on the residual block
        attn_mask (bool): whether to apply mask on the attention weights
    """

    def __init__(self, embed_dim, num_heads, layers, attn_dropout=0.0, relu_dropout=0.0, res_dropout=0.0,
                 embed_dropout=0.0, attn_mask=False, attention_type='linear'):
        super().__init__()
        self.dropout = embed_dropout      # Embedding dropout
        self.attn_dropout = attn_dropout
        self.embed_dim = embed_dim
        self.embed_scale = math.sqrt(embed_dim)
        self.embed_positions = SinusoidalPositionalEmbedding(embed_dim)
        self.attention_type = attention_type
        self.attn_mask = attn_mask

        self.layers = nn.ModuleList([])
        for layer in range(layers):
            new_layer = TransformerEncoderLayer(embed_dim,
                                                num_heads=num_heads,
                                                attn_dropout=attn_dropout,
                                                relu_dropout=relu_dropout,
                                                res_dropout=res_dropout,
                                                attn_mask=attn_mask,
                                                attention_type=attention_type)
            self.layers.append(new_layer)

        self.register_buffer('version', torch.Tensor([2]))
        self.normalize = True
        if self.normalize:
            self.layer_norm = nn.LayerNorm(embed_dim)


    def forward(self, x_in, x_in_k = None, x_in_v = None):
        """
        Args:
            x_in (FloatTensor): embedded input of shape `(src_len, batch, embed_dim)`
            x_in_k (FloatTensor): embedded input of shape `(src_len, batch, embed_dim)`
            x_in_v (FloatTensor): embedded input of shape `(src_len, batch, embed_dim)`
        Returns:
            dict:
                - **encoder_out** (Tensor): the last encoder layer's output of
                  shape `(src_len, batch, embed_dim)`
                - **encoder_padding_mask** (ByteTensor): the positions of
                  padding elements of shape `(batch, src_len)`
        """
        # embed tokens and positions
        x = self.embed_scale * x_in
        if self.embed_positions is not None:
            x += self.embed_positions(x_in[:, :, 0])
        x = F.dropout(x, p=self.dropout, training=self.training)

        if x_in_k is not None and x_in_v is not None:
            # embed tokens and positions    
            x_k = self.embed_scale * x_in_k
            x_v = self.embed_scale * x_in_v
            if self.embed_positions is not None:
                x_k += self.embed_positions(x_in_k.transpose(0, 1)[:, :, 0]).transpose(0, 1)   # Add positional embedding
                x_v += self.embed_positions(x_in_v.transpose(0, 1)[:, :, 0]).transpose(0, 1)   # Add positional embedding
            x_k = F.dropout(x_k, p=self.dropout, training=self.training)
            x_v = F.dropout(x_v, p=self.dropout, training=self.training)
        
        # encoder layers
        intermediates = [x]
        for layer in self.layers:
            if x_in_k is not None and x_in_v is not None:
                x = layer(x, x_k, x_v)
            else:
                x = layer(x)
            intermediates.append(x)

        if self.normalize:
            x = self.layer_norm(x)

        return x

    def max_positions(self):
        """Maximum input length supported by the encoder."""
        if self.embed_positions is None:
            return self.max_source_positions
        return min(self.max_source_positions, self.embed_positions.max_positions())


class TransformerEncoderLayer(nn.Module):
    """Encoder layer block.
    In the original paper each operation (multi-head attention or FFN) is
    postprocessed with: `dropout -> add residual -> layernorm`. In the
    tensor2tensor code they suggest that learning is more robust when
    preprocessing each layer with layernorm and postprocessing with:
    `dropout -> add residual`. We default to the approach in the paper, but the
    tensor2tensor approach can be enabled by setting
    *args.encoder_normalize_before* to ``True``.
    Args:
        embed_dim: Embedding dimension
    """

    def __init__(self, embed_dim, num_heads=4, attn_dropout=0.1, relu_dropout=0.1, res_dropout=0.1,
                 attn_mask=None, attention_type='linear'):
        super().__init__()
        self.embed_dim = embed_dim
        self.num_heads = num_heads
        self.attention_type = attention_type

        if attention_type == 'linear':
            builder = AttentionBuilder.from_kwargs(
                attention_dropout=attn_dropout,
                query_dimensions=embed_dim//num_heads
            )
            self.self_attn = AttentionLayer(LinearAttention(query_dimensions=embed_dim//num_heads), 
                                                            d_model=embed_dim, 
                                                            n_heads=num_heads, 
                                                            d_keys=embed_dim//num_heads, 
                                                            d_values=embed_dim//num_heads)
        else: # quadratic (softmax) attention
            self.self_attn = MultiheadAttention(
                embed_dim=self.embed_dim,
                num_heads=self.num_heads,
                attn_dropout=attn_dropout
            )
        self.attn_mask = attn_mask

        self.relu_dropout = relu_dropout
        self.res_dropout = res_dropout
        self.normalize_before = True

        self.fc1 = Linear(self.embed_dim, 4*self.embed_dim)   # The "Add & Norm" part in the paper
        self.fc2 = Linear(4*self.embed_dim, self.embed_dim)
        self.layer_norms = nn.ModuleList([nn.LayerNorm(self.embed_dim) for _ in range(2)])

    def forward(self, x, x_k=None, x_v=None):
        """
        Args:
            x (Tensor): input to the layer of shape `(seq_len, batch, embed_dim)`
            encoder_padding_mask (ByteTensor): binary ByteTensor of shape
                `(batch, src_len)` where padding elements are indicated by ``1``.
            x_k (Tensor): same as x
            x_v (Tensor): same as x
        Returns:
            encoded output of shape `(batch, src_len, embed_dim)`
        """
        residual = x
        x = self.maybe_layer_norm(0, x, before=True)
        N = x.shape[0]
        L = x.shape[1]
        
        if self.attention_type == 'linear':
            #attn_mask = TriangularCausalMask(L, device=x.device)
            attn_mask = FullMask(L, device=x.device)
            if x_k is None and x_v is None:
                length_mask = LengthMask(x.new_full((N,), L, dtype=torch.int64))
                x = self.self_attn(queries=x, keys=x, values=x, query_lengths=length_mask , key_lengths=length_mask, attn_mask=attn_mask)
            else:
                S = x_k.shape[1]
                #CUDA error: an illegal memory access was encountered
                length_mask = LengthMask(x.new_full((N,), S, dtype=torch.int64))
                x_k = self.maybe_layer_norm(0, x_k, before=True)
                x_v = self.maybe_layer_norm(0, x_v, before=True) 
                x = self.self_attn(queries=x, keys=x_k, values=x_v, query_lengths=length_mask , key_lengths=length_mask,  attn_mask=attn_mask)
        else:
            mask = buffered_future_mask(x, x_k) if self.attn_mask else None
            if x_k is None and x_v is None:
                x, _ = self.self_attn(query=x, key=x, value=x, attn_mask=mask)
            else:
                x_k = self.maybe_layer_norm(0, x_k, before=True)
                x_v = self.maybe_layer_norm(0, x_v, before=True) 
                x, _ = self.self_attn(query=x, key=x_k, value=x_v, attn_mask=mask)

        x = F.dropout(x, p=self.res_dropout, training=self.training)
        x = residual + x
        x = self.maybe_layer_norm(0, x, after=True)

        residual = x
        x = self.maybe_layer_norm(1, x, before=True)
        x = F.relu(self.fc1(x))
        x = F.dropout(x, p=self.relu_dropout, training=self.training)
        x = self.fc2(x)
        x = F.dropout(x, p=self.res_dropout, training=self.training)
        x = residual + x
        x = self.maybe_layer_norm(1, x, after=True)
        return x

    def maybe_layer_norm(self, i, x, before=False, after=False):
        assert before ^ after
        if after ^ self.normalize_before:
            return self.layer_norms[i](x)
        else:
            return x


def fill_with_neg_inf(t):
    """FP16-compatible function that fills a tensor with -inf."""
    return t.float().fill_(float('-inf')).type_as(t)


def buffered_future_mask(tensor, tensor2=None):
    dim1 = dim2 = tensor.size(1) # (B, T, C)
    if tensor2 is not None:
        dim2 = tensor2.size(1)
    future_mask = torch.triu(fill_with_neg_inf(torch.ones(dim1, dim2)), 1+abs(dim2-dim1))
    if tensor.is_cuda:
        future_mask = future_mask.cuda()
    return future_mask[:dim1, :dim2]


def Linear(in_features, out_features, bias=True):
    m = nn.Linear(in_features, out_features, bias)
    nn.init.xavier_uniform_(m.weight)
    if bias:
        nn.init.constant_(m.bias, 0.)
    return m

