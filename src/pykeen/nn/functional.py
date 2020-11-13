# -*- coding: utf-8 -*-

"""Functional forms of interaction methods."""

from typing import Optional, Tuple, Union

import torch
from torch import nn

from ..utils import broadcast_cat, clamp_norm, is_cudnn_error, normalize_for_einsum, split_complex

__all__ = [
    "complex_interaction",
    "conve_interaction",
    "convkb_interaction",
    "distmult_interaction",
    "ermlp_interaction",
    "ermlpe_interaction",
    'hole_interaction',
    'rotate_interaction',
    'translational_interaction',
    'transr_interaction',
]


def _normalize_terms_for_einsum(
    h: torch.FloatTensor,
    r: torch.FloatTensor,
    t: torch.FloatTensor,
) -> Tuple[torch.FloatTensor, str, torch.FloatTensor, str, torch.FloatTensor, str]:
    batch_size = max(h.shape[0], r.shape[0], t.shape[0])
    h_term, h = normalize_for_einsum(x=h, batch_size=batch_size, symbol='h')
    r_term, r = normalize_for_einsum(x=r, batch_size=batch_size, symbol='r')
    t_term, t = normalize_for_einsum(x=t, batch_size=batch_size, symbol='t')
    return h, h_term, r, r_term, t, t_term


def _add_cuda_warning(func):
    def wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except RuntimeError as e:
            if not is_cudnn_error(e):
                raise e
            raise RuntimeError(
                '\nThis code crash might have been caused by a CUDA bug, see '
                'https://github.com/allenai/allennlp/issues/2888, '
                'which causes the code to crash during evaluation mode.\n'
                'To avoid this error, the batch size has to be reduced.',
            ) from e

    return wrapped


@_add_cuda_warning
def conve_interaction(
    h: torch.FloatTensor,
    r: torch.FloatTensor,
    t: torch.FloatTensor,
    t_bias: torch.FloatTensor,
    input_channels: int,
    embedding_height: int,
    embedding_width: int,
    num_in_features: int,
    bn0: Optional[nn.BatchNorm1d],
    bn1: Optional[nn.BatchNorm1d],
    bn2: Optional[nn.BatchNorm1d],
    inp_drop: nn.Dropout,
    feature_map_drop: nn.Dropout2d,
    hidden_drop: nn.Dropout,
    conv1: nn.Conv2d,
    activation: nn.Module,
    fc: nn.Linear,
) -> torch.FloatTensor:
    """
    Evaluate the ConvE interaction function.

    :param h: shape: (batch_size, num_heads, dim)
        The head representations.
    :param r: shape: (batch_size, num_relations, dim)
        The relation representations.
    :param t: shape: (batch_size, num_tails, dim)
        The tail representations.
    :param t_bias: shape: (batch_size, num_tails, dim)
        The tail entity bias.
    :param input_channels:
        The number of input channels.
    :param embedding_height:
        The height of the reshaped embedding.
    :param embedding_width:
        The width of the reshaped embedding.
    :param num_in_features:
        The number of output features of the final layer (calculated with kernel and embedding dimensions).
    :param bn0:
        The first batch normalization layer.
    :param bn1:
        The second batch normalization layer.
    :param bn2:
        The third batch normalization layer.
    :param inp_drop:
        The input dropout layer.
    :param feature_map_drop:
        The feature map dropout layer.
    :param hidden_drop:
        The hidden dropout layer.
    :param conv1:
        The convolution layer.
    :param activation:
        The activation function.
    :param fc:
        The final fully connected layer.

    :return: shape: (batch_size, num_heads, num_relations, num_tails)
        The scores.
    """
    # bind sizes
    # batch_size = max(x.shape[0] for x in (h, r, t))
    num_heads = h.shape[1]
    num_relations = r.shape[1]
    num_tails = t.shape[1]
    embedding_dim = h.shape[-1]

    # repeat if necessary, and concat head and relation, batch_size', num_input_channels, 2*height, width
    # with batch_size' = batch_size * num_heads * num_relations
    h = h.unsqueeze(dim=2)
    h = h.view(*h.shape[:-1], input_channels, embedding_height, embedding_width)
    r = r.unsqueeze(dim=1)
    r = r.view(*r.shape[:-1], input_channels, embedding_height, embedding_width)
    x = broadcast_cat(h, r, dim=2).view(-1, input_channels, 2 * embedding_height, embedding_width)

    # batch_size, num_input_channels, 2*height, width
    if bn0 is not None:
        x = bn0(x)

    # batch_size, num_input_channels, 2*height, width
    x = inp_drop(x)

    # (N,C_out,H_out,W_out)
    x = conv1(x)

    if bn1 is not None:
        x = bn1(x)

    x = activation(x)
    x = feature_map_drop(x)

    # batch_size', num_output_channels * (2 * height - kernel_height + 1) * (width - kernel_width + 1)
    x = x.view(-1, num_in_features)
    x = fc(x)
    x = hidden_drop(x)

    if bn2 is not None:
        x = bn2(x)
    x = activation(x)

    # reshape: (batch_size', embedding_dim)
    x = x.view(-1, num_heads, num_relations, 1, embedding_dim)

    # For efficient calculation, each of the convolved [h, r] rows has only to be multiplied with one t row
    # output_shape: (batch_size, num_heads, num_relations, num_tails)
    t = t.view(t.shape[0], 1, 1, num_tails, embedding_dim).transpose(-1, -2)
    x = (x @ t).squeeze(dim=-2)

    # add bias term
    x = x + t_bias.view(t.shape[0], 1, 1, num_tails)

    return x


