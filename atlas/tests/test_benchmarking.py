import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from benchmarking import (
    _resolve_entity_references,
    derive_bridge_relationships,
    derive_impact,
    derive_relationships,
    run_benchmark,
    score_observation,
    summarize,
    validate_manifest,
)
from ollama_benchmark_agent import _decode_final_answer, _source_files, tool_definitions


def _manifest(repository: Path) -> dict:
    adapter = """import json,sys
r=json.load(sys.stdin)
assert 'ground_truth' not in r and 'entity_catalog' not in r
assert not any(k.startswith('expected_') for k in r['question'])
a=r['condition']=='atlas'
json.dump({
    'answer': 'Found `verify_token`.' if a else 'Found `verify_token` and `invented_function`.',
    'input_tokens': 40 if a else 100,
    'output_tokens': 8 if a else 10,
    'tool_calls': {'Atlas query':1} if a else {'Search':2},
    'relationship_entities': ['login@src/auth.py'] if a else [],
    'impact_entities': ['verify_token@src/tokens.py','login@src/auth.py'] if a else ['verify_token@src/tokens.py']
},sys.stdout)
"""
    return {
        "schema_version": 1,
        "benchmark_id": "test",
        "repository": {
            "name": "fixture",
            "path": str(repository),
            "size_bucket": "small",
            "git_commit": "abc123",
        },
        "model": {"name": "test-model", "version": "1"},
        "entity_catalog": [
            {"id": "entity-target", "name": "verify_token", "kind": "function", "file_path": "src/tokens.py"},
            {"id": "entity-caller", "name": "login", "kind": "function", "file_path": "src/auth.py"},
        ],
        "questions": [
            {
                "id": "q1",
                "category": "relationship",
                "prompt": "Which function calls verify_token?",
                "expected_entities": ["entity-target"],
                "expected_relationships": ["entity-caller"],
                "expected_impact": ["entity-target", "entity-caller"],
            },
            {
                "id": "q2",
                "category": "impact",
                "prompt": "What is impacted by verify_token?",
                "expected_entities": ["entity-target"],
                "expected_relationships": ["entity-caller"],
                "expected_impact": ["entity-target", "entity-caller"],
            },
        ],
        "conditions": {
            "baseline": {
                "command": [sys.executable, "-c", adapter],
                "settings": {"tools": ["Read", "Search", "Grep", "Glob"]},
            },
            "atlas": {
                "command": [sys.executable, "-c", adapter],
                "settings": {"tools": ["Read", "Search", "Grep", "Glob", "Atlas query"]},
            },
        },
    }


def test_run_benchmark_records_paired_tool_token_and_accuracy_metrics(tmp_path):
    manifest = _manifest(tmp_path)
    result = run_benchmark(manifest, repeats=2, seed=9)

    assert len(result["observations"]) == 8
    assert result["summary"]["paired_observations"] == 4
    baseline = result["summary"]["conditions"]["baseline"]
    atlas = result["summary"]["conditions"]["atlas"]
    assert baseline["tool_calls"] == 2
    assert atlas["tool_calls"] == 1
    assert baseline["total_tokens"] == 110
    assert atlas["total_tokens"] == 48
    assert baseline["entity_accuracy"] == 0.5
    assert atlas["entity_accuracy"] == 1
    assert result["summary"]["conditions"]["atlas"]["relationship_f1"] == 1
    assert result["summary"]["conditions"]["atlas"]["impact_f1"] == 1
    assert result["summary"]["effects"]["tool_calls"]["mean"] == 0.5
    assert result["summary"]["effects"]["tool_calls"]["confidence_interval_95"] == [0.5, 0.5]


def test_manifest_rejects_ground_truth_ids_outside_catalog(tmp_path):
    manifest = _manifest(tmp_path)
    manifest["questions"][0]["expected_impact"].append("not-indexed")

    with pytest.raises(ValueError, match="unknown entity ids"):
        validate_manifest(manifest)


def test_manifest_rejects_atlas_access_in_baseline(tmp_path):
    manifest = _manifest(tmp_path)
    manifest["conditions"]["baseline"]["settings"]["tools"].append("Atlas query")

    with pytest.raises(ValueError, match="baseline.settings.tools"):
        validate_manifest(manifest)


def test_no_entity_mentions_is_not_scored_as_perfect_accuracy():
    run = {
        "answer": "",
        "entity_mentions": [],
        "relationship_entities": [],
        "impact_entities": [],
    }
    scored = score_observation(
        {"category": "relationship", "expected_relationships": [], "expected_impact": []},
        run,
        [{"id": "known-id", "name": "known", "kind": "function"}],
    )
    assert scored["entity_accuracy"] is None
    assert scored["relationship"]["f1"] == 1
    assert scored["impact"]["f1"] is None


def test_bad_adapter_output_fails_loudly(tmp_path):
    manifest = _manifest(tmp_path)
    manifest["conditions"]["atlas"]["command"] = [
        sys.executable, "-c", "print('not json')"
    ]
    with pytest.raises(ValueError, match="stdout must be one JSON object"):
        run_benchmark(manifest)


