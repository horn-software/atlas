"""Reproducible, adapter-driven Atlas benchmark runner and report generator."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import random
import re
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
CATEGORIES = {"discovery", "relationship", "impact", "cross_language"}
TOOL_TYPES = {"Read", "Search", "Grep", "Glob", "Atlas query"}
CONDITIONS = ("baseline", "atlas")
BASELINE_TOOL_TYPES = TOOL_TYPES - {"Atlas query"}


def catalog_from_entities(entities: list[dict]) -> list[dict]:
    """Build a stable, explicit entity catalog from Atlas parser documents."""
    catalog = []
    for entity in entities:
        entity_id = entity.get("id")
        name = entity.get("name")
        kind = entity.get("kind")
        if entity_id and name and kind:
            catalog.append({
                "id": str(entity_id),
                "name": str(name),
                "kind": str(kind),
                "file_path": str(entity.get("file_path", "")),
            })
    return sorted(catalog, key=lambda item: item["id"])


def derive_relationships(entities: list[dict], target_entity_id: str) -> list[str]:
    """Return entity IDs whose direct call edge targets the given function or method."""
    targets = {entity.get("id"): entity for entity in entities}
    target = targets.get(target_entity_id)
    if not target or target.get("kind") not in {"function", "method"}:
        raise ValueError(f"unknown function or method entity id: {target_entity_id}")
    return sorted({
        entity["id"]
        for entity in entities
        if entity.get("id")
        and entity.get("kind") in {"function", "method"}
        and target["name"] in entity.get("calls", [])
    })


def derive_bridge_relationships(entities: list[dict], **criteria: str) -> list[str]:
    """Select Atlas bridge entity IDs using exact indexed bridge fields."""
    allowed = {"bridge_type", "route_path", "target_function", "variable_name", "name"}
    unknown = set(criteria) - allowed
    if unknown:
        raise ValueError(f"unsupported bridge criteria: {sorted(unknown)}")
    if not criteria:
        raise ValueError("at least one bridge criterion is required")
    return sorted(
        entity["id"]
        for entity in entities
        if entity.get("kind") == "bridge"
        and entity.get("id")
        and all(entity.get(field) == value for field, value in criteria.items())
    )


def derive_impact(
    entities: list[dict],
    target_entity_ids: list[str],
    max_depth: int = 3,
) -> list[str]:
    """Traverse caller edges to produce a bounded, function-only impact set."""
    if max_depth < 0:
        raise ValueError("max_depth cannot be negative")
    by_id = {entity.get("id"): entity for entity in entities if entity.get("id")}
    unknown = set(target_entity_ids) - by_id.keys()
    if unknown:
        raise ValueError(f"unknown impact target entity ids: {sorted(unknown)}")
    name_to_ids: dict[str, list[str]] = defaultdict(list)
    qualified_to_ids: dict[str, list[str]] = defaultdict(list)
    for entity_id, entity in by_id.items():
        if entity.get("kind") in {"function", "method"}:
            name_to_ids[entity.get("name", "")].append(entity_id)
            qualified_name = entity.get("qualified_name")
            if qualified_name:
                qualified_to_ids[qualified_name].append(entity_id)

    depths = {entity_id: 0 for entity_id in target_entity_ids}
    frontier = list(target_entity_ids)
    while frontier:
        current_id = frontier.pop(0)
        depth = depths[current_id]
        if depth >= max_depth:
            continue
        current = by_id[current_id]
        for caller_name in current.get("called_by", []):
            caller_ids = qualified_to_ids.get(caller_name, [])
            if not caller_ids:
                exact_name_ids = name_to_ids.get(caller_name, [])
                caller_ids = exact_name_ids if len(exact_name_ids) == 1 else []
            for caller_id in caller_ids:
                if caller_id not in depths:
                    depths[caller_id] = depth + 1
                    frontier.append(caller_id)
    return sorted(depths)


def validate_manifest(manifest: dict) -> None:
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
    if not isinstance(manifest.get("benchmark_id"), str) or not manifest["benchmark_id"]:
        raise ValueError("benchmark_id is required")
    repository = manifest.get("repository", {})
    if not isinstance(repository, dict):
        raise ValueError("repository must be an object")
    if repository.get("size_bucket") not in {"small", "medium", "large"}:
        raise ValueError("repository.size_bucket must be small, medium, or large")
    if not all(
        isinstance(repository.get(field), str) and repository[field]
        for field in ("name", "path", "git_commit")
    ):
        raise ValueError("repository.name, path, and git_commit are required")
    model = manifest.get("model", {})
    if not isinstance(model, dict):
        raise ValueError("model must be an object")
    if not all(
        isinstance(model.get(field), str) and model[field]
        for field in ("name", "version")
    ):
        raise ValueError("model.name and model.version are required")

    catalog = manifest.get("entity_catalog")
    if not isinstance(catalog, list):
        raise ValueError("entity_catalog must be a list")
    catalog_ids = set()
    for entity in catalog:
        if not isinstance(entity, dict):
            raise ValueError("each catalog entity must be an object")
        if not all(
            isinstance(entity.get(field), str) and entity[field]
            for field in ("id", "name", "kind")
        ):
            raise ValueError("each catalog entity requires id, name, and kind")
        if entity["id"] in catalog_ids:
            raise ValueError(f"duplicate entity id: {entity['id']}")
        catalog_ids.add(entity["id"])

    questions = manifest.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("questions must be a non-empty list")
    seen_questions = set()
    for question in questions:
        if not isinstance(question, dict):
            raise ValueError("each question must be an object")
        if not all(
            isinstance(question.get(field), str) and question[field]
            for field in ("id", "category", "prompt")
        ):
            raise ValueError("each question requires id, category, and prompt")
        if question["category"] not in CATEGORIES:
            raise ValueError(f"unsupported question category: {question['category']}")
        if question["id"] in seen_questions:
            raise ValueError(f"duplicate question id: {question['id']}")
        seen_questions.add(question["id"])
        for field in ("expected_entities", "expected_relationships", "expected_impact"):
            values = question.get(field, [])
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError(f"{question['id']}.{field} must be a list of entity ids")
            unknown = set(question.get(field, [])) - catalog_ids
            if unknown:
                raise ValueError(
                    f"{question['id']}.{field} references unknown entity ids: "
                    f"{sorted(unknown)}"
                )
    conditions = manifest.get("conditions")
    if not isinstance(conditions, dict):
        raise ValueError("conditions must be an object")
    for condition in CONDITIONS:
        config = conditions.get(condition, {})
        if not isinstance(config, dict):
            raise ValueError(f"conditions.{condition} must be an object")
        command = config.get("command")
        if not isinstance(command, list) or not command or any(
            not isinstance(argument, str) or not argument for argument in command
        ):
            raise ValueError(f"conditions.{condition}.command must be a non-empty argv list")
        settings = config.get("settings")
        tools = settings.get("tools") if isinstance(settings, dict) else None
        expected_tools = BASELINE_TOOL_TYPES if condition == "baseline" else TOOL_TYPES
        if (
            not isinstance(tools, list)
            or any(not isinstance(tool, str) for tool in tools)
            or len(tools) != len(set(tools))
            or set(tools) != expected_tools
        ):
            raise ValueError(
                f"conditions.{condition}.settings.tools must list exactly "
                f"{sorted(expected_tools)}"
            )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_agent(
    condition: str,
    config: dict,
    question: dict,
    manifest: dict,
    timeout: float,
) -> dict:
    repository = Path(manifest["repository"]["path"]).resolve()
    if not repository.is_dir():
        raise ValueError(f"repository path does not exist: {repository}")
    request = {
        "schema_version": SCHEMA_VERSION,
        "condition": condition,
        "question": {
            key: question[key]
            for key in ("id", "category", "prompt")
        },
        "repository": {
            **manifest["repository"],
            "path": str(repository),
        },
        "model": manifest["model"],
        "adapter_config": config.get("settings", {}),
    }
    started_at = _utc_now()
    start = time.perf_counter()
    try:
        process = subprocess.run(
            config["command"],
            input=json.dumps(request, ensure_ascii=False),
            text=True,
            capture_output=True,
            cwd=repository,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{condition} adapter timed out after {timeout:g}s") from exc
    duration = time.perf_counter() - start
    ended_at = _utc_now()
    if process.returncode != 0:
        details = process.stderr.strip()[-2000:]
        raise RuntimeError(
            f"{condition} adapter exited with status {process.returncode}: {details}"
        )
    try:
        response = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{condition} adapter stdout must be one JSON object") from exc
    if not isinstance(response, dict) or not isinstance(response.get("answer"), str):
        raise ValueError(f"{condition} adapter response requires an answer string")
    for field in ("input_tokens", "output_tokens"):
        if (
            not isinstance(response.get(field), int)
            or isinstance(response[field], bool)
            or response[field] < 0
        ):
            raise ValueError(f"{condition} adapter response requires non-negative {field}")
    calls = response.get("tool_calls")
    if not isinstance(calls, dict) or any(
        key not in TOOL_TYPES
        or not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        for key, value in calls.items()
    ):
        raise ValueError(
            f"{condition} tool_calls must map known tool names to non-negative integers"
        )
    for field in ("entity_mentions", "relationship_entities", "impact_entities"):
        if field != "entity_mentions" and field not in response:
            raise ValueError(f"{condition} response field {field} is required")
        if field in response and (not isinstance(response[field], list) or any(
            not isinstance(value, str) for value in response[field]
        )):
            raise ValueError(f"{condition} response field {field} must be a list of strings")
    return {
        "answer": response["answer"],
        "input_tokens": response["input_tokens"],
        "output_tokens": response["output_tokens"],
        "tool_calls": {tool: response.get("tool_calls", {}).get(tool, 0) for tool in sorted(TOOL_TYPES)},
        "entity_mentions": re.findall(r"`([^`\r\n]+)`", response["answer"]),
        "response_format": response.get("response_format", "json"),
        "relationship_entities": response.get("relationship_entities", []),
        "impact_entities": response.get("impact_entities", []),
        "started_at": started_at,
        "ended_at": ended_at,
        "latency_seconds": duration,
    }


def _f1(expected: list[str], predicted: list[str]) -> dict:
    truth, answer = set(expected), set(predicted)
    tp = len(truth & answer)
    precision = tp / len(answer) if answer else (1.0 if not truth else 0.0)
    recall = tp / len(truth) if truth else (1.0 if not answer else 0.0)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def _resolve_entity_references(references: list[str], catalog: list[dict]) -> list[str]:
    by_id = {entity["id"] for entity in catalog}
    by_name = defaultdict(list)
    by_path = defaultdict(list)
    for entity in catalog:
        by_name[entity["name"].casefold()].append(entity["id"])
        if entity.get("kind") == "file" and entity.get("file_path"):
            by_path[entity["file_path"].replace("\\", "/").casefold()].append(entity["id"])

    resolved = []
    for reference in references:
        if reference in by_id:
            resolved.append(reference)
            continue
        name, separator, file_path = reference.partition("@")
        name = name.strip().strip("`")
        name = re.sub(r"\s*\([^()]*\)\s*$", "", name).strip()
        if separator:
            wanted_path = file_path.strip().replace("\\", "/").casefold()
            if name.replace("\\", "/").casefold() == wanted_path:
                matches = by_path.get(wanted_path, [])
            else:
                matches = [
                    entity["id"] for entity in catalog
                    if entity["name"].casefold() == name.casefold()
                    and entity.get("file_path", "").replace("\\", "/").casefold() == wanted_path
                ]
        else:
            matches = by_name.get(name.casefold(), [])
            if not matches:
                matches = by_path.get(name.replace("\\", "/").casefold(), [])
        resolved.extend(matches or [f"unresolved:{reference}"])
    return sorted(set(resolved))


def _answer_entity_references(answer: str) -> list[str]:
    return [
        f"{match.group('name')}@{match.group('path')}"
        for match in re.finditer(
            r"`(?P<name>[^`\r\n]+)`\s+(?:in|at|from)\s+`(?P<path>[^`\r\n]+)`",
            answer,
        )
    ]


def score_observation(question: dict, run: dict, catalog: list[dict]) -> dict:
    mentions = run["entity_mentions"]
    resolved_mentions = _resolve_entity_references(mentions, catalog)
    valid_ids = {entity["id"] for entity in catalog}
    correct = sum(
        any(entity_id in valid_ids for entity_id in _resolve_entity_references([mention], catalog))
        for mention in mentions
    )
    run["entity_accuracy"] = correct / len(mentions) if mentions else None
    run["entity_mention_ids"] = [
        entity_id for entity_id in resolved_mentions if entity_id in valid_ids
    ]
    answer_predictions = _answer_entity_references(run["answer"])
    if question["category"] in {"relationship", "cross_language"}:
        run["relationship"] = _f1(
            question.get("expected_relationships", []),
            _resolve_entity_references(
                run["relationship_entities"] + answer_predictions, catalog
            ),
        )
    else:
        run["relationship"] = {"precision": 0.0, "recall": 0.0, "f1": None}
    if question["category"] == "impact":
        run["impact"] = _f1(
            question.get("expected_impact", []),
            _resolve_entity_references(run["impact_entities"] + answer_predictions, catalog),
        )
    else:
        run["impact"] = {"precision": 0.0, "recall": 0.0, "f1": None}
    return run


def _bootstrap_paired_interval(
    pairs: list[tuple[float, float]],
    metric: str,
    seed: int,
    iterations: int = 5000,
) -> list[float] | None:
    if len(pairs) < 2:
        return None
    rng = random.Random(seed)
    samples = []
    for _ in range(iterations):
        selected = [rng.choice(pairs) for _ in pairs]
        baseline = statistics.fmean(pair[0] for pair in selected)
        atlas = statistics.fmean(pair[1] for pair in selected)
        if metric in {"tool_calls", "total_tokens", "latency_seconds"}:
            if baseline == 0 and atlas != 0:
                return None
            samples.append((baseline - atlas) / baseline if baseline else 0.0)
        else:
            samples.append(atlas - baseline)
    samples.sort()
    return [samples[int(0.025 * iterations)], samples[min(iterations - 1, int(0.975 * iterations))]]


def _mean(records: list[dict], selector) -> float:
    values = [selector(record) for record in records]
    values = [value for value in values if value is not None]
    return statistics.fmean(values) if values else None


def summarize(observations: list[dict], seed: int) -> dict:
    grouped = defaultdict(lambda: defaultdict(list))
    for item in observations:
        grouped[item["question_id"]][item["condition"]].append(item)

    metric_selectors = {
        "tool_calls": lambda run: sum(run["tool_calls"].values()),
        "total_tokens": lambda run: run["input_tokens"] + run["output_tokens"],
        "latency_seconds": lambda run: run["latency_seconds"],
        "entity_accuracy": lambda run: run["entity_accuracy"],
        "relationship_f1": lambda run: run["relationship"]["f1"],
        "impact_f1": lambda run: run["impact"]["f1"],
    }
    result = {"conditions": {}, "effects": {}}
    for condition in CONDITIONS:
        records = [item for item in observations if item["condition"] == condition]
        result["conditions"][condition] = {
            metric: _mean(records, selector) if records else None
            for metric, selector in metric_selectors.items()
        }

    for metric, selector in metric_selectors.items():
        question_pairs = []
        for question_id in sorted(grouped):
            baseline = grouped[question_id]["baseline"]
            atlas = grouped[question_id]["atlas"]
            per_question_pairs = []
            for base_run, atlas_run in zip(baseline, atlas):
                base, treatment = selector(base_run), selector(atlas_run)
                if base is None or treatment is None:
                    continue
                per_question_pairs.append((base, treatment))
            if per_question_pairs:
                question_pairs.append((
                    statistics.fmean(pair[0] for pair in per_question_pairs),
                    statistics.fmean(pair[1] for pair in per_question_pairs),
                ))
        if question_pairs:
            baseline_mean = statistics.fmean(pair[0] for pair in question_pairs)
            atlas_mean = statistics.fmean(pair[1] for pair in question_pairs)
            if metric in {"tool_calls", "total_tokens", "latency_seconds"}:
                effect_mean = (
                    (baseline_mean - atlas_mean) / baseline_mean
                    if baseline_mean
                    else (0.0 if atlas_mean == 0 else None)
                )
            else:
                effect_mean = atlas_mean - baseline_mean
        else:
            effect_mean = None
        result["effects"][metric] = {
            "mean": effect_mean,
            "confidence_interval_95": _bootstrap_paired_interval(question_pairs, metric, seed),
            "interpretation": (
                "relative_reduction"
                if metric in {"tool_calls", "total_tokens", "latency_seconds"}
                else "absolute_gain"
            ),
        }
    result["paired_observations"] = sum(
        min(len(pair["baseline"]), len(pair["atlas"])) for pair in grouped.values()
    )
    return result


def run_benchmark(manifest: dict, repeats: int = 1, timeout: float = 300, seed: int = 0) -> dict:
    validate_manifest(manifest)
    if (
        not isinstance(repeats, int)
        or isinstance(repeats, bool)
        or repeats < 1
        or not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or timeout <= 0
        or not isinstance(seed, int)
        or isinstance(seed, bool)
    ):
        raise ValueError("repeats and seed must be integers; timeout must be finite and positive")
    observations = []
    rng = random.Random(seed)
    for repeat in range(repeats):
        # Alternate condition order to reduce systematic time/order effects.
        question_order = list(manifest["questions"])
        rng.shuffle(question_order)
        for question_index, question in enumerate(question_order):
            order = list(CONDITIONS)
            if (repeat + question_index + seed) % 2:
                order.reverse()
            for condition in order:
                run = _run_agent(
                    condition,
                    manifest["conditions"][condition],
                    question,
                    manifest,
                    timeout,
                )
                score_observation(question, run, manifest["entity_catalog"])
                observations.append({
                    "question_id": question["id"],
                    "category": question["category"],
                    "repeat": repeat,
                    "condition": condition,
                    **run,
                })
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark_id": manifest["benchmark_id"],
        "created_at": _utc_now(),
        "manifest_sha256": hashlib.sha256(
            json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
        "entity_catalog_sha256": hashlib.sha256(
            json.dumps(
                manifest["entity_catalog"], sort_keys=True, ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest(),
        "questions": [
            {
                "id": question["id"],
                "category": question["category"],
                "prompt": question["prompt"],
                "expected_entities": question.get("expected_entities", []),
                "expected_relationships": question.get("expected_relationships", []),
                "expected_impact": question.get("expected_impact", []),
            }
            for question in manifest["questions"]
        ],
        "reproducibility": {
            "repository": manifest["repository"],
            "model": manifest["model"],
            "tool_policies": {
                condition: manifest["conditions"][condition]["settings"]["tools"]
                for condition in CONDITIONS
            },
            "repeats": repeats,
            "bootstrap_seed": seed,
            "bootstrap_iterations": 5000,
            "adapter_versions": manifest.get("adapter_versions", {}),
        },
        "summary": summarize(observations, seed),
        "observations": observations,
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def render_report(results: dict, *, illustrative: bool = False) -> str:
    summary = results["summary"]
    baseline = summary["conditions"]["baseline"]
    atlas = summary["conditions"]["atlas"]
    labels = {
        "tool_calls": "Average tool calls",
        "total_tokens": "Average total tokens",
        "latency_seconds": "Average latency (s)",
        "entity_accuracy": "Entity accuracy",
        "relationship_f1": "Relationship F1",
        "impact_f1": "Impact F1",
    }
    lines = [
        f"# Atlas benchmark: {results['benchmark_id']}",
        "",
        "> **Illustrative dry run only; not empirical evidence.**" if illustrative
        else "> Results are valid only for the repository, model, adapters, and settings recorded below.",
        "",
        f"- Repository: `{results['reproducibility']['repository'].get('name', 'unnamed')}` "
        f"({results['reproducibility']['repository'].get('size_bucket', 'unknown')})",
        f"- Model: `{results['reproducibility']['model'].get('name')}`",
        f"- Paired observations: {summary['paired_observations']}",
        f"- Repeats per question: {results['reproducibility']['repeats']}",
        f"- Bootstrap seed / iterations: {results['reproducibility']['bootstrap_seed']} / "
        f"{results['reproducibility']['bootstrap_iterations']}",
        "",
        "| Metric | Baseline | Atlas | Improvement | 95% paired bootstrap CI |",
        "|---|---:|---:|---:|---:|",
    ]
    for key, label in labels.items():
        base_value, atlas_value = baseline.get(key), atlas.get(key)
        effect = summary["effects"][key]
        interval = effect["confidence_interval_95"]
        if interval is None:
            ci = "not estimable (<2 questions)"
        elif effect["interpretation"] == "relative_reduction":
            ci = f"[{_pct(interval[0])}, {_pct(interval[1])}]"
        else:
            ci = f"[{interval[0] * 100:+.1f} pp, {interval[1] * 100:+.1f} pp]"
        if effect["interpretation"] == "relative_reduction":
            improvement = _pct(effect["mean"])
        else:
            improvement = "n/a" if effect["mean"] is None else f"{effect['mean'] * 100:+.1f} pp"
        formatter = (lambda value: "n/a" if value is None else f"{value:.2f}") if key in {
            "tool_calls", "latency_seconds"
        } else (lambda value: "n/a" if value is None else f"{value:.0f}")
        if key.endswith("_accuracy") or key.endswith("_f1"):
            formatter = _pct
        lines.append(
            f"| {label} | {formatter(base_value)} | {formatter(atlas_value)} | "
            f"{improvement} | {ci} |"
        )
    lines.extend([
        "",
        "## Interpretation notes",
        "",
        "- Improvement is a relative reduction for calls, tokens, and latency; accuracy and F1 "
        "are Atlas-minus-baseline percentage-point gains.",
        "- Confidence intervals resample paired question-level means with replacement using "
        "the recorded seed. Relative reductions are recomputed from the sampled condition means; "
        "questions, not tool calls, are the experimental units.",
        "- Entity mentions are extracted deterministically from inline-code spans and resolved "
        "against the recorded entity catalog; this is not a general-purpose named-entity recognizer.",
        f"- Manifest SHA-256: `{results['manifest_sha256']}`.",
        "- Relationship and impact scores use the explicit, versioned ground truth in the "
        "benchmark manifest. They are not inferred from model-generated answers.",
        "- One run or a single question cannot establish generalizable causal claims. Report "
        "all repositories, questions, exclusions, model settings, and adapter versions.",
        "",
    ])
    return "\n".join(lines)


def write_results(results: dict, output_dir: Path, *, illustrative: bool = False) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "report.md").write_text(
        render_report(results, illustrative=illustrative),
        encoding="utf-8",
    )


def _example_results() -> dict:
    """Create deterministic synthetic observations for a clearly labeled dry run."""
    observations = []
    questions = []
    for index in range(8):
        qid = f"example-{index + 1:02d}"
        category = ("discovery", "relationship", "impact", "cross_language")[index % 4]
        target_id, caller_id = f"target-{qid}", f"caller-{qid}"
        questions.append({
            "id": qid,
            "category": category,
            "prompt": "synthetic question; no repository was queried",
            "expected_entities": [target_id],
            "expected_relationships": [caller_id],
            "expected_impact": [target_id, caller_id],
        })
        for condition in CONDITIONS:
            is_atlas = condition == "atlas"
            calls = 2 if is_atlas else 8
            tokens = 700 if is_atlas else 2200
            latency = 1.2 if is_atlas else 6.4
            mentions = [target_id] if is_atlas else [target_id, "not-in-catalog"]
            relationship_ids = [caller_id] if is_atlas else []
            impact_ids = [target_id, caller_id] if is_atlas else [target_id]
            entity_accuracy = 1.0 if is_atlas else 0.5
            relationship = _f1([caller_id], relationship_ids)
            impact = _f1([target_id, caller_id], impact_ids)
            observations.append({
                "question_id": qid,
                "category": category,
                "repeat": 0,
                "condition": condition,
                "answer": "synthetic example; no model was called",
                "response_format": "synthetic",
                "input_tokens": tokens - 100,
                "output_tokens": 100,
                "tool_calls": {tool: 0 for tool in sorted(TOOL_TYPES)},
                "entity_mentions": mentions,
                "entity_mention_ids": [],
                "relationship_entities": relationship_ids,
                "impact_entities": impact_ids,
                "started_at": "2026-01-01T00:00:00+00:00",
                "ended_at": "2026-01-01T00:00:01+00:00",
                "latency_seconds": latency,
                "entity_accuracy": entity_accuracy,
                "relationship": relationship,
                "impact": impact,
            } | {"tool_calls": {**{tool: 0 for tool in TOOL_TYPES}, "Search": calls}})
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark_id": "illustrative-synthetic-example",
        "created_at": "2026-01-01T00:00:00+00:00",
        "manifest_sha256": hashlib.sha256(b"illustrative-synthetic-example").hexdigest(),
        "entity_catalog_sha256": hashlib.sha256(b"illustrative-entity-catalog").hexdigest(),
        "questions": questions,
        "reproducibility": {
            "repository": {"name": "synthetic-fixture", "size_bucket": "small"},
            "model": {"name": "none; synthetic data"},
            "tool_policies": {
                "baseline": sorted(BASELINE_TOOL_TYPES),
                "atlas": sorted(TOOL_TYPES),
            },
            "repeats": 1,
            "bootstrap_seed": 17,
            "bootstrap_iterations": 5000,
            "adapter_versions": {},
        },
        "summary": summarize(observations, 17),
        "observations": observations,
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="atlas-benchmark")
    subparsers = parser.add_subparsers(dest="action", required=True)
    run_parser = subparsers.add_parser("run", help="run paired baseline and Atlas adapters")
    run_parser.add_argument("--manifest", required=True, type=Path)
    run_parser.add_argument("--output-dir", required=True, type=Path)
    run_parser.add_argument("--repeats", type=int, default=1)
    run_parser.add_argument("--timeout", type=float, default=300)
    run_parser.add_argument("--seed", type=int, default=0)
    example_parser = subparsers.add_parser("example", help="write synthetic, non-empirical example files")
    example_parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    try:
        if args.action == "example":
            write_results(_example_results(), args.output_dir, illustrative=True)
        else:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            results = run_benchmark(manifest, args.repeats, args.timeout, args.seed)
            write_results(results, args.output_dir)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        parser.exit(2, f"atlas-benchmark: error: {exc}\n")
    print(f"Wrote results.json and report.md to {args.output_dir}")


if __name__ == "__main__":
    main()
