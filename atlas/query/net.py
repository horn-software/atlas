"""
BiLSTM token classifier with kind classification head for code queries.
torch.nn only — randomly initialised, no pretrained weights.
~600K params, same pattern as knowledge/model/.

Two heads:
  1. Token classifier (Linear 256→2): KEEP / DROP per token
  2. Kind classifier  (Linear 256→6): file / function / class / method / import / any
     (applied to mean-pooled hidden state)
"""
import torch
import torch.nn as nn
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence

PAD_IDX = 0
UNK_IDX = 1

KIND_LABELS = ["file", "function", "class", "method", "import", "any"]
KIND_TO_IDX = {k: i for i, k in enumerate(KIND_LABELS)}


class CodeQueryModel(nn.Module):
    def __init__(self, vocab_size: int, embed_dim: int = 64, hidden: int = 128,
                 num_layers: int = 2, dropout: float = 0.3, num_kinds: int = 6):
        super().__init__()
        self.embedding = nn.Embedding(vocab_size, embed_dim, padding_idx=PAD_IDX)
        self.bilstm = nn.LSTM(
            embed_dim, hidden,
            num_layers=num_layers,
            bidirectional=True,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0.0,
        )
        self.drop = nn.Dropout(dropout)
        self.token_head = nn.Linear(hidden * 2, 2)
        self.kind_head = nn.Linear(hidden * 2, num_kinds)

    def forward(self, x, lengths):
        emb = self.drop(self.embedding(x))
        packed = pack_padded_sequence(emb, lengths.cpu(), batch_first=True, enforce_sorted=False)
        out, _ = self.bilstm(packed)
        out, _ = pad_packed_sequence(out, batch_first=True)
        out = self.drop(out)

        token_logits = self.token_head(out)

        mask = torch.arange(out.size(1), device=out.device).unsqueeze(0) < lengths.unsqueeze(1)
        masked = out * mask.unsqueeze(2).float()
        pooled = masked.sum(dim=1) / lengths.unsqueeze(1).float().to(out.device)
        kind_logits = self.kind_head(pooled)

        return token_logits, kind_logits