def distmult_interaction(
    h: torch.FloatTensor,
    r: torch.FloatTensor,
    t: torch.FloatTensor,
) -> torch.FloatTensor:
    """
    Evaluate the DistMult interaction function.

    :param h: shape: (batch_size, num_heads, dim)
        The head representations.
    :param r: shape: (batch_size, num_relations, dim)
        The relation representations.
    :param t: shape: (batch_size, num_tails, dim)
        The tail representations.

    :return: shape: (batch_size, num_heads, num_relations, num_tails)
        The scores.
    """
    # TODO: check if einsum is still very slow.
    h, h_term, r, r_term, t, t_term = _normalize_terms_for_einsum(h, r, t)
    return torch.einsum(f'{h_term},{r_term},{t_term}->bhrt', h, r, t)


def complex_interaction(
    h: torch.FloatTensor,
    r: torch.FloatTensor,
    t: torch.FloatTensor,
) -> torch.FloatTensor:
    """
    Evaluate the ComplEx interaction function.

    :param h: shape: (batch_size, num_heads, 2*dim)
        The complex head representations.
    :param r: shape: (batch_size, num_relations, 2*dim)
        The complex relation representations.
    :param t: shape: (batch_size, num_tails, 2*dim)
        The complex tail representations.

    :return: shape: (batch_size, num_heads, num_relations, num_tails)
        The scores.
    """
    h, h_term, r, r_term, t, t_term = _normalize_terms_for_einsum(h, r, t)
    (h_re, h_im), (r_re, r_im), (t_re, t_im) = [split_complex(x=x) for x in (h, r, t)]
    # TODO: check if einsum is still very slow.
    return sum(
        torch.einsum(f'{h_term},{r_term},{t_term}->bhrt', hh, rr, tt)
        for hh, rr, tt in [
            (h_re, r_re, t_re),
            (h_re, r_im, t_im),
            (h_im, r_re, t_im),
            (h_im, r_im, t_re),
        ]
    )


