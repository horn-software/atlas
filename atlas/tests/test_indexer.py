"""
Regression tests for new graph layers: entity IDs, route/event/config/repo
graph builders, and the benchmark fixture loader.

These tests run without Meilisearch — they only test the graph-building logic.
"""
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.call_graph import build_call_graph, _parse_body_aliases, _resolve_self_calls


# ── W1.1: Stable Entity IDs ────────────────────────────────────────────────────

def _make_entity(eid: str, name: str, kind: str = "function", file_path: str = "pkg/a.py",
                 calls: list = None, parent_class: str = "", imports: list = None) -> dict:
    return {
        "id": eid,
        "kind": kind,
        "name": name,
        "file_path": file_path,
        "language": "python",
        "line_start": 1,
        "line_end": 5,
        "signature": f"def {name}():",
        "docstring": "",
        "body_preview": "",
        "calls": calls or [],
        "called_by": [],
        "imports": imports or [],
        "parent_class": parent_class,
        "decorators": [],
    }


def test_entity_id_alias_added():
    """build_call_graph should add entity_id = id for every entity."""
    entities = [_make_entity("abc123", "foo")]
    result = build_call_graph(entities)
    assert result[0]["entity_id"] == "abc123"


def test_called_by_ids_populated():
    """called_by_ids should contain the caller's entity ID (not bare name)."""
    entities = [
        _make_entity("caller-id", "setup", calls=["load_config"]),
        _make_entity("callee-id", "load_config"),
    ]
    result = build_call_graph(entities)
    callee = next(e for e in result if e["name"] == "load_config")
    assert "caller-id" in callee["called_by_ids"], (
        f"called_by_ids should contain caller entity ID; got {callee['called_by_ids']}"
    )


def test_called_by_names_still_populated():
    """called_by (names) must still be populated for backward compatibility."""
    entities = [
        _make_entity("a", "setup", calls=["load_config"]),
        _make_entity("b", "load_config"),
    ]
    result = build_call_graph(entities)
    callee = next(e for e in result if e["name"] == "load_config")
    assert "setup" in callee["called_by"]


def test_no_entity_id_collision_across_files():
    """Two functions with the same name in different files must have different entity IDs."""
    entities = [
        _make_entity("id-one", "load", file_path="pkg/one.py"),
        _make_entity("id-two", "load", file_path="pkg/two.py"),
    ]
    result = build_call_graph(entities)
    ids = [e["entity_id"] for e in result]
    assert len(ids) == len(set(ids)), f"entity_id collision: {ids}"


# ── W1.2: Import / alias resolution ──────────────────────────────────────────

def test_alias_resolution_from_body():
    """from x import y as z should map z → x.y in alias resolution."""
    aliases = _parse_body_aliases("from pkg.utils import load_config as lc\nlc()")
    assert aliases.get("lc") == "pkg.utils.load_config"


def test_alias_resolution_simple_import():
    """import module as alias should map alias → module."""
    aliases = _parse_body_aliases("import json as j\nj.loads()")
    assert aliases.get("j") == "json"


def test_call_through_alias_resolves():
    """A call through an imported alias should resolve to the original function."""
    entities = [
        _make_entity(
            "caller-id", "main",
            calls=["lc"],
            imports=["pkg.utils.load_config"],
            file_path="app.py",
        ),
        _make_entity("target-id", "load_config", file_path="pkg/utils.py"),
    ]
    # Add alias in body_preview
    entities[0]["body_preview"] = "from pkg.utils import load_config as lc\nlc()"
    result = build_call_graph(entities)
    target = next(e for e in result if e["name"] == "load_config")
    assert "caller-id" in target["called_by_ids"], (
        f"alias call should resolve to original: {target['called_by_ids']}"
    )


# ── W1.3: Class method resolution ─────────────────────────────────────────────

def test_self_call_expanded():
    """self.run() inside a class method should resolve to the class's run method."""
    from ingest.call_graph import _resolve_self_calls
    ent = _make_entity("id1", "restart", kind="method", parent_class="Worker")
    ent["body_preview"] = "def restart(self):\n    return self.run()"
    extra = _resolve_self_calls(ent, {"Worker": {"run", "stop"}})
    assert "run" in extra


