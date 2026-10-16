"""Local Ollama agent adapter for paired Atlas benchmark runs."""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

TOOL_NAMES = ("Read", "Search", "Grep", "Glob", "atlas_query", "atlas_impact")
ATLAS_TOOLS = {"atlas_query", "atlas_impact"}
MAX_TOOL_OUTPUT = 12000


def _function_tool(
    name: str,
    description: str,
    properties: dict,
    required: list[str],
) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def tool_definitions(include_atlas: bool) -> list[dict]:
    definitions = [
        _function_tool(
            "Read",
            "Read a source file in the indexed repositories. Paths are repository-relative.",
            {"path": {"type": "string"}},
            ["path"],
        ),
        _function_tool(
            "Search",
            "Find a literal, case-insensitive term in allowed source files.",
            {"query": {"type": "string"}, "limit": {"type": "integer"}},
            ["query"],
        ),
        _function_tool(
            "Grep",
            "Find a regular-expression match in allowed source files.",
            {"pattern": {"type": "string"}, "limit": {"type": "integer"}},
            ["pattern"],
        ),
        _function_tool(
            "Glob",
            "List allowed source files matching a repository-relative glob pattern.",
            {"pattern": {"type": "string"}, "limit": {"type": "integer"}},
            ["pattern"],
        ),
    ]
    if include_atlas:
        definitions.extend([
            _function_tool(
                "atlas_query",
                "Query Atlas's indexed functions, classes, methods, call edges, and cross-project bridges.",
                {
                    "question": {"type": "string"},
                    "limit": {"type": "integer"},
                    "bridge_type": {
                        "type": "string",
                        "enum": ["http", "env_var", "config_file", "db_schema", "shared_type"],
                    },
                },
                ["question"],
            ),
            _function_tool(
                "atlas_impact",
                "Use Atlas's call graph to find functions affected by changes to named functions.",
                {
                    "functions": {"type": "array", "items": {"type": "string"}},
                    "max_depth": {"type": "integer"},
                },
                ["functions"],
            ),
        ])
    return definitions


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
            raise ValueError(f"source root must be an existing directory under repository: {relative}")
        allowed_directories.append(directory)

    files = {}
    for path in crawl(str(root_resolved)):
        resolved = Path(path).resolve()
        if not any(resolved.is_relative_to(directory) for directory in allowed_directories):
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
            "content": "\n".join(f"{index + 1}: {line}" for index, line in enumerate(lines))[:8000],
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
        matcher = re.compile(re.escape(query), re.IGNORECASE) if name == "Search" else re.compile(query)
        limit = max(1, min(int(arguments.get("limit", 30)), 80))
        results = []
        for relative, path in sorted(files.items()):
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if matcher.search(line):
                    results.append({
                        "path": relative,
                        "line": line_number,
                        "text": line.strip()[:400],
                    })
                    if len(results) >= limit:
                        return json.dumps({"matches": results, "truncated": True}, ensure_ascii=False)
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
        filters = f"kind = 'bridge' AND bridge_type = '{bridge_type}'" if bridge_type else None
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
            not isinstance(function, str) or not function for function in functions
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