def test_adapter_timeouts_are_explicit(tmp_path):
    manifest = _manifest(tmp_path)
    manifest["conditions"]["baseline"]["command"] = [
        sys.executable, "-c", "import time; time.sleep(2)"
    ]
    with pytest.raises(RuntimeError, match="timed out"):
        run_benchmark(manifest, timeout=0.05)


def test_ground_truth_helpers_derive_direct_callers_and_bounded_impact():
    entities = [
        {"id": "verify", "name": "verify_token", "kind": "function", "calls": [], "called_by": ["login", "refresh"]},
        {"id": "login", "name": "login", "kind": "function", "calls": ["verify_token"], "called_by": ["route"]},
        {"id": "refresh", "name": "refresh", "kind": "function", "calls": ["verify_token"], "called_by": []},
        {"id": "route", "name": "route", "kind": "function", "calls": ["login"], "called_by": []},
        {"id": "http-bridge", "name": "HTTP /login", "kind": "bridge", "bridge_type": "http", "route_path": "/login", "target_function": "verify_token"},
    ]

    assert derive_relationships(entities, "verify") == ["login", "refresh"]
    assert derive_bridge_relationships(
        entities, bridge_type="http", route_path="/login"
    ) == ["http-bridge"]
    assert derive_impact(entities, ["verify"], max_depth=1) == [
        "login", "refresh", "verify"
    ]
    assert derive_impact(entities, ["verify"], max_depth=2) == [
        "login", "refresh", "route", "verify"
    ]


def test_impact_ground_truth_uses_qualified_callers_and_skips_ambiguous_names():
    entities = [
        {
            "id": "target",
            "name": "target",
            "kind": "function",
            "called_by": ["pkg.one.caller", "caller"],
        },
        {
            "id": "caller-one",
            "name": "caller",
            "qualified_name": "pkg.one.caller",
            "kind": "function",
            "called_by": [],
        },
        {
            "id": "caller-two",
            "name": "caller",
            "qualified_name": "pkg.two.caller",
            "kind": "function",
            "called_by": [],
        },
    ]

    assert derive_impact(entities, ["target"], max_depth=1) == [
        "caller-one", "target"
    ]


def test_ollama_adapter_enforces_condition_specific_tools():
    assert [tool["function"]["name"] for tool in tool_definitions(False)] == [
        "Read", "Search", "Grep", "Glob"
    ]
    assert [tool["function"]["name"] for tool in tool_definitions(True)] == [
        "Read", "Search", "Grep", "Glob", "atlas_query", "atlas_impact"
    ]


def test_adapter_source_file_allowlist_excludes_other_workspace_projects(tmp_path):
    (tmp_path / "allowed").mkdir()
    (tmp_path / "private").mkdir()
    (tmp_path / "allowed" / "module.py").write_text("def available(): pass\n", encoding="utf-8")
    (tmp_path / "private" / "secrets.py").write_text("def hidden(): pass\n", encoding="utf-8")

    files = _source_files(tmp_path, {
        "atlas_package": str(Path(__file__).resolve().parent.parent),
        "source_roots": ["allowed"],
    })

    assert list(files) == ["allowed/module.py"]


def test_entity_references_resolve_names_and_file_paths_without_duplicates():
    catalog = [
        {"id": "file-id", "name": "module.py", "kind": "file", "file_path": "src/module.py"},
        {"id": "function-id", "name": "run_task", "kind": "function", "file_path": "src/module.py"},
    ]

    assert _resolve_entity_references(["src/module.py"], catalog) == ["file-id"]
    assert _resolve_entity_references(["run_task@src/module.py"], catalog) == ["function-id"]


def test_summary_reduction_is_consistent_with_reported_condition_means():
    observations = []
    for question_id, baseline_calls, atlas_calls in (
        ("q1", 10, 5),
        ("q2", 100, 100),
    ):
        for condition, calls in (("baseline", baseline_calls), ("atlas", atlas_calls)):
            observations.append({
                "question_id": question_id,
                "condition": condition,
                "tool_calls": {"Read": calls},
                "input_tokens": 1,
                "output_tokens": 0,
                "latency_seconds": 1,
                "entity_accuracy": None,
                "relationship": {"f1": None},
                "impact": {"f1": None},
            })
    result = summarize(observations, seed=42)

    assert result["conditions"]["baseline"]["tool_calls"] == 55
    assert result["conditions"]["atlas"]["tool_calls"] == 52.5
    assert result["effects"]["tool_calls"]["mean"] == pytest.approx(1 - 52.5 / 55)


def test_adapter_accepts_json_fenced_after_explanatory_prose():
    response, response_format = _decode_final_answer(
        'Answer follows.\n```json\n{"answer":"Use `run_task`.","relationship_entities":["run_task@src/module.py"],"impact_entities":[]}\n```',
        "relationship",
    )

    assert response_format == "embedded_json"
    assert response["relationship_entities"] == ["run_task@src/module.py"]
