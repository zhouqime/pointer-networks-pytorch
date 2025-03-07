from typing import Tuple
import torch
import torch.nn as nn


# Adopted from allennlp (https://github.com/allenai/allennlp/blob/master/allennlp/nn/util.py)
def masked_log_softmax(
    vector: torch.Tensor, mask: torch.Tensor, dim: int = -1
) -> torch.Tensor:
    """
    ``torch.nn.functional.log_softmax(vector)`` does not work if some elements of ``vector`` should be
    masked.  This performs a log_softmax on just the non-masked portions of ``vector``.  Passing
    ``None`` in for the mask is also acceptable; you'll just get a regular log_softmax.
    ``vector`` can have an arbitrary number of dimensions; the only requirement is that ``mask`` is
    broadcastable to ``vector's`` shape.  If ``mask`` has fewer dimensions than ``vector``, we will
    unsqueeze on dimension 1 until they match.  If you need a different unsqueezing of your mask,
    do it yourself before passing the mask into this function.
    In the case that the input vector is completely masked, the return value of this function is
    arbitrary, but not ``nan``.  You should be masking the result of whatever computation comes out
    of this in that case, anyway, so the specific values returned shouldn't matter.  Also, the way
    that we deal with this case relies on having single-precision floats; mixing half-precision
    floats with fully-masked vectors will likely give you ``nans``.
    If your logits are all extremely negative (i.e., the max value in your logit vector is -50 or
    lower), the way we handle masking here could mess you up.  But if you've got logit values that
    extreme, you've got bigger problems than this.
    """
    if mask is not None:
        mask = mask.float()
        while mask.dim() < vector.dim():
            mask = mask.unsqueeze(1)
        # vector + mask.log() is an easy way to zero out masked elements in logspace, but it
        # results in nans when the whole vector is masked.  We need a very small value instead of a
        # zero in the mask for these cases.  log(1 + 1e-45) is still basically 0, so we can safely
        # just add 1e-45 before calling mask.log().  We use 1e-45 because 1e-46 is so small it
        # becomes 0 - this is just the smallest value we can actually use.
        vector = vector + (mask + 1e-45).log()
    return torch.nn.functional.log_softmax(vector, dim=dim)


