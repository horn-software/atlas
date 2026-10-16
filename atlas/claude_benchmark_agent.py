"""Claude API adapter for paired Atlas benchmark runs.

Interface: identical to ollama_benchmark_agent.py — reads one JSON request from
stdin, runs an agentic loop using the Anthropic Messages API, writes one JSON
result to stdout.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import sys

_ATLAS_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ATLAS_ROOT))

TOOL_NAMES = ("Read", "Search", "Grep", "Glob", "atlas_query", "atlas_impact")
ATLAS_TOOLS = {"atlas_query", "atlas_impact"}
MAX_TOOL_OUTPUT = 16000


# ── Tool definitions ──────────────────────────────────────────────────────────

def _tool_spec(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


def tool_definitions(include_atlas: bool) -> list[dict]:
    tools = [
        _tool_spec(
            "Read",
            "Read a source file in the indexed repository. Paths are repository-relative.",
            {"path": {"type": "string", "description": "Repository-relative file path"}},
            ["path"],
        ),
        _tool_spec(
            "Search",
            "Find a literal, case-insensitive term in allowed source files.",
            {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 30},
            },
            ["query"],
        ),
        _tool_spec(
            "Grep",
            "Find a regular-expression match in allowed source files.",
            {
                "pattern": {"type": "string"},
                "limit": {"type": "integer", "default": 30},
            },
            ["pattern"],
        ),
        _tool_spec(
            "Glob",
            "List allowed source files matching a repository-relative glob pattern.",
            {
                "pattern": {"type": "string"},
                "limit": {"type": "integer", "default": 40},
            },
            ["pattern"],
        ),
    ]
    if include_atlas:
        tools.extend([
            _tool_spec(
                "atlas_query",
                "Query Atlas's indexed functions, classes, methods, call edges, and cross-project bridges.",
                {
                    "question": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                    "bridge_type": {
                        "type": "string",
                        "enum": ["http", "env_var", "config_file", "db_schema", "shared_type"],
                    },
                },
                ["question"],
            ),
            _tool_spec(
                "atlas_impact",
                "Use Atlas's call graph to find functions affected by changes to named functions.",
                {
                    "functions": {"type": "array", "items": {"type": "string"}},
                    "max_depth": {"type": "integer", "default": 3},
                },
                ["functions"],
            ),
        ])
    return tools


# ── Tool execution (same logic as ollama_benchmark_agent) ─────────────────────

def _source_files(root: Path, settings: dict) -> dict[str, Path]:
    atlas_package = Path(settings["atlas_package"]).resolve()
    sys.path.insert(0, str(atlas_package))
    from ingest.crawler import crawl

    allowed_roots = settings.get("source_roots")
    if not isinstance(allowed_roots, list) or not allowed_roots:
        raise ValueError("adapter settings require non-empty source_roots")
    root_resolved = root.resolve()
    allowed_directories = []
    for relative in allowed_roots:
        directory = (root_resolved / relative).resolve()
        if not directory.is_relative_to(root_resolved) or not directory.is_dir():
            raise ValueError(
                f"source root must be an existing directory under repository: {relative}"
            )
        allowed_directories.append(directory)

    files = {}
    for path in crawl(str(root_resolved)):
        resolved = Path(path).resolve()
        if not any(resolved.is_relative_to(d) for d in allowed_directories):
            continue
        files[resolved.relative_to(root_resolved).as_posix()] = resolved
    if not files:
        raise ValueError("no allowed source files found for this repository")
    return files


def _tool_result(name: str, arguments: dict, context: dict) -> str:
    root: Path = context["root"]
    files: dict[str, Path] = context["files"]

    if name == "Read":
        relative = PurePosixPath(arguments["path"].replace("\\", "/")).as_posix()
        path = files.get(relative)
        if path is None:
            raise ValueError(f"Read path is not an allowed source file: {relative}")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return json.dumps({
            "path": relative,
            "line_count": len(lines),
            "content": "\n".join(
                f"{i + 1}: {line}" for i, line in enumerate(lines)
            )[:8000],
        }, ensure_ascii=False)

    if name == "Glob":
        pattern = arguments["pattern"].replace("\\", "/")
        limit = max(1, min(int(arguments.get("limit", 40)), 100))
        matches = [
            relative for relative in sorted(files)
            if PurePosixPath(relative).match(pattern)
        ]
        return json.dumps({"matches": matches[:limit], "count": len(matches)}, ensure_ascii=False)

    if name in {"Search", "Grep"}:
        query = arguments.get("query") if name == "Search" else arguments.get("pattern")
        if not isinstance(query, str) or not query:
            raise ValueError(f"{name} requires a non-empty search string")
        matcher = (
            re.compile(re.escape(query), re.IGNORECASE)
            if name == "Search"
            else re.compile(query)
        )
        limit = max(1, min(int(arguments.get("limit", 30)), 80))
        results = []
        for relative, path in sorted(files.items()):
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if matcher.search(line):
                    results.append({"path": relative, "line": lineno, "text": line.strip()[:400]})
                    if len(results) >= limit:
                        return json.dumps(
                            {"matches": results, "truncated": True}, ensure_ascii=False
                        )
        return json.dumps({"matches": results, "truncated": False}, ensure_ascii=False)

    if name == "atlas_query":
        from ingest.indexer import search

        question = arguments["question"]
        limit = max(1, min(int(arguments.get("limit", 10)), 20))
        bridge_type = arguments.get("bridge_type")
        if bridge_type is not None and bridge_type not in {
            "http", "env_var", "config_file", "db_schema", "shared_type"
        }:
            raise ValueError(f"unsupported bridge type: {bridge_type}")
        filters = (
            f"kind = 'bridge' AND bridge_type = '{bridge_type}'" if bridge_type else None
        )
        hits = search(context["project_id"], question, filters=filters, limit=limit)
        fields = (
            "kind", "name", "file_path", "language", "signature", "docstring",
            "line_start", "calls", "called_by", "parent_class", "bridge_type",
            "route_path", "variable_name", "languages", "files", "uses",
            "target_function", "caller_function", "confidence",
        )
        return json.dumps(
            [{key: hit[key] for key in fields if key in hit} for hit in hits],
            ensure_ascii=False,
        )

    if name == "atlas_impact":
        from edge.impact import find_impact

        functions = arguments["functions"]
        if not isinstance(functions, list) or not functions or any(
            not isinstance(f, str) or not f for f in functions
        ):
            raise ValueError("atlas_impact functions must be a non-empty list of names")
        max_depth = max(0, min(int(arguments.get("max_depth", 3)), 5))
        affected = find_impact(context["project_id"], functions, max_depth=max_depth)
        fields = (
            "kind", "name", "file_path", "language", "line_start",
            "calls", "called_by", "_impact_depth", "bridge_type",
        )
        return json.dumps(
            [{key: hit[key] for key in fields if key in hit} for hit in affected],
            ensure_ascii=False,
        )

    raise ValueError(f"model requested an unavailable tool: {name}")


# ── Anthropic agentic loop ────────────────────────────────────────────────────

def run(request: dict) -> dict:
    import anthropic

    condition = request["condition"]
    include_atlas = condition == "atlas"
    root = Path(request["repository"]["path"]).resolve()
    settings = request.get("adapter_config", {})

    atlas_package = Path(settings["atlas_package"]).resolve()
    sys.path.insert(0, str(atlas_package))

    meili_url = settings["meili_url"]
    os.environ["MEILI_URL"] = meili_url
    os.environ["MEILI_KEY"] = ""

    context = {
        "root": root,
        "files": _source_files(root, settings),
        "project_id": settings["project_id"],
    }

    model_settings = request["model"]
    model_name = model_settings.get("name", "claude-sonnet-4-6")
    api_key = settings.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.Anthropic(api_key=api_key)

    tools = tool_definitions(include_atlas)
    allowed_tools = {t["name"] for t in tools}

    system_prompt = (
        "Answer the repository question using only the tools available in this run. "
        "Use more than one source for non-trivial claims. Never trigger applications, "
        "scrapers, migrations, database writes, cloud operations, or external network calls. "
        "The Read/Search/Grep/Glob tools expose source files only. "
        "In the answer, wrap every referenced function, method, class, or file in inline "
        "backticks; do not use backticks for ordinary words. For relationship_entities and "
        "impact_entities, return a list of strings formatted exactly as "
        '"entity_name@repository/relative/path", using only results observed through tools. '
        "Return one JSON object only with keys answer (string), relationship_entities "
        "(string array), and impact_entities (string array). For categories other than "
        "relationship and impact, return empty prediction arrays. Distinguish uncertainty "
        "from verified facts; do not invent identifiers or paths."
    )

    messages: list[dict] = [
        {"role": "user", "content": request["question"]["prompt"]},
    ]

    tool_counts = {name: 0 for name in ("Read", "Search", "Grep", "Glob", "Atlas query")}
    input_tokens = 0
    output_tokens = 0

    for _ in range(16):
        response = client.messages.create(
            model=model_name,
            max_tokens=1024,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )
        input_tokens += response.usage.input_tokens
        output_tokens += response.usage.output_tokens

        # Collect all content blocks
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        text_blocks = [b for b in response.content if b.type == "text"]

        # Add assistant turn to history
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn" or not tool_use_blocks:
            # Extract final text answer
            final_text = "\n".join(b.text for b in text_blocks).strip()
            if not final_text:
                final_text = "{}"

            # Parse JSON answer
            fenced = re.fullmatch(
                r"```(?:json)?\s*(.*?)\s*```", final_text, re.IGNORECASE | re.DOTALL
            )
            candidate = fenced.group(1).strip() if fenced else final_text
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                # Try embedded JSON block
                blocks = list(re.finditer(
                    r"```(?:json)?\s*(.*?)\s*```", final_text, re.IGNORECASE | re.DOTALL
                ))
                parsed = None
                for block in reversed(blocks):
                    try:
                        parsed = json.loads(block.group(1).strip())
                        break
                    except json.JSONDecodeError:
                        continue
                if parsed is None:
                    parsed = {
                        "answer": final_text,
                        "relationship_entities": [],
                        "impact_entities": [],
                    }

            if not isinstance(parsed, dict):
                parsed = {"answer": str(parsed), "relationship_entities": [], "impact_entities": []}
            parsed.setdefault("answer", final_text)
            parsed.setdefault("relationship_entities", [])
            parsed.setdefault("impact_entities", [])

            return {
                "answer": str(parsed["answer"]),
                "relationship_entities": [
                    str(x) for x in parsed.get("relationship_entities", [])
                    if isinstance(x, str)
                ],
                "impact_entities": [
                    str(x) for x in parsed.get("impact_entities", [])
                    if isinstance(x, str)
                ],
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "tool_calls": tool_counts,
                "response_format": "json",
            }

        # Handle tool calls
        tool_results = []
        for block in tool_use_blocks:
            name = block.name
            if name not in allowed_tools:
                result_content = json.dumps(
                    {"error": f"unavailable tool: {name}", "is_error": True}
                )
            else:
                tool_counts["Atlas query" if name in ATLAS_TOOLS else name] += 1
                try:
                    result_content = _tool_result(name, block.input, context)
                except (ValueError, re.error) as exc:
                    result_content = json.dumps({"error": str(exc), "is_error": True})
            if len(result_content) > MAX_TOOL_OUTPUT:
                result_content = result_content[:MAX_TOOL_OUTPUT] + "\n[tool output truncated]"
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_content,
            })

        messages.append({"role": "user", "content": tool_results})

    raise RuntimeError("Claude agent exceeded the 16-turn tool-call limit")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        result = run(payload)
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        sys.stderr.write(f"claude benchmark adapter: {exc}\n")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