def _ollama_chat(payload: dict, timeout: float) -> dict:
    request = Request(
        "http://127.0.0.1:11434/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"local Ollama request failed: {exc}") from exc
    if not isinstance(result, dict) or not isinstance(result.get("message"), dict):
        raise ValueError("Ollama returned an invalid chat response")
    return result


def _decode_final_answer(content: str, category: str) -> tuple[dict, str]:
    candidate = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, re.IGNORECASE | re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        parsed = json.loads(candidate)
        return parsed, "json"
    except json.JSONDecodeError:
        pass

    blocks = list(re.finditer(r"```(?:json)?\s*(.*?)\s*```", content, re.IGNORECASE | re.DOTALL))
    for block in reversed(blocks):
        try:
            parsed = json.loads(block.group(1).strip())
        except json.JSONDecodeError:
            continue
        return parsed, "embedded_json"

    if candidate.startswith("{"):
        raise ValueError("Ollama final response began as JSON but was malformed")
    references = [
        f"{match.group('name')}@{match.group('path')}"
        for match in re.finditer(
            r"`(?P<name>[^`\r\n]+)`\s+(?:in|at|from)\s+`(?P<path>[^`\r\n]+)`",
            candidate,
        )
    ]
    return {
        "answer": candidate,
        "relationship_entities": references if category == "relationship" else [],
        "impact_entities": references if category == "impact" else [],
    }, "markdown_fallback"


def run(request: dict) -> dict:
    condition = request["condition"]
    include_atlas = condition == "atlas"
    root = Path(request["repository"]["path"]).resolve()
    settings = request.get("adapter_config", {})
    atlas_package = Path(settings["atlas_package"]).resolve()
    sys.path.insert(0, str(atlas_package))
    meili_url = urlsplit(settings["meili_url"])
    if meili_url.scheme != "http" or meili_url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("benchmark Meilisearch URL must use loopback HTTP")
    os.environ["MEILI_URL"] = settings["meili_url"]
    os.environ["MEILI_KEY"] = settings.get("meili_key", "")
    context = {
        "root": root,
        "files": _source_files(root, settings),
        "project_id": settings["project_id"],
    }
    definitions = tool_definitions(include_atlas)
    allowed_tools = {tool["function"]["name"] for tool in definitions}
    model_settings = request["model"]
    options = {
        "temperature": model_settings.get("temperature", 0),
        "seed": model_settings.get("seed", 42),
        "num_ctx": settings.get("num_ctx", 8192),
        "num_predict": settings.get("num_predict", 700),
    }
    system_prompt = (
        "Answer the repository question using only the tools available in this run. "
        "Use more than one source for non-trivial claims. Never trigger applications, "
        "scrapers, migrations, database writes, cloud operations, or external network calls. "
        "The Read/Search/Grep/Glob tools expose source files only. "
        "In the answer, wrap every referenced function, method, class, or file in inline "
        "backticks; do not use backticks for ordinary words. For relationship_entities and "
        "impact_entities, return a list of strings formatted exactly as "
        "\"entity_name@repository/relative/path\", using only results observed through tools. "
        "Return one JSON object only with keys answer (string), relationship_entities "
        "(string array), and impact_entities (string array). For categories other than "
        "relationship and impact, return empty prediction arrays. Distinguish uncertainty "
        "from verified facts; do not invent identifiers or paths."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": request["question"]["prompt"]},
    ]
    tool_counts = {name: 0 for name in ("Read", "Search", "Grep", "Glob", "Atlas query")}
    input_tokens = 0
    output_tokens = 0
    api_timeout = float(settings.get("api_timeout_seconds", 240))
    empty_final_retries = 0
    require_json = False

    for _ in range(16):
        payload = {
            "model": model_settings["name"],
            "messages": messages,
            "tools": definitions,
            "stream": False,
            "think": False,
            "keep_alive": settings.get("keep_alive", "10m"),
            "options": options,
        }
        if require_json:
            payload["format"] = "json"
        response = _ollama_chat(payload, api_timeout)
        prompt_count, output_count = response.get("prompt_eval_count"), response.get("eval_count")
        if not isinstance(prompt_count, int) or not isinstance(output_count, int):
            raise ValueError("Ollama response omitted provider token counts")
        input_tokens += prompt_count
        output_tokens += output_count
        assistant = response["message"]
        messages.append(assistant)
        calls = assistant.get("tool_calls", [])
        if not calls:
            content = assistant.get("content", "").strip()
            if not content:
                if empty_final_retries >= 2:
                    raise ValueError(
                        f"Ollama repeatedly returned an empty final answer for "
                        f"{request['question']['id']}"
                    )
                empty_final_retries += 1
                require_json = True
                messages.append({
                    "role": "user",
                    "content": (
                        "Your previous turn produced no user-facing answer. Now return the "
                        "complete requested JSON response. Do not return an empty message."
                    ),
                })
                continue
            final, response_format = _decode_final_answer(
                content, request["question"]["category"]
            )
            if (
                not isinstance(final, dict)
                or not isinstance(final.get("answer"), str)
                or any(not isinstance(final.get(field), list) for field in (
                    "relationship_entities", "impact_entities"
                ))
                or any(
                    not isinstance(value, str)
                    for field in ("relationship_entities", "impact_entities")
                    for value in final[field]
                )
            ):
                raise ValueError("Ollama final answer has an invalid JSON response shape")
            return {
                **final,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "tool_calls": tool_counts,
                "response_format": response_format,
            }

        for tool_call in calls:
            function = tool_call.get("function", {})
            name = function.get("name")
            if name not in allowed_tools:
                raise ValueError(f"model requested unavailable tool: {name}")
            arguments: Any = function.get("arguments", {})
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                raise ValueError(f"tool arguments for {name} must be a JSON object")
            tool_counts["Atlas query" if name in ATLAS_TOOLS else name] += 1
            try:
                content = _tool_result(name, arguments, context)
            except (ValueError, re.error) as exc:
                content = json.dumps({"error": str(exc), "is_error": True})
            if len(content) > MAX_TOOL_OUTPUT:
                content = content[:MAX_TOOL_OUTPUT] + "\n[tool output truncated]"
            messages.append({
                "role": "tool",
                "tool_name": name,
                "content": content,
            })
    raise RuntimeError("Ollama agent exceeded the 16-turn tool-call limit")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        result = run(payload)
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        sys.stderr.write(f"ollama benchmark adapter: {exc}\n")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