def convkb_interaction(
    h: torch.FloatTensor,
    r: torch.FloatTensor,
    t: torch.FloatTensor,
    conv: nn.Conv2d,
    activation: nn.Module,
    hidden_dropout: nn.Dropout,
    linear: nn.Linear,
) -> torch.FloatTensor:
    r"""
    Evaluate the ConvKB interaction function.

    .. math::
        W_L drop(act(W_C \ast ([h; r; t]) + b_C)) + b_L

    :param h: shape: (batch_size, num_heads, dim)
        The head representations.
    :param r: shape: (batch_size, num_relations, dim)
        The relation representations.
    :param t: shape: (batch_size, num_tails, dim)
        The tail representations.
    :param conv:
        The 3x1 convolution.
    :param activation:
        The activation function.
    :param hidden_dropout:
        The dropout layer applied to the hidden activations.
    :param linear:
        The final linear layer.

    :return: shape: (batch_size, num_heads, num_relations, num_tails)
        The scores.
    """
    # bind sizes
    batch_size = max(x.shape[0] for x in (h, r, t))
    num_heads = h.shape[1]
    num_relations = r.shape[1]
    num_tails = t.shape[1]

    # decompose convolution for faster computation in 1-n case
    num_filters = conv.weight.shape[0]
    assert conv.weight.shape == (num_filters, 1, 1, 3)
    embedding_dim = h.shape[-1]

    # compute conv(stack(h, r, t))
    conv_head, conv_rel, conv_tail = conv.weight[:, 0, 0, :].t()
    conv_bias = conv.bias.view(1, 1, 1, 1, 1, num_filters)
    # h.shape: (b, nh, d), conv_head.shape: (o), out.shape: (b, nh, d, o)
    h = (h.view(h.shape[0], h.shape[1], 1, 1, embedding_dim, 1) * conv_head.view(1, 1, 1, 1, 1, num_filters))
    r = (r.view(r.shape[0], 1, r.shape[1], 1, embedding_dim, 1) * conv_rel.view(1, 1, 1, 1, 1, num_filters))
    t = (t.view(t.shape[0], 1, 1, t.shape[1], embedding_dim, 1) * conv_tail.view(1, 1, 1, 1, 1, num_filters))
    x = activation(conv_bias + h + r + t)

    # Apply dropout, cf. https://github.com/daiquocnguyen/ConvKB/blob/master/model.py#L54-L56
    x = hidden_dropout(x)

    # Linear layer for final scores
    return linear(
        x.view(-1, embedding_dim * num_filters),
    ).view(batch_size, num_heads, num_relations, num_tails)


def ermlp_interaction(
    h: torch.FloatTensor,
    r: torch.FloatTensor,
    t: torch.FloatTensor,
    hidden: nn.Linear,
    activation: nn.Module,
    final: nn.Linear,
) -> torch.FloatTensor:
    r"""
    Evaluate the ER-MLP interaction function.

    :param h: shape: (batch_size, num_heads, dim)
        The head representations.
    :param r: shape: (batch_size, num_relations, dim)
        The relation representations.
    :param t: shape: (batch_size, num_tails, dim)
        The tail representations.
    :param hidden:
        The first linear layer.
    :param activation:
        The activation function of the hidden layer.
    :param final:
        The second linear layer.

    :return: shape: (batch_size, num_heads, num_relations, num_tails)
        The scores.
    """
    num_heads, num_relations, num_tails = [x.shape[1] for x in (h, r, t)]
    hidden_dim, embedding_dim = hidden.weight.shape
    assert embedding_dim % 3 == 0
    embedding_dim = embedding_dim // 3
    # split, shape: (embedding_dim, hidden_dim)
    head_to_hidden, rel_to_hidden, tail_to_hidden = hidden.weight.t().split(embedding_dim)
    bias = hidden.bias.view(1, 1, 1, 1, -1)
    h = h.view(-1, num_heads, 1, 1, embedding_dim) @ head_to_hidden.view(1, 1, 1, embedding_dim, hidden_dim)
    r = r.view(-1, 1, num_relations, 1, embedding_dim) @ rel_to_hidden.view(1, 1, 1, embedding_dim, hidden_dim)
    t = t.view(-1, 1, 1, num_tails, embedding_dim) @ tail_to_hidden.view(1, 1, 1, embedding_dim, hidden_dim)
    # TODO: Choosing which to combine first, h/r, h/t or r/t, depending on the shape might further improve
    #       performance in a 1:n scenario.
    return final(activation(bias + h + r + t)).squeeze(dim=-1)


def ermlpe_interaction(
    h: torch.FloatTensor,
    r: torch.FloatTensor,
    t: torch.FloatTensor,
    mlp: nn.Module,
) -> torch.FloatTensor:
    r"""
    Evaluate the ER-MLPE interaction function.

    :param h: shape: (batch_size, num_heads, dim)
        The head representations.
    :param r: shape: (batch_size, num_relations, dim)
        The relation representations.
    :param t: shape: (batch_size, num_tails, dim)
        The tail representations.
    :param mlp:
        The MLP.

    :return: shape: (batch_size, num_heads, num_relations, num_tails)
        The scores.
    """
    # repeat if necessary, and concat head and relation, (batch_size, num_heads, num_relations, 2 * embedding_dim)
    x = broadcast_cat(h.unsqueeze(dim=2), r.unsqueeze(dim=1), dim=-1)

    # Predict t embedding, shape: (batch_size, num_heads, num_relations, embedding_dim)
    x = mlp(x)

    return (x.unsqueeze(dim=-2) @ t.view(t.shape[0], 1, 1, t.shape[1], t.shape[2]).transpose(-2, -1)).squeeze(dim=-1)


