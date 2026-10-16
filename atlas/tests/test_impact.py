"""Tests for exact impact and dependency graph traversal."""
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edge import impact


ENTITIES = [
    {
        "id": "target-id",
        "kind": "function",
        "name": "target",
        "qualified_name": "pkg.target",
        "called_by": ["pkg.caller"],
        "calls": ["pkg.callee"],
    },
    {
        "id": "caller-id",
        "kind": "function",
        "name": "caller",
        "qualified_name": "pkg.caller",
        "called_by": ["pkg.root"],
        "calls": ["pkg.target"],
    },
    {
        "id": "root-id",
        "kind": "function",
        "name": "root",
        "qualified_name": "pkg.root",
        "called_by": [],
        "calls": ["pkg.caller"],
    },
    {
        "id": "callee-id",
        "kind": "function",
        "name": "callee",
        "qualified_name": "pkg.callee",
        "called_by": ["pkg.target"],
        "calls": [],
    },
]


def _install_exact_search(monkeypatch):
    calls = []

    def exact_search(project_id, query, filters=None, limit=10, min_score=0.05):
        calls.append((project_id, query, filters, min_score))
        match = re.search(r'(id|qualified_name|name) = ("(?:[^"\\\\]|\\\\.)*")', filters or "")
        if not match:
            return []
        field, encoded_value = match.groups()
        value = json.loads(encoded_value)
        return [
            dict(entity)
            for entity in ENTITIES
            if entity.get(field) == value
        ]

    monkeypatch.setattr(impact, "search", exact_search)
    return calls


def test_impact_traverses_only_exact_caller_chains(monkeypatch):
    calls = _install_exact_search(monkeypatch)

    affected = impact.find_impact("project", ["target-id"], max_depth=3)

    assert {entity["id"] for entity in affected} == {
        "target-id", "caller-id", "root-id",
    }
    assert "callee-id" not in {entity["id"] for entity in affected}
    assert {entity["id"]: entity["_impact_depth"] for entity in affected} == {
        "target-id": 0,
        "caller-id": 1,
        "root-id": 2,
    }
    assert calls
    assert all(query == "" and min_score == 0 for _, query, _, min_score in calls)


def test_impact_resolves_qualified_symbol_and_honors_depth(monkeypatch):
    _install_exact_search(monkeypatch)

    affected = impact.find_impact("project", ["pkg.target"], max_depth=1)

    assert {entity["id"] for entity in affected} == {"target-id", "caller-id"}


def test_impact_rejects_ambiguous_bare_symbol(monkeypatch):
    _install_exact_search(monkeypatch)
    duplicate = {
        "id": "other-target",
        "kind": "function",
        "name": "target",
        "qualified_name": "other.target",
        "called_by": [],
        "calls": [],
    }
    original = impact.search

    def search_with_duplicate(project_id, query, filters=None, limit=10, min_score=0.05):
        results = original(project_id, query, filters, limit, min_score)
        if 'name = "target"' in (filters or ""):
            results.append(dict(duplicate))
        return results

    monkeypatch.setattr(impact, "search", search_with_duplicate)

    with pytest.raises(ValueError, match="ambiguous"):
        impact.find_impact("project", ["target"])


def test_impact_does_not_promote_unresolved_or_fuzzy_results(monkeypatch):
    calls = _install_exact_search(monkeypatch)

    with pytest.raises(ValueError, match="no indexed function or method exactly matches"):
        impact.find_impact("project", ["near-match"])

    assert calls
    assert all(query == "" for _, query, _, _ in calls)


def test_dependency_exploration_traverses_callers_and_callees(monkeypatch):
    _install_exact_search(monkeypatch)

    dependencies = impact.find_dependencies("project", ["target-id"], max_depth=1)

    assert {entity["id"] for entity in dependencies} == {
        "target-id", "caller-id", "callee-id",
    }


def test_traversal_deduplicates_cycles_by_entity_id(monkeypatch):
    _install_exact_search(monkeypatch)

    affected = impact.find_impact("project", ["target-id"], max_depth=5)

    ids = [entity["id"] for entity in affected]
    assert len(ids) == len(set(ids))
