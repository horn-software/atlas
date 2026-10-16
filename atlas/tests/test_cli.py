"""Tests for Atlas CLI setup behavior."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli import _prepare_semantic_model


def test_prepare_semantic_model_loads_pinned_encoder(monkeypatch, capsys):
    from query import semantic

    loaded = []
    monkeypatch.setattr(semantic, "_load_encoder", lambda: loaded.append(True))

    assert _prepare_semantic_model() is True
    assert loaded == [True]
    assert semantic.MODEL_NAME in capsys.readouterr().out


def test_prepare_semantic_model_reports_offline_download_failure(monkeypatch, capsys):
    from query import semantic

    def fail_to_download():
        raise OSError("network unavailable")

    monkeypatch.setattr(semantic, "_load_encoder", fail_to_download)

    assert _prepare_semantic_model() is False
    output = capsys.readouterr().out
    assert "network unavailable" in output
    assert "rerun atlas init to retry" in output
    assert "lexical search" in output


def test_prepare_semantic_model_reports_missing_transformers(monkeypatch, capsys):
    from query import semantic

    def missing_dependency():
        raise ImportError("transformers missing")

    monkeypatch.setattr(semantic, "_load_encoder", missing_dependency)

    assert _prepare_semantic_model() is False
    output = capsys.readouterr().out
    assert "transformers missing" in output
    assert "pip install" in output
