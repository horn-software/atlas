"""
Self-benchmark runner: tests Atlas vs baseline on the Atlas codebase itself.

Runs 4 conditions:
  gemma-baseline   — Gemma 4e4b, no Atlas tools
  gemma-atlas      — Gemma 4e4b, Atlas tools
  claude-baseline  — Claude Sonnet 4.6, no Atlas tools
  claude-atlas     — Claude Sonnet 4.6, Atlas tools

Usage:
  python run_self_benchmark.py [--questions N] [--repeats N] [--out results.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

MEILI_URL = os.environ.get("MEILI_URL", "http://localhost:7700")
MEILI_KEY = os.environ.get("MEILI_KEY", "poc_master_key_local_1234567890")
ATLAS_ROOT = str(_ROOT)

GEMMA_MODEL = "gemma4:e4b"
CLAUDE_MODEL = "claude-sonnet-4-6"

TOOL_TYPES = {"Read", "Search", "Grep", "Glob", "Atlas query"}
CONDITIONS = ("baseline", "atlas")


def _build_manifest(n_questions: int, seed: int, repo_path: str | None = None) -> dict:
    """Generate a benchmark manifest. Defaults to the Atlas codebase itself."""
    from benchmarking import (
        catalog_from_entities,
        derive_impact,
        derive_relationships,
    )
    from ingest.call_graph import build_call_graph
    from ingest.crawler import crawl
    from ingest.indexer import create_index, index_documents
    from ingest.parsers import parse_file

    os.environ["MEILI_URL"] = MEILI_URL
    os.environ["MEILI_KEY"] = MEILI_KEY

    root = Path(repo_path).resolve() if repo_path else _ROOT
    py_files = crawl(str(root))
    all_entities: list[dict] = []
    for fpath in py_files:
        all_entities.extend(parse_file(fpath, str(root)))
    all_entities = build_call_graph(all_entities)

    # git info
    def _git(*args: str) -> str:
        r = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, check=False
        )
        return r.stdout.strip() if r.returncode == 0 else "unknown"

    git_commit = _git("rev-parse", "HEAD")
    source_hash = hashlib.sha256(
        json.dumps(
            [e["id"] for e in sorted(all_entities, key=lambda e: e.get("id", ""))],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()[:12]
    repo_name = root.name.lower().replace(" ", "_")[:20]
    project_id = f"bench_{repo_name}_{source_hash}"

    # Index into Meilisearch
    create_index(project_id)
    index_documents(project_id, all_entities)

    catalog = catalog_from_entities(all_entities)
    catalog_by_id = {e["id"]: e for e in catalog}

    from collections import Counter
    symbol_counts: Counter[str] = Counter(
        e["name"]
        for e in all_entities
        if e.get("kind") in {"function", "method", "class"}
    )

    questions = []
    import random
    rng = random.Random(seed)

    # ── Discovery questions ───────────────────────────────────────────────────
    discovery_pool = [
        e for e in all_entities
        if e.get("kind") in {"function", "method"}
        and e.get("docstring", "").strip()
        and len(e.get("name", "")) > 3
        and not e.get("name", "").startswith("_")
        and symbol_counts[e["name"]] == 1
        and e.get("id") in catalog_by_id
    ]
    rng.shuffle(discovery_pool)
    for e in discovery_pool[:max(1, n_questions // 3)]:
        questions.append({
            "id": f"discovery-{len([q for q in questions if q['category']=='discovery'])+1:02d}",
            "category": "discovery",
            "prompt": (
                f"Where is `{e['name']}` defined and what is its purpose? "
                "Give the repository-relative file path."
            ),
            "expected_entities": [e["id"]],
            "expected_relationships": [],
            "expected_impact": [],
        })

    # ── Relationship questions ────────────────────────────────────────────────
    rel_pool = [
        e for e in all_entities
        if e.get("kind") in {"function", "method"}
        and e.get("name", "").isidentifier()
        and not e.get("name", "").startswith("_")
        and symbol_counts[e["name"]] == 1
        and e.get("id") in catalog_by_id
    ]
    rng.shuffle(rel_pool)
    for e in rel_pool:
        if len([q for q in questions if q["category"] == "relationship"]) >= max(1, n_questions // 3):
            break
        try:
            callers = derive_relationships(all_entities, e["id"])
        except ValueError:
            continue
        if not 1 <= len(callers) <= 6:
            continue
        questions.append({
            "id": f"relationship-{len([q for q in questions if q['category']=='relationship'])+1:02d}",
            "category": "relationship",
            "prompt": (
                f"Which functions or methods directly call `{e['name']}`? "
                "List each caller and its repository-relative file path."
            ),
            "expected_entities": [e["id"]],
            "expected_relationships": callers,
            "expected_impact": [],
        })

    # ── Impact questions ──────────────────────────────────────────────────────
    impact_pool = [
        e for e in all_entities
        if e.get("kind") in {"function", "method"}
        and not e.get("name", "").startswith("_")
        and symbol_counts[e["name"]] == 1
        and e.get("id") in catalog_by_id
    ]
    rng.shuffle(impact_pool)
    for e in impact_pool:
        if len([q for q in questions if q["category"] == "impact"]) >= max(1, n_questions // 3):
            break
        try:
            affected = derive_impact(all_entities, [e["id"]], max_depth=3)
        except ValueError:
            continue
        callers = [aid for aid in affected if aid != e["id"]]
        if not 1 <= len(callers) <= 10:
            continue
        questions.append({
            "id": f"impact-{len([q for q in questions if q['category']=='impact'])+1:02d}",
            "category": "impact",
            "prompt": (
                f"If `{e['name']}` changes, which functions will be affected "
                "(look up to 3 call levels deep)?"
            ),
            "expected_entities": [e["id"]],
            "expected_relationships": [],
            "expected_impact": callers,
        })

    if not questions:
        raise RuntimeError("No questions generated — check entity catalog")

    # ── Condition configs ─────────────────────────────────────────────────────
    n_files = len(py_files)
    size_bucket = "small" if n_files < 100 else "medium" if n_files < 500 else "large"

    adapter_settings_base = {
        "atlas_package": ATLAS_ROOT,
        "meili_url": MEILI_URL,
        "meili_key": MEILI_KEY,
        "project_id": project_id,
        "source_roots": ["."],
        "api_timeout_seconds": 180,
        "keep_alive": "10m",
        "api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
    }

    py = sys.executable
    manifest = {
        "schema_version": 1,
        "benchmark_id": f"atlas-{repo_name}-{source_hash}",
        "repository": {
            "name": root.name,
            "path": str(root),
            "git_commit": git_commit,
            "size_bucket": size_bucket,
            "working_tree_clean": not bool(_git("status", "--porcelain")),
        },
        "model": {
            "name": GEMMA_MODEL,
            "version": "ollama",
        },
        "entity_catalog": catalog,
        "questions": questions,
        "conditions": {
            "baseline": {
                "command": [py, str(_ROOT / "ollama_benchmark_agent.py")],
                "settings": {
                    **adapter_settings_base,
                    "tools": sorted(TOOL_TYPES - {"Atlas query"}),
                },
            },
            "atlas": {
                "command": [py, str(_ROOT / "ollama_benchmark_agent.py")],
                "settings": {
                    **adapter_settings_base,
                    "tools": sorted(TOOL_TYPES),
                },
            },
        },
    }
    return manifest, project_id


def _claude_manifest(base_manifest: dict) -> dict:
    """Clone manifest but point conditions at the Claude adapter."""
    import copy
    m = copy.deepcopy(base_manifest)
    m["model"] = {"name": CLAUDE_MODEL, "version": "anthropic"}
    m["benchmark_id"] = base_manifest["benchmark_id"].replace("atlas-self-", "atlas-self-claude-")
    py = sys.executable
    for cond in CONDITIONS:
        m["conditions"][cond]["command"] = [py, str(_ROOT / "claude_benchmark_agent.py")]
    return m


def _run_and_summarize(manifest: dict, repeats: int, timeout: float, seed: int) -> dict:
    from benchmarking import run_benchmark
    return run_benchmark(manifest, repeats=repeats, timeout=timeout, seed=seed)


def _render_comparison(gemma_result: dict, claude_result: dict) -> str:
    def pct(v):
        return "n/a" if v is None else f"{v * 100:.1f}%"

    def fmt(v, is_ratio=False):
        if v is None:
            return "n/a"
        return f"{v:.2f}" if not is_ratio else pct(v)

    g_base = gemma_result["summary"]["conditions"]["baseline"]
    g_atlas = gemma_result["summary"]["conditions"]["atlas"]
    c_base = claude_result["summary"]["conditions"]["baseline"]
    c_atlas = claude_result["summary"]["conditions"]["atlas"]

    rows = [
        ("Tool calls", "tool_calls", False),
        ("Total tokens", "total_tokens", False),
        ("Latency (s)", "latency_seconds", False),
        ("Entity accuracy", "entity_accuracy", True),
        ("Relationship F1", "relationship_f1", True),
        ("Impact F1", "impact_f1", True),
    ]

    lines = [
        "# Atlas self-benchmark results",
        "",
        f"Repository: `atlas` (self)  |  Questions: "
        f"{gemma_result['summary']['paired_observations']}  |  "
        f"Bootstrap seed/iters: 0 / 5000",
        "",
        "| Metric | Gemma 4e4b baseline | Gemma 4e4b + Atlas | "
        "Claude Sonnet baseline | Claude Sonnet + Atlas |",
        "|--------|--------------------|--------------------|"
        "----------------------|----------------------|",
    ]
    for label, key, is_pct in rows:
        gb = pct(g_base.get(key)) if is_pct else fmt(g_base.get(key))
        ga = pct(g_atlas.get(key)) if is_pct else fmt(g_atlas.get(key))
        cb = pct(c_base.get(key)) if is_pct else fmt(c_base.get(key))
        ca = pct(c_atlas.get(key)) if is_pct else fmt(c_atlas.get(key))
        lines.append(f"| {label} | {gb} | {ga} | {cb} | {ca} |")

    lines += [
        "",
        "## Effect of Atlas (Atlas − baseline)",
        "",
        "| Metric | Gemma Δ | Claude Δ |",
        "|--------|---------|----------|",
    ]
    g_fx = gemma_result["summary"]["effects"]
    c_fx = claude_result["summary"]["effects"]
    for label, key, is_pct in rows:
        gm = g_fx.get(key, {}).get("mean")
        cm = c_fx.get(key, {}).get("mean")
        gi = g_fx.get(key, {}).get("confidence_interval_95")
        ci = c_fx.get(key, {}).get("confidence_interval_95")
        g_ci = (
            f" [{gi[0]*100:+.1f}%, {gi[1]*100:+.1f}%]" if gi else ""
        ) if is_pct else (
            f" [{gi[0]:+.2f}, {gi[1]:+.2f}]" if gi else ""
        )
        c_ci = (
            f" [{ci[0]*100:+.1f}%, {ci[1]*100:+.1f}%]" if ci else ""
        ) if is_pct else (
            f" [{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else ""
        )
        gv = (f"{gm*100:+.1f}%" if gm is not None else "n/a") if is_pct else (f"{gm:+.2f}" if gm is not None else "n/a")
        cv = (f"{cm*100:+.1f}%" if cm is not None else "n/a") if is_pct else (f"{cm:+.2f}" if cm is not None else "n/a")
        lines.append(f"| {label} | {gv}{g_ci} | {cv}{c_ci} |")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Atlas 4-condition self-benchmark")
    parser.add_argument("--questions", type=int, default=12,
                        help="Approximate number of questions (default 12)")
    parser.add_argument("--repeats", type=int, default=1,
                        help="Repeats per question per condition (default 1)")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="Per-question timeout in seconds (default 120)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--repo", default=None,
                        help="Path to repo to benchmark (default: Atlas itself)")
    parser.add_argument("--out", default="self_benchmark_results.json",
                        help="Output JSON path")
    parser.add_argument("--skip-gemma", action="store_true",
                        help="Skip Gemma runs (Claude only)")
    parser.add_argument("--skip-claude", action="store_true",
                        help="Skip Claude runs (Gemma only)")
    args = parser.parse_args()

    repo_label = args.repo or "Atlas (self)"
    print(f"Building manifest from {repo_label}...", flush=True)
    gemma_manifest, project_id = _build_manifest(args.questions, args.seed, repo_path=args.repo)
    print(
        f"  {len(gemma_manifest['questions'])} questions, "
        f"project_id={project_id}",
        flush=True,
    )

    gemma_result = None
    claude_result = None

    if not args.skip_gemma:
        print(f"\nRunning Gemma ({GEMMA_MODEL}) — baseline + atlas...", flush=True)
        t0 = time.perf_counter()
        gemma_result = _run_and_summarize(
            gemma_manifest, args.repeats, args.timeout, args.seed
        )
        print(f"  Done in {time.perf_counter()-t0:.1f}s", flush=True)

    if not args.skip_claude:
        print(f"\nRunning Claude ({CLAUDE_MODEL}) — baseline + atlas...", flush=True)
        claude_manifest = _claude_manifest(gemma_manifest)
        t0 = time.perf_counter()
        claude_result = _run_and_summarize(
            claude_manifest, args.repeats, args.timeout, args.seed
        )
        print(f"  Done in {time.perf_counter()-t0:.1f}s", flush=True)

    # Save results
    combined = {
        "gemma": gemma_result,
        "claude": claude_result,
        "project_id": project_id,
        "questions": gemma_manifest["questions"],
    }
    out_path = Path(args.out)
    out_path.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nResults saved to {out_path}", flush=True)

    # Print report
    if gemma_result and claude_result:
        print("\n" + _render_comparison(gemma_result, claude_result))
    elif gemma_result:
        from benchmarking import render_report
        print("\n" + render_report(gemma_result))
    elif claude_result:
        from benchmarking import render_report
        print("\n" + render_report(claude_result))


if __name__ == "__main__":
    main()