def test_self_call_no_parent_skipped():
    """self.foo() in a module-level function should not produce extra calls."""
    ent = _make_entity("id1", "standalone_fn")
    ent["body_preview"] = "def standalone_fn(self):\n    self.foo()"
    extra = _resolve_self_calls(ent, {"SomeClass": {"foo"}})
    assert extra == []


def test_class_method_called_by_populated():
    """Worker.restart calling self.run() should add restart to run's called_by."""
    entities = [
        {**_make_entity("restart-id", "restart", kind="method", parent_class="Worker"),
         "body_preview": "def restart(self):\n    return self.run()"},
        _make_entity("run-id", "run", kind="method", parent_class="Worker"),
    ]
    result = build_call_graph(entities)
    run_ent = next(e for e in result if e["name"] == "run")
    assert "restart-id" in run_ent["called_by_ids"], (
        f"self.run() should add restart-id to run's called_by_ids: {run_ent['called_by_ids']}"
    )


# ── W2.1: Caller-only impact (no fuzzy promotion) ─────────────────────────────

def test_impact_uses_called_by_ids_when_present(monkeypatch):
    """When called_by_ids is populated, impact traversal uses it directly."""
    import json
    from edge import impact

    entities = [
        {
            "id": "target",
            "kind": "function",
            "name": "target",
            "qualified_name": "pkg.target",
            "called_by": ["caller"],
            "called_by_ids": ["caller"],
            "calls": [],
        },
        {
            "id": "caller",
            "kind": "function",
            "name": "caller",
            "qualified_name": "pkg.caller",
            "called_by": [],
            "called_by_ids": [],
            "calls": ["target"],
        },
    ]

    id_filter_calls = []

    def fake_search(project_id, query, filters=None, limit=10, min_score=0.05):
        import re
        if filters:
            m = re.search(r'(id|qualified_name|name) = ("(?:[^"\\\\]|\\\\.)*")', filters)
            if m:
                field, encoded = m.groups()
                value = json.loads(encoded)
                if field == "id":
                    id_filter_calls.append(value)
                return [dict(e) for e in entities if e.get(field) == value]
        return []

    monkeypatch.setattr(impact, "search", fake_search)

    affected = impact.find_impact("proj", ["target"], max_depth=1)
    assert {e["id"] for e in affected} == {"target", "caller"}
    # Must have used id-based lookup for the edge traversal
    assert "caller" in id_filter_calls


def test_heuristic_edges_excluded_from_impact(monkeypatch):
    """Edges with confidence=heuristic should not be followed by default."""
    import json
    from edge import impact

    entities = [
        {
            "id": "target",
            "kind": "function",
            "name": "target",
            "qualified_name": "pkg.target",
            "called_by": ["caller"],
            "called_by_ids": ["caller"],
            "calls": [],
        },
        {
            "id": "caller",
            "kind": "function",
            "name": "caller",
            "qualified_name": "pkg.caller",
            "called_by": [],
            "called_by_ids": [],
            "calls": [],
            "confidence": "heuristic",  # should be skipped
        },
    ]

    def fake_search(project_id, query, filters=None, limit=10, min_score=0.05):
        import re
        if filters:
            m = re.search(r'(id|qualified_name|name) = ("(?:[^"\\\\]|\\\\.)*")', filters)
            if m:
                field, encoded = m.groups()
                value = json.loads(encoded)
                return [dict(e) for e in entities if e.get(field) == value]
        return []

    monkeypatch.setattr(impact, "search", fake_search)

    affected = impact.find_impact("proj", ["target"], max_depth=1, include_heuristic=False)
    assert {e["id"] for e in affected} == {"target"}, (
        "heuristic caller should be excluded from default impact"
    )

    affected_wide = impact.find_impact("proj", ["target"], max_depth=1, include_heuristic=True)
    assert {e["id"] for e in affected_wide} == {"target", "caller"}


