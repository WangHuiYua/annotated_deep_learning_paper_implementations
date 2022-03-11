"""
---
title: RETRO model
summary: >
  RETRO model with encoder for neighbors and autoregressive decoder
---

# RETRO model

This is the model definition for
 [RETRO](index.html).
"""

import math
from typing import Set

import torch
from torch import nn

from labml.logger import inspect


class RotaryPositionalEmbeddings(nn.Module):
    """
    ## [RoPE embeddings](../rope/index.html)

    *We use rotary position embeddings in self attention layers.
    We assume the positional information gets embedded in embeddings
    and therefore not use them in causal attention.
    [Non-causal self-attention needs explicit positional information
     because it cannot infer it](https://papers.labml.ai/paper/3999902edc8511eba3db37f65e372566).*
    """

    def __init__(self, d: int, base: int = 10_000):
        """
        * `d` is the number of features $d$
        * `base` is the constant used for calculating $\Theta$
        """
        super().__init__()
        # $\Theta = {\theta_i = 10000^{\frac{2(i-1)}{d}}, i \in [1, 2, ..., \frac{d}{2}]}$
        self.theta = nn.Parameter(1. / (base ** (torch.arange(0, d, 2).float() / d)), requires_grad=False)

    def forward(self, x: torch.Tensor):
        """
        * `x` is the Tensor at the head of a key or a query with shape `[ batch_size, seq_len, n_heads, d]`
        """
        # Extract the shape
        batch_size, seq_len, n_heads, d = x.shape

        # $\frac{d}{2}$
        d_2 = d // 2

        # Create position indexes `[0, 1, ..., seq_len - 1]`
        seq_idx = torch.arange(seq_len, device=x.device).type_as(self.theta)

        # Calculate the product of position index and $\theta_i$
        idx_theta = torch.einsum('n,d->nd', seq_idx, self.theta)

        # Concatenate so that for row $m$ we have
        # $[m \theta_0, m \theta_1, ..., m \theta_{\frac{d}{2}}, m \theta 0, m \theta 1, ..., m \theta_{\frac{d}{2}}]$
        idx_theta2 = torch.cat([idx_theta, idx_theta], dim=1)

        # Calculate
        # $[-x^{(\frac{d}{2} + 1)}, -x^{(\frac{d}{2} + 2)}, ..., -x^{(d)}, x^{(1)}, x^{(2)}, ..., -x^{(\frac{d}{2})}]$
        neg_half_x = torch.cat([-x[:, :, :, d_2:], x[:, :, :, :d_2]], dim=-1)

        # Calculate
        #
        # \begin{align}
        # \begin{pmatrix}
        # x^{(i)}_m \cos m \theta_i - x^{(i + \frac{d}{2})}_m \sin m \theta_i \\
        # x^{(i + \frac{d}{2})}_m \cos m\theta_i + x^{(i)}_m \sin m \theta_i \\
        # \end{pmatrix} \\
        # \end{align}
        #
        # for $i \in {1, 2, ..., \frac{d}{2}}$
        rx = (x * idx_theta2.cos()[None, :, None, :]) + (neg_half_x * idx_theta2.sin()[None, :, None, :])

        #
        return rx


