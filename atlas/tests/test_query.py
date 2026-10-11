"""
Test the query model architecture and inference path.
Does NOT require a trained model — tests the untrained model shape
and the data generation pipeline.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "query"))

from query.net import CodeQueryModel, PAD_IDX, UNK_IDX, KIND_LABELS, KIND_TO_IDX


def test_model_shape():
    """Test that the model produces the expected output shapes."""
    vocab_size = 100
    batch_size = 4
    seq_len = 10

    model = CodeQueryModel(vocab_size=vocab_size)

    ids = torch.randint(2, vocab_size, (batch_size, seq_len))
    lengths = torch.tensor([seq_len, 8, 6, 3], dtype=torch.long)

    token_logits, kind_logits = model(ids, lengths)

    assert token_logits.shape == (batch_size, seq_len, 2), \
        f"Token logits shape wrong: {token_logits.shape}"
    assert kind_logits.shape == (batch_size, len(KIND_LABELS)), \
        f"Kind logits shape wrong: {kind_logits.shape}"

    print("  PASS test_model_shape")


def test_model_params():
    """Test that the model stays near ~600K params."""
    model = CodeQueryModel(vocab_size=200)
    total = sum(p.numel() for p in model.parameters())
    assert 400_000 < total < 800_000, \
        f"Model params out of range: {total:,} (expected ~600K)"
    print(f"  PASS test_model_params ({total:,} params)")


def test_model_inference():
    """Test end-to-end inference with a random model."""
    vocab = {"<PAD>": 0, "<UNK>": 1, "what": 2, "function": 3,
             "handles": 4, "auth": 5, "in": 6, "the": 7, "api": 8}

    model = CodeQueryModel(vocab_size=len(vocab))
    model.eval()

    question = "what function handles auth in the api"
    tokens = question.split()
    ids = torch.tensor(
        [vocab.get(t.lower(), UNK_IDX) for t in tokens],
        dtype=torch.long,
    ).unsqueeze(0)
    lengths = torch.tensor([len(tokens)], dtype=torch.long)

    with torch.no_grad():
        token_logits, kind_logits = model(ids, lengths)

    token_preds = token_logits.argmax(dim=-1).squeeze(0).tolist()
    kind_idx = kind_logits.argmax(dim=-1).item()

    assert len(token_preds) == len(tokens), "Should have one prediction per token"
    assert all(p in (0, 1) for p in token_preds), "Predictions should be 0 or 1"
    assert 0 <= kind_idx < len(KIND_LABELS), f"Kind index out of range: {kind_idx}"

    kept = [w for w, p in zip(tokens, token_preds) if p == 1]
    predicted_kind = KIND_LABELS[kind_idx]
    print(f"  PASS test_model_inference (kept={kept}, kind={predicted_kind})")


def test_data_generation():
    """Test that generate_data produces valid training examples."""
    from ingest.parsers import parse_file
    from ingest.call_graph import build_call_graph
    from query.generate_data import generate_from_entities

    sample = '''
def authenticate(username: str, password: str) -> bool:
    """Check credentials."""
    return verify_hash(password)

def verify_hash(password: str) -> bool:
    return True

class UserManager:
    def create_user(self, name: str):
        authenticate(name, "default")
'''

    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = Path(tmpdir) / "auth.py"
        fpath.write_text(sample, encoding="utf-8")

        entities = parse_file(str(fpath), tmpdir)
        entities = build_call_graph(entities)

        examples = generate_from_entities(entities, target_count=50)

        assert len(examples) >= 50, f"Expected 50+ examples, got {len(examples)}"

        for ex in examples[:10]:
            assert "text" in ex, "Example missing 'text'"
            assert "tokens" in ex, "Example missing 'tokens'"
            assert "labels" in ex, "Example missing 'labels'"
            assert "kind" in ex, "Example missing 'kind'"
            assert len(ex["tokens"]) == len(ex["labels"]), \
                f"Token/label length mismatch: {len(ex['tokens'])} vs {len(ex['labels'])}"
            assert ex["kind"] in KIND_TO_IDX, f"Unknown kind: {ex['kind']}"
            assert sum(ex["labels"]) > 0, f"No KEEP labels in: {ex['text']}"

        print(f"  PASS test_data_generation ({len(examples)} examples)")


if __name__ == "__main__":
    print("Running query tests...")
    test_model_shape()
    test_model_params()
    test_model_inference()
    test_data_generation()
    print("All query tests passed.")