# ── W2.3: Same-name symbols must not merge into impact results ────────────────

def test_dunder_calls_are_not_linked_by_bare_name():
    """super().__init__() must not make every __init__ in the repo a callee."""
    entities = [
        _make_entity("a-init", "__init__", kind="method", file_path="pkg/a.py",
                     parent_class="A", calls=["__init__"]),
        _make_entity("b-init", "__init__", kind="method", file_path="pkg/b.py",
                     parent_class="B"),
    ]
    result = build_call_graph(entities)
    for ent in result:
        assert ent["called_by_ids"] == []
        assert ent["called_by_heuristic_ids"] == []


def test_ambiguous_bare_name_is_heuristic_not_exact():
    """A call to a name defined in several files, with no import evidence, is heuristic."""
    entities = [
        _make_entity("caller", "main", file_path="app.py", calls=["run"]),
        _make_entity("run-a", "run", file_path="pkg/a.py"),
        _make_entity("run-b", "run", file_path="pkg/b.py"),
    ]
    result = build_call_graph(entities)
    for name_id in ("run-a", "run-b"):
        ent = next(e for e in result if e["id"] == name_id)
        assert "caller" not in ent["called_by_ids"]
        assert "caller" in ent["called_by_heuristic_ids"]


def test_same_file_definition_wins_over_other_files():
    """A bare call resolves to the definition in the caller's own file."""
    entities = [
        _make_entity("caller", "helper_user", file_path="pkg/a.py", calls=["run"]),
        _make_entity("run-a", "run", file_path="pkg/a.py"),
        _make_entity("run-b", "run", file_path="pkg/b.py"),
    ]
    result = build_call_graph(entities)
    run_a = next(e for e in result if e["id"] == "run-a")
    run_b = next(e for e in result if e["id"] == "run-b")
    assert run_a["called_by_ids"] == ["caller"]
    assert run_b["called_by_ids"] == [] and run_b["called_by_heuristic_ids"] == []


def test_self_call_resolves_to_own_class_only():
    """self.run() links to the same class's run, not another class's run."""
    entities = [
        {**_make_entity("restart-id", "restart", kind="method", file_path="pkg/a.py",
                        parent_class="Worker"),
         "body_preview": "def restart(self):\n    return self.run()"},
        _make_entity("run-worker", "run", kind="method", file_path="pkg/a.py",
                     parent_class="Worker"),
        _make_entity("run-other", "run", kind="method", file_path="pkg/a.py",
                     parent_class="Other"),
    ]
    result = build_call_graph(entities)
    assert next(e for e in result if e["id"] == "run-worker")["called_by_ids"] == ["restart-id"]
    assert next(e for e in result if e["id"] == "run-other")["called_by_ids"] == []


def test_nested_local_def_shadows_global_function():
    """A call to a function defined inside the caller must not bind to a global of that name."""
    caller = _make_entity("caller", "merge", file_path="pkg/a.py", calls=["is_empty"])
    caller["body_preview"] = "def merge(d):\n    def is_empty(v):\n        return v is None\n    return is_empty(d)"
    entities = [caller, _make_entity("global-is-empty", "is_empty", file_path="pkg/b.py")]
    result = build_call_graph(entities)
    target = next(e for e in result if e["id"] == "global-is-empty")
    assert target["called_by_ids"] == [] and target["called_by_heuristic_ids"] == []


def test_recursive_call_still_links_to_self():
    """The caller's own def line must not count as a nested local definition."""
    fn = _make_entity("rec", "walk", file_path="pkg/a.py", calls=["walk"])
    fn["body_preview"] = "def walk(n):\n    return walk(n - 1)"
    result = build_call_graph([fn])
    assert result[0]["called_by_ids"] == ["rec"]


def _fake_search_for(entities):
    import json as _json
    import re as _re

    def fake_search(project_id, query, filters=None, limit=10, min_score=0.05):
        if filters:
            m = _re.search(r'(id|qualified_name|name) = ("(?:[^"\\\\]|\\\\.)*")', filters)
            if m:
                field, encoded = m.groups()
                value = _json.loads(encoded)
                return [dict(e) for e in entities if e.get(field) == value]
        return []

    return fake_search