class SelfAttention(nn.Module):
    """
    ## Self-Attention Layer

    This applies causal and non-causal [multi-headed self attention](../mha.html).
    """

    def __init__(self, d_model: int, n_heads: int, d_k: int, is_causal: bool):
        """
        * `d_model` is the number of features in transformer embeddings
        * `n_heads` is the number of attention heads
        * `d_k` is the number of features per head
        * `is_causal` indicates whether this is causal attention (masked)
        """
        super().__init__()

        self.is_causal = is_causal
        self.n_heads = n_heads
        self.d_k = d_k

        # To scale attentions before softmax by $\frac{1}{\sqrt{d_k}}$
        self.scale = 1 / math.sqrt(self.d_k)

        # Linear layers for query, key and value heads.
        self.query = nn.Linear(d_model, n_heads * d_k)
        self.key = nn.Linear(d_model, n_heads * d_k)
        self.value = nn.Linear(d_model, n_heads * d_k)

        # Pre-norm layer. The paper uses RMSNorm instead.
        self.norm = nn.LayerNorm(d_model)

        # Softmax for attention probabilities
        self.softmax = nn.Softmax(dim=-1)

        # Rotary positional embeddings
        self.rotary_pe = RotaryPositionalEmbeddings(self.d_k)

        # Final linear layer
        self.output = nn.Linear(n_heads * d_k, d_model)

    def mask_attention(self, attn: torch.Tensor):
        """
        ### Mask the attention layer for causal attention

        * `attn` is the attention matrix of shape `[batch_size, n_heads, seq_len, seq_len]`
        """

        # No masking for non-causal attention
        if not self.is_causal:
            return attn

        # Create a triangular mask
        mask = torch.tril(attn.new_ones(attn.shape[-2:]))
        # Filter by the mask
        return attn.masked_fill(mask == 0, float('-inf'))

    def forward(self, h: torch.Tensor):
        """
        * `h` is the transformer embeddings of shape `[batch_size, seq_len, d_model]`
        """

        # Residual connection
        h_res = h

        # Pre-normalization
        h = self.norm(h)

        # Get query, key, and values and split them in to heads.
        # These will have shapes `[batch_size, seq_len, n_heads, d_k]`
        mh_shape = (*h.shape[:-1], self.n_heads, self.d_k)
        q = self.query(h).view(mh_shape)
        k = self.key(h).view(mh_shape)
        v = self.value(h).view(mh_shape)

        # Apply rotary positional embeddings
        q = self.rotary_pe(q)
        k = self.rotary_pe(k)

        # Calculate attentions
        attn = torch.einsum('bihd,bjhd->bhij', q, k)
        # Scale it by $\frac{1}{\sqrt{d_k}}$
        attn = attn * self.scale

        # Apply masks if it's causal attention
        attn = self.mask_attention(attn)

        # Calculate attention probabilities
        attn = self.softmax(attn)

        # Get values
        h = torch.einsum("bhij,bjhd->bihd", attn, v)

        # Change from shape `[batch_size, seq_len, n_heads, d_k]`
        # to `[batch_size, seq_len, n_heads * d_k]`
        h = h.reshape(*h.shape[:-2], -1)

        # Apply final linear layer.
        # The result will have shape `[batch_size, seq_len, d_model]`
        h = self.output(h)

        # Add the residual connection
        return h + h_res


class CrossAttention(nn.Module):
    """
    ## Cross-Attention Layer

    This is similar to self-attention layer defined above, except that
    it gets keys and values from different set of embeddings than the queries.

    This is used in the encoder to encode the retrieved chunks based on the
    input chunks.

    *We do not use any explicit positional embeddings here.
    We assume that the model can represent positional information in the embeddings implicitly.*
    """
    def __init__(self, d_model: int, n_heads: int, d_k: int):
        """
        * `d_model` is the number of features in transformer embeddings
        * `n_heads` is the number of attention heads
        * `d_k` is the number of features per head
        """
        super().__init__()

        self.n_heads = n_heads
        self.d_k = d_k

        # To scale attentions before softmax by $\frac{1}{\sqrt{d_k}}$
        self.scale = 1 / math.sqrt(self.d_k)

        # Linear layers for query, key and value heads.
        self.query = nn.Linear(d_model, n_heads * d_k)
        self.key = nn.Linear(d_model, n_heads * d_k)
        self.value = nn.Linear(d_model, n_heads * d_k)

        # Pre-norm layer for the query embeddings. The paper uses RMSNorm instead.
        self.norm = nn.LayerNorm(d_model)

        # Softmax for attention probabilities
        self.softmax = nn.Softmax(dim=-1)

        # Final linear layer
        self.output = nn.Linear(n_heads * d_k, d_model)

    def forward(self, e: torch.Tensor, h: torch.Tensor):
        """
        * `e` are the retrieved nearest neighbor chunk embeddings with shape
          `[batch_size, chunks, neighbors, neighbor_len, d_model]`
        * `h` are the input chunks from which the nearest neighbors were retrieved with shape
          `[batch_size, chunks, chunk_len, d_model]`. This is already normalized.
        """

        # Residual connection
        e_res = e

        # Normalize retrieved chunks
        e = self.norm(e)

        # Get query from the retrieved chunks
        q = self.query(e).view(*e.shape[:-1], self.n_heads, self.d_k)
        # Get keys and values from the input chunks
        k = self.key(h).view(*h.shape[:-1], self.n_heads, self.d_k)
        v = self.value(h).view(*h.shape[:-1], self.n_heads, self.d_k)

        # Calculate attention scores for all chunks.
        # Each retrieved neighbor will pay attention to the original chunk that retrieved it.
        # This will have shape `[batch_size, chunks, neighbors, n_heads, neighbor_len, chunk_len]`
        attn = torch.einsum('bcnihd,bcjhd->bcnhij', q, k)
        # Scale attention scores
        attn = attn * self.scale

        # Calculate softmax across the last dimension
        attn = self.softmax(attn)

        # Gather values
        e = torch.einsum("bcnhij,bcjhd->bcnihd", attn, v)

        # Change from shape `[batch_size, chunks, neighbors, neighbor_len, n_heads, d_k]`
        # to `[batch_size, chunks, neighbors, neighbor_len, n_heads * d_k]`
        e = e.reshape(*e.shape[:-2], -1)

        # Apply final linear layer.
        # The result will have shape `[batch_size, chunks, neighbors, neighbor_len, d_model]`
        e = self.output(e)

        # Add residual connection
        return e + e_res


