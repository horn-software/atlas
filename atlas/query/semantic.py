"""Local semantic reranking with the compact all-MiniLM-L6-v2 encoder."""
from functools import lru_cache
import os
import re
from typing import Any

import torch
import torch.nn.functional as F


MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
_MAX_LENGTH = 256


@lru_cache(maxsize=1)
def _load_encoder() -> tuple[Any, Any]:
    """Load the pretrained encoder once; model files are cached by Hugging Face."""
    os.environ["USE_TF"] = "0"
    from transformers import AutoModel, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        MODEL_NAME,
        revision=MODEL_REVISION,
    )
    model = AutoModel.from_pretrained(
        MODEL_NAME,
        revision=MODEL_REVISION,
    ).to("cpu")
    model.eval()
    return tokenizer, model


def _encode(texts: list[str]) -> torch.Tensor:
    tokenizer, model = _load_encoder()
    encoded = tokenizer(
        texts,
        padding=True,
        truncation=True,
        max_length=_MAX_LENGTH,
        return_tensors="pt",
    )
    with torch.inference_mode():
        hidden = model(**encoded).last_hidden_state
        mask = encoded["attention_mask"].unsqueeze(-1).to(hidden.dtype)
        pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1)
        return F.normalize(pooled, p=2, dim=1)


def _hit_text(hit: dict) -> str:
    values = []
    for field in (
        "name", "signature", "docstring", "body_preview", "search_terms",
        "calls", "called_by", "imports", "file_path", "route_path",
        "variable_name",
    ):
        value = hit.get(field)
        if value:
            values.append(" ".join(map(str, value)) if isinstance(value, list)
                          else str(value))
    return " ".join(values)[:4000]


def rerank(question: str, hits: list[dict], limit: int) -> list[dict]:
    """Rerank lexical candidates by question/entity semantic similarity."""
    if not hits:
        return []

    embeddings = _encode([question, *(_hit_text(hit) for hit in hits)])
    similarities = (embeddings[1:] @ embeddings[0]).tolist()
    mentioned_names = {
        token.casefold()
        for token in re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", question)
    }
    ranked = sorted(
        zip(hits, similarities),
        key=lambda item: (
            (item[0].get("name") or "").casefold() in mentioned_names,
            item[1],
        ),
        reverse=True,
    )
    return [hit for hit, _ in ranked[:limit]]