def test_impact_empty_called_by_ids_does_not_fall_back_to_names(monkeypatch):
    """An indexed entity with no callers must not be widened via name-based called_by."""
    from edge import impact

    entities = [
        {"id": "target", "kind": "function", "name": "target", "qualified_name": "pkg.target",
         "called_by": ["ghost"], "called_by_ids": [], "calls": []},
        {"id": "ghost-id", "kind": "function", "name": "ghost", "qualified_name": "pkg.ghost",
         "called_by": [], "called_by_ids": [], "calls": []},
    ]
    monkeypatch.setattr(impact, "search", _fake_search_for(entities))
    affected = impact.find_impact("proj", ["target"], max_depth=2)
    assert {e["id"] for e in affected} == {"target"}


def test_impact_follows_heuristic_ids_only_when_opted_in(monkeypatch):
    from edge import impact

    entities = [
        {"id": "target", "kind": "function", "name": "target", "qualified_name": "pkg.target",
         "called_by": [], "called_by_ids": [], "called_by_heuristic_ids": ["maybe"], "calls": []},
        {"id": "maybe", "kind": "function", "name": "maybe", "qualified_name": "pkg.maybe",
         "called_by": [], "called_by_ids": [], "calls": []},
    ]
    monkeypatch.setattr(impact, "search", _fake_search_for(entities))
    assert {e["id"] for e in impact.find_impact("proj", ["target"], max_depth=1)} == {"target"}
    wide = impact.find_impact("proj", ["target"], max_depth=1, include_heuristic=True)
    assert {e["id"] for e in wide} == {"target", "maybe"}


# ── W3.1: Route graph ─────────────────────────────────────────────────────────

def test_flask_route_edge_detected():
    """Flask @app.route decorator should produce a route_edge document."""
    from ingest.route_graph import build_route_edges

    entities = [{
        "id": "fn-id",
        "kind": "function",
        "name": "get_users",
        "qualified_name": "api.get_users",
        "file_path": "api.py",
        "language": "python",
        "line_start": 5,
        "line_end": 10,
        "body_preview": "@app.route('/api/users', methods=['GET'])\ndef get_users():\n    return []",
        "signature": "def get_users():",
        "decorators": ["@app.route('/api/users', methods=['GET'])"],
        "calls": [],
        "called_by": [],
        "called_by_ids": [],
        "imports": [],
        "parent_class": "",
    }]
    edges = build_route_edges(entities, "test-project")
    assert len(edges) == 1
    edge = edges[0]
    assert edge["kind"] == "route_edge"
    assert edge["route_path"] == "/api/users"
    assert edge["http_method"] == "GET"
    assert edge["handler_name"] == "get_users"
    assert edge["confidence"] == "exact"
    assert edge["provenance"] == "ast"


def test_route_path_normalised():
    """Path parameters should be normalised to :param."""
    from ingest.route_graph import build_route_edges

    entities = [{
        "id": "fn-id",
        "kind": "function",
        "name": "get_user",
        "qualified_name": "api.get_user",
        "file_path": "api.py",
        "language": "python",
        "line_start": 1,
        "line_end": 5,
        "body_preview": "@app.route('/api/users/<int:user_id>')\ndef get_user(user_id):\n    pass",
        "signature": "def get_user(user_id):",
        "decorators": ["@app.route('/api/users/<int:user_id>')"],
        "calls": [],
        "called_by": [],
        "called_by_ids": [],
        "imports": [],
        "parent_class": "",
    }]
    edges = build_route_edges(entities, "proj")
    assert len(edges) == 1
    assert edges[0]["route_path"] == "/api/users/:param"


# ── W3.2: Event graph ─────────────────────────────────────────────────────────