# Adopted from allennlp (https://github.com/allenai/allennlp/blob/master/allennlp/nn/util.py)
def masked_max(
    vector: torch.Tensor,
    mask: torch.Tensor,
    dim: int,
    keepdim: bool = False,
    min_val: float = -1e7,
) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    To calculate max along certain dimensions on masked values
    Parameters
    ----------
    vector : ``torch.Tensor``
            The vector to calculate max, assume unmasked parts are already zeros
    mask : ``torch.Tensor``
            The mask of the vector. It must be broadcastable with vector.
    dim : ``int``
            The dimension to calculate max
    keepdim : ``bool``
            Whether to keep dimension
    min_val : ``float``
            The minimal value for paddings
    Returns
    -------
    A ``torch.Tensor`` of including the maximum values.
    """
    one_minus_mask = (1.0 - mask).byte()
    replaced_vector = vector.masked_fill(one_minus_mask, min_val)
    max_value, max_index = replaced_vector.max(dim=dim, keepdim=keepdim)
    return max_value, max_index


class Encoder(nn.Module):
    def __init__(
        self,
        embedding_dim,
        hidden_size,
        num_layers=1,
        batch_first=True,
        bidirectional=True,
    ):
        super(Encoder, self).__init__()

        self.batch_first = batch_first
        self.rnn = nn.LSTM(
            input_size=embedding_dim,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=batch_first,
            bidirectional=bidirectional,
        )

    def forward(self, embedded_inputs, input_lengths):
        # Pack padded batch of sequences for RNN module
        packed = nn.utils.rnn.pack_padded_sequence(
            embedded_inputs, input_lengths.to("cpu"), batch_first=self.batch_first
        )
        # Forward pass through RNN
        outputs, hidden = self.rnn(packed)
        # Unpack padding
        outputs, _ = nn.utils.rnn.pad_packed_sequence(
            outputs, batch_first=self.batch_first
        )
        # Return output and final hidden state
        return outputs, hidden


class Attention(nn.Module):
    def __init__(self, hidden_size):
        super(Attention, self).__init__()
        self.hidden_size = hidden_size
        self.W1 = nn.Linear(hidden_size, hidden_size, bias=False)
        self.W2 = nn.Linear(hidden_size, hidden_size, bias=False)
        self.vt = nn.Linear(hidden_size, 1, bias=False)

    def forward(self, decoder_state, encoder_outputs, mask):
        # (batch_size, max_seq_len, hidden_size)
        encoder_transform = self.W1(encoder_outputs)

        # (batch_size, 1 (unsqueezed), hidden_size)
        decoder_transform = self.W2(decoder_state).unsqueeze(1)

        # 1st line of Eq.(3) in the paper
        # (batch_size, max_seq_len, 1) => (batch_size, max_seq_len)
        u_i = self.vt(torch.tanh(encoder_transform + decoder_transform)).squeeze(-1)

        # softmax with only valid inputs, excluding zero padded parts
        # log-softmax for a better numerical stability
        log_score = masked_log_softmax(u_i, mask, dim=-1)

        return log_score


class PointerNet(nn.Module):
    def __init__(
        self,
        input_dim,
        embedding_dim,
        hidden_size,
        bidirectional=True,
        batch_first=True,
    ):
        super(PointerNet, self).__init__()

        # Embedding dimension
        self.embedding_dim = embedding_dim
        # (Decoder) hidden size
        self.hidden_size = hidden_size
        # Bidirectional Encoder
        self.bidirectional = bidirectional
        self.num_directions = 2 if bidirectional else 1
        self.num_layers = 1
        self.batch_first = batch_first

        # We use an embedding layer for more complicate application usages later, e.g., word sequences.
        self.embedding = nn.Linear(
            in_features=input_dim, out_features=embedding_dim, bias=False
        )
        self.encoder = Encoder(
            embedding_dim=embedding_dim,
            hidden_size=hidden_size,
            num_layers=self.num_layers,
            bidirectional=bidirectional,
            batch_first=batch_first,
        )
        self.decoding_rnn = nn.LSTMCell(input_size=hidden_size, hidden_size=hidden_size)
        self.attn = Attention(hidden_size=hidden_size)

        for m in self.modules():
            if isinstance(m, nn.Linear):
                if m.bias is not None:
                    torch.nn.init.zeros_(m.bias)

    def forward(self, input_seq, input_lengths):

        if self.batch_first:
            batch_size = input_seq.size(0)
            max_seq_len = input_seq.size(1)
        else:
            batch_size = input_seq.size(1)
            max_seq_len = input_seq.size(0)

        # Embedding
        embedded = self.embedding(input_seq)
        # (batch_size, max_seq_len, embedding_dim)

        # encoder_output => (batch_size, max_seq_len, hidden_size) if batch_first else (max_seq_len, batch_size, hidden_size)
        # hidden_size is usually set same as embedding size
        # encoder_hidden => (num_layers * num_directions, batch_size, hidden_size) for each of h_n and c_n
        encoder_outputs, encoder_hidden = self.encoder(embedded, input_lengths)

        if self.bidirectional:
            # Optionally, Sum bidirectional RNN outputs
            encoder_outputs = (
                encoder_outputs[:, :, : self.hidden_size]
                + encoder_outputs[:, :, self.hidden_size :]
            )

        encoder_h_n, encoder_c_n = encoder_hidden
        encoder_h_n = encoder_h_n.view(
            self.num_layers, self.num_directions, batch_size, self.hidden_size
        )
        encoder_c_n = encoder_c_n.view(
            self.num_layers, self.num_directions, batch_size, self.hidden_size
        )

        # Lets use zeros as an intial input for sorting example
        decoder_input = encoder_outputs.new_zeros(
            torch.Size((batch_size, self.hidden_size))
        )
        decoder_hidden = (
            encoder_h_n[-1, 0, :, :].squeeze(),
            encoder_c_n[-1, 0, :, :].squeeze(),
        )

        range_tensor = torch.arange(
            max_seq_len, device=input_lengths.device, dtype=input_lengths.dtype
        ).expand(batch_size, max_seq_len, max_seq_len)
        each_len_tensor = input_lengths.view(-1, 1, 1).expand(
            batch_size, max_seq_len, max_seq_len
        )

        row_mask_tensor = range_tensor < each_len_tensor
        col_mask_tensor = row_mask_tensor.transpose(1, 2)
        mask_tensor = row_mask_tensor * col_mask_tensor

        pointer_log_scores = []
        pointer_argmaxs = []

        for i in range(max_seq_len):
            # We will simply mask out when calculating attention or max (and loss later)
            # not all input and hiddens, just for simplicity
            sub_mask = mask_tensor[:, i, :].float()

            # h, c: (batch_size, hidden_size)
            h_i, c_i = self.decoding_rnn(decoder_input, decoder_hidden)

            # next hidden
            decoder_hidden = (h_i, c_i)

            # Get a pointer distribution over the encoder outputs using attention
            # (batch_size, max_seq_len)
            log_pointer_score = self.attn(h_i, encoder_outputs, sub_mask)
            pointer_log_scores.append(log_pointer_score)

            # Get the indices of maximum pointer
            _, masked_argmax = masked_max(
                log_pointer_score, sub_mask, dim=1, keepdim=True
            )

            pointer_argmaxs.append(masked_argmax)
            index_tensor = masked_argmax.unsqueeze(-1).expand(
                batch_size, 1, self.hidden_size
            )

            # (batch_size, hidden_size)
            decoder_input = torch.gather(
                encoder_outputs, dim=1, index=index_tensor
            ).squeeze(1)

        pointer_log_scores = torch.stack(pointer_log_scores, 1)
        pointer_argmaxs = torch.cat(pointer_argmaxs, 1)

        return pointer_log_scores, pointer_argmaxs, mask_tensor
