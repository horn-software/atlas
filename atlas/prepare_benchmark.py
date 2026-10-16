"""Build an isolated Atlas index and a pinned benchmark manifest for a workspace."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.error import URLError
from urllib.request import urlopen

_ATLAS_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ATLAS_ROOT))

from benchmarking import (  # noqa: E402
    catalog_from_entities,
    derive_impact,
    derive_relationships,
    validate_manifest,
)


def _git_info(directory: Path) -> dict:
    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(directory), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"Git could not inspect {directory}: {result.stderr.strip()}")
        return result.stdout.strip()

    return {
        "name": directory.name,
        "path": directory.name,
        "git_commit": git("rev-parse", "HEAD"),
        "working_tree_clean": not bool(git("status", "--porcelain")),
    }


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _digest_files(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        relative = path.relative_to(_ATLAS_ROOT).as_posix()
        digest.update(relative.encode("utf-8") + b"\0" + _file_digest(path).encode("ascii") + b"\n")
    return digest.hexdigest()


def _ollama_digest(model_name: str) -> str:
    try:
        with urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as response:
            tags = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"local Ollama is not available: {exc}") from exc
    for model in tags.get("models", []):
        if model.get("name") == model_name or model.get("model") == model_name:
            return model.get("digest", "digest-unavailable")
    raise ValueError(f"model {model_name!r} is not installed in local Ollama")


def _source_entities(root: Path, source_roots: list[str]) -> tuple[list[str], list[dict], dict]:
    from ingest.call_graph import build_call_graph
    from ingest.crawler import crawl
    from ingest.parsers import parse_file

    source_files = [
        Path(path)
        for path in crawl(str(root))
        if Path(path).relative_to(root).parts[0] in source_roots
    ]
    if not source_files:
        raise ValueError("no Atlas-supported source files found under the selected repositories")

    entities = []
    loc = 0
    per_repo_hashes: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for path in source_files:
        relative = path.relative_to(root).as_posix()
        repository = Path(relative).parts[0]
        loc += len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        per_repo_hashes[repository].append((relative, _file_digest(path)))
        entities.extend(parse_file(str(path), str(root)))
    entities = build_call_graph(entities)

    repository_hashes = {}
    combined = hashlib.sha256()
    for name, entries in sorted(per_repo_hashes.items()):
        repo_hash = hashlib.sha256()
        for relative, file_hash in sorted(entries):
            record = f"{relative}\0{file_hash}\n".encode("utf-8")
            repo_hash.update(record)
            combined.update(record)
        repository_hashes[name] = repo_hash.hexdigest()
    metadata = {
        "source_file_count": len(source_files),
        "loc": loc,
        "source_tree_sha256": combined.hexdigest(),
        "subrepository_source_sha256": repository_hashes,
    }
    return [str(path) for path in source_files], entities, metadata


def _cross_language_questions(entities: list[dict], bridges: list[dict]) -> list[dict]:
    file_entities = {
        entity["file_path"]: entity
        for entity in entities
        if entity.get("kind") == "file" and entity.get("file_path")
    }
    for entity in entities:
        if entity.get("file_path") and entity.get("kind") in {"function", "method", "class"}:
            file_entities.setdefault(entity["file_path"], entity)

    seen = set()
    candidates = []
    safe_bridge_names = {
        "ENV:PAYSTACK_CALLBACK_URL",
        "ENV:FIREBASE_SERVICE_ACCOUNT_JSON",
        "CONFIG:manifest.json",
        "TABLE:firebase",
    }
    for bridge in bridges:
        if bridge.get("name") not in safe_bridge_names:
            continue
        paths = []
        uses = bridge.get("uses") or []
        if uses:
            for use in uses:
                path = use.get("file")
                if path in file_entities:
                    paths.append((path, use.get("language", "")))
        else:
            for path in bridge.get("files", []):
                if path in file_entities:
                    paths.append((path, file_entities[path].get("language", "")))

        unique_paths = sorted(set(paths))
        for left_index, (left_path, left_language) in enumerate(unique_paths):
            for right_path, right_language in unique_paths[left_index + 1:]:
                if not left_language or not right_language or left_language == right_language:
                    continue
                left, right = file_entities[left_path], file_entities[right_path]
                key = (bridge["id"], *sorted((left["id"], right["id"])))
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    (bridge, left, right, left_language, right_language, left_path, right_path)
                )

    questions = []
    for bridge, left, right, left_language, right_language, left_path, right_path in candidates:
        if len(questions) == 10:
            break
        question_id = f"cross-language-{len(questions) + 1:02d}"
        label = bridge["name"].replace("ENV:", "").replace("CONFIG:", "").replace("TABLE:", "")
        if bridge.get("bridge_type") == "db_schema":
            label = "Firebase data-store"
        questions.append({
            "id": question_id,
            "category": "cross_language",
            "prompt": (
                f"Which {left_language} file `{left_path}` and {right_language} file "
                f"`{right_path}` are linked through the shared {label} integration? "
                "Confirm both source files."
            ),
            "expected_entities": sorted({bridge["id"], left["id"], right["id"]}),
            "expected_relationships": sorted({left["id"], right["id"]}),
            "expected_impact": [],
        })
    if len(questions) < 10:
        raise ValueError(
            f"only {len(questions)} safe, indexed cross-language relationships were found; "
            "review bridge coverage instead of fabricating ground truth"
        )
    return questions


def build_manifest(
    root: Path,
    output_dir: Path,
    atlas_package: Path,
    meili_url: str,
    model_name: str,
    source_roots: list[str],
    force: bool = False,
) -> dict:
    root = root.resolve()
    output_dir = output_dir.resolve()
    atlas_package = atlas_package.resolve()
    if not root.is_dir() or not atlas_package.is_dir():
        raise ValueError("workspace and Atlas package paths must be existing directories")
    meili = urlsplit(meili_url)
    if meili.scheme != "http" or meili.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("benchmark Meilisearch URL must use loopback HTTP")
    resolved_source_roots = []
    for name in source_roots:
        repo_path = (root / name).resolve()
        if not repo_path.is_relative_to(root) or not repo_path.is_dir():
            raise ValueError(f"selected repository does not exist under workspace: {name}")
        resolved_source_roots.append(repo_path)
    if any(output_dir.is_relative_to(repo) for repo in resolved_source_roots):
        raise ValueError("benchmark output directory must be outside the selected Git repositories")
    generated_files = (output_dir / "manifest.json", output_dir / "setup-summary.json")
    if any(path.exists() for path in generated_files) and not force:
        raise FileExistsError(
            f"benchmark manifest already exists in: {output_dir}; use --force to regenerate it"
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    os.environ["MEILI_URL"] = meili_url
    os.environ["MEILI_KEY"] = ""
    from ingest.bridge_resolver import resolve_all_bridges
    from ingest.indexer import create_index, index_documents

    allowed_files, entities, source_metadata = _source_entities(root, source_roots)
    repo_metadata = [_git_info(directory) for directory in resolved_source_roots]
    atlas_source_paths = [
        _ATLAS_ROOT / relative
        for relative in (
            "ingest/crawler.py",
            "ingest/parsers.py",
            "ingest/call_graph.py",
            "ingest/bridge_resolver.py",
            "ingest/indexer.py",
            "query/synonyms.py",
            "edge/impact.py",
        )
    ]
    atlas_source_hash = _digest_files(atlas_source_paths)
    runner_hash = _file_digest(_ATLAS_ROOT / "benchmarking.py")
    adapter_hash = _file_digest(_ATLAS_ROOT / "ollama_benchmark_agent.py")
    generator_hash = _file_digest(_ATLAS_ROOT / "prepare_benchmark.py")
    project_id = (
        f"jgbc_bench_{source_metadata['source_tree_sha256'][:8]}_{atlas_source_hash[:8]}"
    )
    bridges = resolve_all_bridges(entities, project_id)
    index_name = create_index(project_id)
    indexed_entities = index_documents(project_id, entities + bridges)
    catalog = catalog_from_entities(entities + bridges)
    symbol_counts = Counter(
        entity["name"]
        for entity in entities
        if entity.get("kind") in {"function", "method", "class"}
    )

    questions = []
    discovered = [
        entity for entity in entities
        if entity.get("kind") in {"function", "method", "class"}
        and entity.get("docstring", "").strip()
        and len(entity.get("name", "")) > 2
        and not entity.get("name", "").startswith("__")
        and symbol_counts[entity["name"]] == 1
    ]
    for target in discovered:
        if len([q for q in questions if q["category"] == "discovery"]) >= 15:
            break
        questions.append({
            "id": f"discovery-{sum(q['category'] == 'discovery' for q in questions) + 1:02d}",
            "category": "discovery",
            "prompt": (
                f"Where is `{target['name']}` defined, and what is its documented purpose? "
                "Give the repository-relative file path."
            ),
            "expected_entities": [target["id"]],
            "expected_relationships": [],
            "expected_impact": [],
        })

    seen_relationship_targets = set()
    relationship_candidates = sorted(
        (
            entity for entity in entities
            if entity.get("kind") in {"function", "method"}
            and entity.get("name", "").isidentifier()
            and not entity.get("name", "").startswith("__")
            and symbol_counts[entity["name"]] == 1
            and entity.get("name") not in seen_relationship_targets
        ),
        key=lambda entity: (entity["name"].casefold(), entity.get("file_path", "")),
    )
    for target in relationship_candidates:
        callers = derive_relationships(entities, target["id"])
        if not 1 <= len(callers) <= 8:
            continue
        if target["name"] in seen_relationship_targets:
            continue
        seen_relationship_targets.add(target["name"])
        questions.append({
            "id": f"relationship-{sum(q['category'] == 'relationship' for q in questions) + 1:02d}",
            "category": "relationship",
            "prompt": (
                f"Which functions or methods directly call `{target['name']}`? "
                "List each caller and its repository-relative file path."
            ),
            "expected_entities": [target["id"]],
            "expected_relationships": callers,
            "expected_impact": [],
        })
        if sum(q["category"] == "relationship" for q in questions) == 15:
            break

    impact_targets = sorted(
        (
            entity for entity in entities
            if entity.get("kind") in {"function", "method"}
            and entity.get("name", "").isidentifier()
            and not entity.get("name", "").startswith("__")
            and symbol_counts[entity["name"]] == 1
        ),
        key=lambda entity: (entity["name"].casefold(), entity.get("file_path", "")),
    )
    seen_impact_targets = set()
    for target in impact_targets:
        if target["name"] in seen_impact_targets:
            continue
        seen_impact_targets.add(target["name"])
        affected = derive_impact(entities + bridges, [target["id"]], max_depth=3)
        if not 2 <= len(affected) <= 20:
            continue
        questions.append({
            "id": f"impact-{sum(q['category'] == 'impact' for q in questions) + 1:02d}",
            "category": "impact",
            "prompt": (
                f"What functions and methods are affected up to call depth three if "
                f"`{target['name']}` changes? Include repository-relative paths."
            ),
            "expected_entities": [target["id"]],
            "expected_relationships": [],
            "expected_impact": affected,
        })
        if sum(q["category"] == "impact" for q in questions) == 10:
            break

    questions.extend(_cross_language_questions(entities, bridges))
    category_counts = Counter(question["category"] for question in questions)
    if category_counts != {
        "discovery": 15,
        "relationship": 15,
        "impact": 10,
        "cross_language": 10,
    }:
        raise ValueError(f"could not generate the required 50 questions: {dict(category_counts)}")

    ollama_version = _ollama_digest(model_name)
    manifest = {
        "schema_version": 1,
        "benchmark_id": "jg-business-compliance-local",
        "repository": {
            "name": "JG Business Compliance selected source repositories",
            "path": str(root),
            "size_bucket": "medium",
            "loc": source_metadata["loc"],
            "git_commit": "multi-repository; see subrepositories",
            "subrepositories": repo_metadata,
            **source_metadata,
        },
        "model": {
            "name": model_name,
            "version": ollama_version,
            "temperature": 0,
            "seed": 42,
            "provider": "local Ollama (loopback only)",
        },
        "adapter_versions": {
            "baseline": f"ollama_benchmark_agent.py sha256:{adapter_hash}",
            "atlas": f"ollama_benchmark_agent.py sha256:{adapter_hash}",
            "benchmark_runner_sha256": runner_hash,
            "manifest_generator_sha256": generator_hash,
            "atlas_index_pipeline_sha256": atlas_source_hash,
        },
        "question_generation": {
            "method": "Atlas AST index, direct call graph, depth-3 impact graph, bridge resolver output",
            "ground_truth_reviewed": False,
            "review_required_before_publication": True,
            "source_roots": source_roots,
        },
        "entity_catalog": catalog,
        "questions": questions,
        "conditions": {
            condition: {
                "command": [sys.executable, str(atlas_package / "ollama_benchmark_agent.py")],
                "settings": {
                    "tools": tools,
                    "atlas_package": str(atlas_package),
                    "source_roots": source_roots,
                    "project_id": project_id,
                    "meili_url": meili_url,
                    "num_ctx": 8192,
                    "num_predict": 700,
                    "api_timeout_seconds": 240,
                    "keep_alive": "10m",
                },
            }
            for condition, tools in (
                ("baseline", ["Read", "Search", "Grep", "Glob"]),
                ("atlas", ["Read", "Search", "Grep", "Glob", "Atlas query"]),
            )
        },
    }
    validate_manifest(manifest)
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary = {
        "benchmark_id": manifest["benchmark_id"],
        "project_id": project_id,
        "index_name": index_name,
        "indexed_documents": indexed_entities,
        "source_files": len(allowed_files),
        "loc": source_metadata["loc"],
        "entities": len(entities),
        "bridges": len(bridges),
        "questions": dict(category_counts),
        "manifest": str(output_dir / "manifest.json"),
        "ground_truth_reviewed": False,
    }
    (output_dir / "setup-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(prog="atlas-prepare-benchmark")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--atlas-package", type=Path, default=_ATLAS_ROOT)
    parser.add_argument("--meili-url", default="http://127.0.0.1:17700")
    parser.add_argument("--model", default="gemma4:e4b")
    parser.add_argument("--source-roots", nargs="+", default=["jgs_desktop", "jgtendermatch"])
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        result = build_manifest(
            args.workspace,
            args.output_dir,
            args.atlas_package,
            args.meili_url,
            args.model,
            args.source_roots,
            force=args.force,
        )
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"atlas-prepare-benchmark: error: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