def hole_interaction(
    h: torch.FloatTensor,
    r: torch.FloatTensor,
    t: torch.FloatTensor,
) -> torch.FloatTensor:  # noqa: D102
    """Evaluate the HolE interaction function."""
    # Circular correlation of entity embeddings
    a_fft = torch.rfft(h, signal_ndim=1, onesided=True)
    b_fft = torch.rfft(t, signal_ndim=1, onesided=True)

    # complex conjugate, a_fft.shape = (batch_size, num_entities, d', 2)
    a_fft[:, :, :, 1] *= -1

    # Hadamard product in frequency domain
    p_fft = a_fft * b_fft

    # inverse real FFT, shape: (batch_size, num_entities, d)
    composite = torch.irfft(p_fft, signal_ndim=1, onesided=True, signal_sizes=(h.shape[-1],))

    # inner product with relation embedding
    return torch.sum(r * composite, dim=-1, keepdim=False)


def rotate_interaction(
    h: torch.FloatTensor,
    r: torch.FloatTensor,
    t: torch.FloatTensor,
) -> torch.FloatTensor:
    """Evaluate the interaction function of RotatE for given embeddings.

    The embeddings have to be in a broadcastable shape.

    WARNING: No forward constraints are applied.

    :param h: shape: (..., e, 2)
        Head embeddings. Last dimension corresponds to (real, imag).
    :param r: shape: (..., e, 2)
        Relation embeddings. Last dimension corresponds to (real, imag).
    :param t: shape: (..., e, 2)
        Tail embeddings. Last dimension corresponds to (real, imag).

    :return: shape: (...)
        The scores.
    """
    # Decompose into real and imaginary part
    h_re = h[..., 0]
    h_im = h[..., 1]
    r_re = r[..., 0]
    r_im = r[..., 1]

    # Rotate (=Hadamard product in complex space).
    rot_h = torch.stack(
        [
            h_re * r_re - h_im * r_im,
            h_re * r_im + h_im * r_re,
        ],
        dim=-1,
    )
    # Workaround until https://github.com/pytorch/pytorch/issues/30704 is fixed
    diff = rot_h - t
    scores = -torch.norm(diff.view(diff.shape[:-2] + (-1,)), dim=-1)

    return scores


def translational_interaction(
    h: torch.FloatTensor,
    r: torch.FloatTensor,
    t: torch.FloatTensor,
    dim: int,
    p: Union[int, str] = 'fro',
    keepdim: bool = False,
) -> torch.FloatTensor:
    """Evaluate the translational interaction."""
    return -torch.norm(h + r - t, dim=dim, p=p, keepdim=keepdim)


def transr_interaction(
    h: torch.FloatTensor,
    r: torch.FloatTensor,
    t: torch.FloatTensor,
    m_r: torch.FloatTensor,
    p: int,
) -> torch.FloatTensor:
    """Evaluate the interaction function for given embeddings.

    The embeddings have to be in a broadcastable shape.

    :param h: shape: (batch_size, num_entities, d_e)
        Head embeddings.
    :param r: shape: (batch_size, num_entities, d_r)
        Relation embeddings.
    :param t: shape: (batch_size, num_entities, d_e)
        Tail embeddings.
    :param m_r: shape: (batch_size, num_entities, d_e, d_r)
        The relation specific linear transformations.

    :return: shape: (batch_size, num_entities)
        The scores.
    """
    # project to relation specific subspace, shape: (b, e, d_r)
    h_bot = h @ m_r
    t_bot = t @ m_r
    # ensure constraints
    h_bot = clamp_norm(h_bot, p=2, dim=-1, maxnorm=1.)
    t_bot = clamp_norm(t_bot, p=2, dim=-1, maxnorm=1.)

    # evaluate score function, shape: (b, e)
    return translational_interaction(h=h_bot, r=r, t=t_bot, dim=-1, p=p) ** 2