class ChunkedCrossAttention(nn.Module):
    """
    ## Chunked Cross-Attention Layer

    This is similar to cross-attention layer defined above.

    This is used in the decoder to pay attention to the retrieved neighbor chunks.

    *We do not use any explicit positional embeddings here.
    We assume that the model can represent positional information in the embeddings implicitly.*
    """
    def __init__(self, d_model: int, n_heads: int, d_k: int, chunk_len: int):
        """
        * `d_model` is the number of features in transformer embeddings
        * `n_heads` is the number of attention heads
        * `d_k` is the number of features per head
        * `chunk_len` is the length of a chunk
        """

        super().__init__()

        self.chunk_len = chunk_len
        self.n_heads = n_heads
        self.d_k = d_k

        # To scale attentions before softmax by $\frac{1}{\sqrt{d_k}}$
        self.scale = 1 / math.sqrt(self.d_k)

        # Linear layers for query, key and value heads.
        self.query = nn.Linear(d_model, n_heads * d_k)
        self.key = nn.Linear(d_model, n_heads * d_k)
        self.value = nn.Linear(d_model, n_heads * d_k)

        # Pre-norm layer for the query embeddings. The paper uses RMSNorm instead.
        self.norm = nn.LayerNorm(d_model)

        # Softmax for attention probabilities
        self.softmax = nn.Softmax(dim=-1)

        # Final linear layer
        self.output = nn.Linear(n_heads * d_k, d_model)

    def forward(self, h: torch.Tensor, e: torch.Tensor):
        """
        `h` are the input embeddings of shape `[batch_size, seq_len, d_model]`
        `e` are the retrieved nearest neighbors of shape `[batch_size, chunks, neighbors, neighbor_len, d_model]`
        """

        # Get shape
        batch_size, chunks, neighbors, neighbor_len, d_model = e.shape

        # No attention if there are no chunks (for short inputs when sampling)
        if chunks == 0:
            return h

        # Residual connection
        h_res = h

        # Remove the first `chunk_len - 1` embeddings.
        # The input pays attention neighbors retrieved and encoded using the past tokens only;
        # so that there is no information leakage.
        # That is the retrieved neighbors from the first chunks will have information from the first chunk.
        # So by shifting the sequence to the left by `chunk_len - 1` we make sure that information only flows
        # to the right.
        h = h[:, self.chunk_len - 1:]
        # Pre-norm
        h = self.norm(h)
        # Append empty embeddings to the end to be able to split the input into chunks
        if h.shape[1] < chunks * self.chunk_len:
            h = torch.cat((h, h.new_zeros(batch_size, chunks * self.chunk_len - h.shape[1], d_model)), dim=1)
        # Reshape the input into chunks.
        h = h.reshape(batch_size, chunks, self.chunk_len, d_model)

        # Get query from the input
        q = self.query(h).view(*h.shape[:-1], self.n_heads, self.d_k)
        # Get keys and values from the retrieved neighbors
        k = self.key(e).view(*e.shape[:-1], self.n_heads, self.d_k)
        v = self.value(e).view(*e.shape[:-1], self.n_heads, self.d_k)

        # Calculate attention scores for input chunks.
        # Each chunk will pay attention to neighbors retrieved by previous chunk.
        # This will have shape `[batch_size, chunks, heads, chunk_len, neighbors, neighbor_len]`
        attn = torch.einsum('bcihd,bcnjhd->bchinj', q, k)
        # Scale attention scores
        attn = attn * self.scale

        # Apply softmax over the last two dimensions `neighbors, neighbor_len`
        attn = self.softmax(attn.view(*attn.shape[:-2], -1)).view(attn.shape)

        # Gather values
        h = torch.einsum("bchinj,bcnjhd->bcihd", attn, v)

        # Change from shape `[batch_size, chunks, chunk_len, n_heads, d_k]`
        # to `[batch_size, chunks * chunk_len, n_heads * d_k]`
        h = h.reshape(batch_size, chunks * self.chunk_len, -1)

        # Apply final linear layer.
        # The result will have shape `[batch_size, chunks * chunk_len, d_model]`
        h = self.output(h)

        # Append `chunk_len - 1` zero embedding to the left; i.e. right shift it back
        h = torch.cat((h.new_zeros(batch_size, self.chunk_len - 1, d_model), h), dim=1)

        # Truncate and add the residual connection
        return h[:, :h_res.shape[1]] + h_res