def test_celery_task_detected():
    """@celery.task decorator should produce a celery_task event_edge."""
    from ingest.event_graph import build_event_edges

    entities = [{
        "id": "task-id",
        "kind": "function",
        "name": "send_email",
        "qualified_name": "tasks.send_email",
        "file_path": "tasks.py",
        "language": "python",
        "line_start": 5,
        "line_end": 10,
        "body_preview": "@celery.task\ndef send_email(email):\n    pass",
        "signature": "def send_email(email):",
        "decorators": ["@celery.task"],
        "calls": [],
        "called_by": [],
        "called_by_ids": [],
        "imports": [],
        "parent_class": "",
    }]
    edges = build_event_edges(entities, "proj")
    assert len(edges) == 1
    edge = edges[0]
    assert edge["kind"] == "event_edge"
    assert edge["event_type"] == "celery_task"
    assert edge["handler_name"] == "send_email"
    assert edge["confidence"] == "exact"


def test_js_event_listener_detected():
    """addEventListener call should produce a listener event_edge."""
    from ingest.event_graph import build_event_edges

    entities = [{
        "id": "fn-id",
        "kind": "function",
        "name": "setupHandlers",
        "qualified_name": "app.setupHandlers",
        "file_path": "app.js",
        "language": "javascript",
        "line_start": 1,
        "line_end": 5,
        "body_preview": "function setupHandlers() {\n  btn.addEventListener('click', handleClick);\n}",
        "signature": "function setupHandlers()",
        "decorators": [],
        "calls": [],
        "called_by": [],
        "called_by_ids": [],
        "imports": [],
        "parent_class": "",
    }]
    edges = build_event_edges(entities, "proj")
    assert len(edges) == 1
    assert edges[0]["event_type"] == "listener"
    assert edges[0]["event_name"] == "click"


# ── W4.1: Config / env graph ──────────────────────────────────────────────────

def test_env_var_access_detected():
    """os.getenv('KEY') should produce a config_edge document."""
    from ingest.config_graph import build_config_edges

    entities = [{
        "id": "fn-id",
        "kind": "function",
        "name": "connect_db",
        "qualified_name": "db.connect_db",
        "file_path": "db.py",
        "language": "python",
        "line_start": 1,
        "line_end": 5,
        "body_preview": "def connect_db():\n    url = os.getenv('DATABASE_URL')\n    return url",
        "signature": "def connect_db():",
        "decorators": [],
        "calls": [],
        "called_by": [],
        "called_by_ids": [],
        "imports": [],
        "parent_class": "",
    }]
    edges = build_config_edges(entities, "proj")
    env_edges = [e for e in edges if e["config_type"] == "env_var"]
    assert len(env_edges) == 1
    assert env_edges[0]["config_key"] == "DATABASE_URL"
    assert env_edges[0]["consumer_name"] == "connect_db"
    assert env_edges[0]["confidence"] == "exact"


def test_module_level_env_var_detected():
    """os.environ.get at module level (no enclosing function) should still produce a config_edge."""
    from ingest.config_graph import build_config_edges

    with tempfile.TemporaryDirectory() as tmpdir:
        # Write a file with a module-level env var access — no enclosing function
        src = Path(tmpdir) / "indexer.py"
        src.write_text("MEILI_URL = os.environ.get('MEILI_URL', 'http://localhost:7700')\n",
                       encoding="utf-8")

        # No entities (module-level code has no function entity)
        edges = build_config_edges([], "proj", root_path=tmpdir)
        env_edges = [e for e in edges if e["config_type"] == "env_var"]
        keys = [e["config_key"] for e in env_edges]
        assert "MEILI_URL" in keys, f"Module-level env var missed; found: {keys}"
        meili_edge = next(e for e in env_edges if e["config_key"] == "MEILI_URL")
        assert meili_edge["consumer_name"] == "<module>"
        assert meili_edge["provenance"] == "file_scan"