class FeedForward(nn.Module):
    def __init__(self, d_model: int, d_ff: int):
        super().__init__()

        self.lin1 = nn.Linear(d_model, d_ff)
        self.lin2 = nn.Linear(d_ff, d_model)

        self.norm = nn.LayerNorm(d_model)
        self.act = nn.ReLU()

    def forward(self, x: torch.Tensor):
        x_res = x
        x = self.norm(x)
        x = self.lin1(x)
        x = self.act(x)
        x = self.lin2(x)

        return x + x_res


class Encoder(nn.Module):
    def __init__(self, chunk_len: int, n_layers: int, ca_layers: Set[int],
                 d_model: int, n_heads: int, d_k: int, d_ff: int):
        super().__init__()
        self.ca_layers = ca_layers
        self.chunk_len = chunk_len
        self.ca = nn.ModuleList([CrossAttention(d_model, n_heads, d_k) for _ in range(len(ca_layers))])
        self.attn = nn.ModuleList([SelfAttention(d_model, n_heads, d_k, is_causal=False) for _ in range(n_layers)])
        self.ffw = nn.ModuleList([FeedForward(d_model, d_ff) for _ in range(n_layers)])

        self.norm_h = nn.LayerNorm(d_model)

    def forward(self, e: torch.Tensor, h: torch.Tensor):
        """
        e [batch_size, chunks, neighbors, seq, d_model]
        h [batch_size, seq, d_model]
        """

        batch_size, chunks, neighbors, neighbor_len, d_model = e.shape
        h_split = h[:, :self.chunk_len * chunks, :].reshape(batch_size, chunks, self.chunk_len, d_model)
        h_split = self.norm_h(h_split)

        p_ca = 0
        for p in range(len(self.attn)):
            e = self.attn[p](e.view(-1, neighbor_len, d_model)).view(e.shape)

            if p in self.ca_layers:
                e = self.ca[p_ca](e, h_split)
                p_ca += 1

            e = self.ffw[p](e)

        return e


class RetroModel(nn.Module):
    def __init__(self, n_vocab: int, d_model: int, n_layers: int, ca_layers: Set[int], chunk_len: int,
                 n_heads: int, d_k: int, d_ff: int,
                 encoder: Encoder):
        super().__init__()

        self.ca_layers = ca_layers
        self.emb = nn.Embedding(n_vocab, d_model)
        self.linear = nn.Linear(d_model, d_model)
        self.encoder = encoder
        self.cca = nn.ModuleList(
            [ChunkedCrossAttention(d_model, n_heads, d_k, chunk_len) for _ in range(len(ca_layers))])
        self.attn = nn.ModuleList([SelfAttention(d_model, n_heads, d_k, is_causal=True) for _ in range(n_layers)])
        self.ffw = nn.ModuleList([FeedForward(d_model, d_ff) for _ in range(n_layers)])
        self.read = nn.Linear(d_model, n_vocab)

        self.norm_e = nn.LayerNorm(d_model)

    def forward(self, x: torch.Tensor, ret: torch.Tensor):
        """
        x [batch_size, seq]
        e [batch_size, chunks, neighbors, seq]
        """

        h = self.emb(x)

        ret_emb = self.emb(ret)

        p_ca = 0
        for p in range(len(self.attn)):
            h = self.attn[p](h)

            if self.ca_layers and p == min(self.ca_layers):
                e = self.encoder(ret_emb, h)
                e = self.norm_e(e)

            if p in self.ca_layers:
                h = self.cca[p_ca](h, e)
                p_ca += 1

            h = self.ffw[p](h)

        return self.read(h)


def _test():
    chunk_len = 4
    d_model = 8
    d_ff = 32
    n_heads = 2
    d_k = 4

    device = torch.device('cuda:0')

    m = RetroModel(5, d_model, 6, {2, 5}, chunk_len, n_heads, d_k, d_ff,
                   encoder=Encoder(chunk_len, 2, {1}, d_model, n_heads, d_k, d_ff))

    m.to(device)
    x = [1, 2, 4, 4, 0, 1, 2, 3, 4, 3]
    ret = [
        [[0, 0, 0, 0, 0, 0], [1, 1, 1, 1, 1, 1]],
        [[0, 0, 0, 0, 0, 0], [1, 1, 1, 1, 1, 1]],
    ]
    res = m(torch.tensor([x] * 10).to(device), torch.tensor([ret] * 10).to(device))

    inspect(res)


if __name__ == '__main__':
    _test()