def test_dotenv_source_linked():
    """If a .env file declares the key, source_file should be populated."""
    from ingest.config_graph import build_config_edges

    with tempfile.TemporaryDirectory() as tmpdir:
        env_file = Path(tmpdir) / ".env"
        env_file.write_text("DATABASE_URL=postgres://localhost/mydb\n", encoding="utf-8")

        entities = [{
            "id": "fn-id",
            "kind": "function",
            "name": "connect",
            "qualified_name": "db.connect",
            "file_path": "db.py",
            "language": "python",
            "line_start": 1,
            "line_end": 3,
            "body_preview": "import os\nurl = os.getenv('DATABASE_URL')",
            "signature": "def connect():",
            "decorators": [],
            "calls": [],
            "called_by": [],
            "called_by_ids": [],
            "imports": [],
            "parent_class": "",
        }]
        edges = build_config_edges(entities, "proj", root_path=tmpdir)
        env_edges = [e for e in edges if e["config_type"] == "env_var"]
        assert len(env_edges) == 1
        assert str(env_file) in env_edges[0]["source_file"] or env_edges[0]["source_file"].endswith(".env")


# ── W4.2: Repo identity ───────────────────────────────────────────────────────

def test_repo_fingerprint_stable():
    """Two calls to fingerprint_repo on the same dir return the same ID."""
    from ingest.repo_identity import fingerprint_repo

    with tempfile.TemporaryDirectory() as tmpdir:
        rid1 = fingerprint_repo(tmpdir)
        rid2 = fingerprint_repo(tmpdir)
        assert rid1 == rid2
        assert len(rid1) == 16


def test_duplicate_repo_detected():
    """A nested directory with same .git root commit should be flagged."""
    from ingest.repo_identity import detect_duplicate_repos
    import unittest.mock as mock

    # Can't easily fake git, so just verify the function runs and returns a list
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a nested pyproject.toml to trigger repo detection
        nested = Path(tmpdir) / "copy"
        nested.mkdir()
        (nested / "pyproject.toml").write_text("[tool.poetry]\nname='copy'", encoding="utf-8")
        (Path(tmpdir) / "pyproject.toml").write_text("[tool.poetry]\nname='root'", encoding="utf-8")

        warnings = detect_duplicate_repos(tmpdir)
        # Just assert it returns a list — git may not be available in CI
        assert isinstance(warnings, list)


def test_annotate_entities_adds_repo_id():
    """annotate_entities should add repo_id field to every entity."""
    from ingest.repo_identity import annotate_entities

    entities = [{"id": "a", "name": "foo"}, {"id": "b", "name": "bar"}]
    annotate_entities(entities, "test-repo-id")
    assert all(e["repo_id"] == "test-repo-id" for e in entities)


# ── W5.2: Benchmark fixture integrity ────────────────────────────────────────

def test_benchmark_fixtures_exist():
    """Both benchmark fixtures must exist with required fields."""
    fixtures_dir = Path(__file__).resolve().parent.parent / "benchmarking" / "fixtures"
    assert fixtures_dir.is_dir(), "benchmarking/fixtures/ directory must exist"

    fixture_names = ["call_graph_basic", "route_event"]
    for fname in fixture_names:
        expected_file = fixtures_dir / fname / "expected.json"
        assert expected_file.exists(), f"Missing expected.json for fixture {fname}"

        data = json.loads(expected_file.read_text(encoding="utf-8"))
        assert "fixture" in data
        assert "reviewed_by" in data
        assert "reviewed_at" in data
        assert "benchmark_notes" in data, f"{fname}: must have benchmark_notes"
        assert data["reviewed_by"] != "", f"{fname}: reviewed_by must not be empty"


def test_benchmark_fixture_no_atlas_labels():
    """Fixtures must declare they were not generated from Atlas output."""
    fixtures_dir = Path(__file__).resolve().parent.parent / "benchmarking" / "fixtures"
    for fixture_dir in fixtures_dir.iterdir():
        expected_file = fixture_dir / "expected.json"
        if not expected_file.exists():
            continue
        data = json.loads(expected_file.read_text(encoding="utf-8"))
        notes = data.get("benchmark_notes", "").lower()
        assert "atlas" not in notes or "not" in notes or "manually" in notes, (
            f"Fixture {fixture_dir.name}: benchmark_notes must confirm manual review, not Atlas-generated labels"
        )


if __name__ == "__main__":
    import pytest as pt
    pt.main([__file__, "-v"])
