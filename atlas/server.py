"""
Atlas — MCP server over stdio transport.

Exposes 5 tools for codebase ingestion, querying, impact analysis,
and edge-case detection via the existing code verifier.

Usage:
  python atlas/server.py
"""
import hashlib
import json
import os
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT.parent / ".env")
except ImportError:
    pass

from ingest.crawler import crawl
from ingest.parsers import parse_file
from ingest.call_graph import build_call_graph
from ingest.indexer import (
    create_index, index_documents, delete_index,
    get_index_stats, search,
)

SYSTEM_INSTRUCTION = (
    "Before modifying any code, call atlas_query and atlas_edge_check first. "
    "Never generate code referencing a function or file you have not retrieved "
    "from the codebase index."
)

TOOLS = [
    {
        "name": "atlas_ingest",
        "description": (
            "Crawl a codebase and build a structured knowledge base in Meilisearch. "
            "Call this before modifying any function/file to ensure the index is current. "
            "Creates one index per project — no cross-contamination between projects."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root_path": {
                    "type": "string",
                    "description": "Absolute path to the codebase root directory to crawl",
                },
                "project_id": {
                    "type": "string",
                    "description": "Unique identifier for this project (used as index name prefix)",
                },
                "languages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Languages to parse. Currently only 'python' is supported.",
                    "default": ["python"],
                },
            },
            "required": ["root_path", "project_id"],
        },
    },
    {
        "name": "atlas_query",
        "description": (
            "Query a codebase's Meilisearch index with a natural language question. "
            "Call this before modifying any function/file to understand what exists. "
            "Only retrieves what actually exists — never predicts or generates. "
            "Use bridge_type filter to restrict to cross-language relationships "
            "(http, env_var, config_file, db_schema, shared_type)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Natural language question about the codebase",
                },
                "project_id": {
                    "type": "string",
                    "description": "Project identifier (must match a previously ingested project)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return",
                    "default": 20,
                },
                "bridge_type": {
                    "type": "string",
                    "description": (
                        "Optional: filter results to a specific bridge type. "
                        "One of: http, env_var, config_file, db_schema, shared_type"
                    ),
                    "enum": ["http", "env_var", "config_file", "db_schema", "shared_type"],
                },
            },
            "required": ["question", "project_id"],
        },
    },
    {
        "name": "atlas_impact",
        "description": (
            "Find all functions that depend on the given functions via call graph edges. "
            "Call this before modifying any function/file to see what will be affected."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "functions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of function names to trace impact for",
                },
                "project_id": {
                    "type": "string",
                    "description": "Project identifier",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum call-graph traversal depth",
                    "default": 3,
                },
            },
            "required": ["functions", "project_id"],
        },
    },
    {
        "name": "atlas_edge_check",
        "description": (
            "Full edge-case pipeline: find all affected functions for a proposed change, "
            "gather their code, and send to the verifier to surface vulnerabilities. "
            "Call this before modifying any function/file."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The proposed code change",
                },
                "functions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Functions being changed",
                },
                "project_id": {
                    "type": "string",
                    "description": "Project identifier",
                },
                "language": {
                    "type": "string",
                    "description": "Programming language of the code",
                    "default": "python",
                },
            },
            "required": ["code", "functions", "project_id"],
        },
    },
    {
        "name": "atlas_status",
        "description": "Check whether a project has been indexed and get index stats.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project identifier to check",
                },
            },
            "required": ["project_id"],
        },
    },
]


def _current_tier() -> str:
    try:
        from license import load_license
        info = load_license()
        if info.get("valid"):
            return info.get("tier", "free")
    except Exception:
        pass
    return "free"


def _key_namespace() -> str:
    """
    Return an 8-char hex prefix derived from the active license key (or machine
    hostname for free-tier).  Used to namespace Meilisearch index names so two
    users sharing an instance never see each other's data.
    """
    try:
        from license import load_license
        info = load_license()
        if info.get("valid") and info.get("email"):
            return hashlib.sha256(info["email"].encode()).hexdigest()[:8]
    except Exception:
        pass
    import socket
    return hashlib.sha256(socket.gethostname().encode()).hexdigest()[:8]


def _scoped_project(project_id: str) -> str:
    """Return the namespaced project id used for all Meilisearch operations."""
    ns = _key_namespace()
    if project_id.startswith(f"{ns}_"):
        return project_id  # already scoped
    return f"{ns}_{project_id}"


def _handle_ingest(args: dict) -> dict:
    root_path = args["root_path"]
    project_id = args["project_id"]
    scoped_id = _scoped_project(project_id)
    languages = args.get("languages", ["python"])

    # Free-tier project limit: 1 project only
    try:
        from usage import record_project, project_count
        tier = _current_tier()
        if tier == "free":
            # Use a stable anonymous hash — no PII
            key_hash = hashlib.sha256(b"free-tier").hexdigest()
            existing = project_count(key_hash)
            # Check if this project is already recorded
            from usage import _conn
            with _conn() as c:
                row = c.execute(
                    "SELECT 1 FROM projects WHERE key_hash=? AND project=?",
                    (key_hash, project_id),
                ).fetchone()
            is_new = row is None
            if is_new and existing >= 1:
                return {
                    "error": (
                        "Free tier is limited to 1 project. "
                        f"You already have {existing} project(s) indexed. "
                        "Upgrade to Atlas Pro at https://horn-dev.co.za/atlas"
                    ),
                    "upgrade_url": "https://horn-dev.co.za/atlas",
                }
        else:
            from license import load_license
            info = load_license()
            key_hash = hashlib.sha256(
                info.get("email", "pro-user").encode()
            ).hexdigest()
        record_project(key_hash, project_id)
    except Exception:
        pass

    from usage import log_call
    log_call("atlas_ingest", project_id, _current_tier())

    py_files = crawl(root_path, languages)
    if not py_files:
        return {"indexed": 0, "project_id": project_id, "message": "No files found"}

    all_entities = []
    for fpath in py_files:
        entities = parse_file(fpath, root_path)
        all_entities.extend(entities)

    all_entities = build_call_graph(all_entities)

    idx_name = create_index(scoped_id)
    count = index_documents(scoped_id, all_entities)

    # Post-processing: detect cross-language bridges and add to the same index
    bridges_count = 0
    try:
        from ingest.bridge_resolver import resolve_all_bridges
        bridges = resolve_all_bridges(all_entities, scoped_id)
        if bridges:
            index_documents(scoped_id, bridges)
            bridges_count = len(bridges)
    except Exception:
        pass

    return {
        "indexed": count + bridges_count,
        "project_id": project_id,
        "index_name": idx_name,
        "files_crawled": len(py_files),
        "entities_parsed": len(all_entities),
        "bridges_detected": bridges_count,
    }


def _handle_query(args: dict) -> dict:
    from usage import log_call
    log_call("atlas_query", args.get("project_id"), _current_tier())
    question = args["question"]
    project_id = args["project_id"]
    scoped_id = _scoped_project(project_id)
    limit = args.get("limit", 20)

    query_text = question
    filter_kind = None

    try:
        from query.net import CodeQueryModel, PAD_IDX, UNK_IDX, KIND_LABELS
        import torch

        model_dir = _ROOT / "query" / "model"
        config_path = model_dir / "config.json"
        vocab_path = model_dir / "vocab.json"
        weights_path = model_dir / "query_model.pt"

        if config_path.exists() and vocab_path.exists() and weights_path.exists():
            with open(config_path, encoding="utf-8") as f:
                cfg = json.load(f)
            with open(vocab_path, encoding="utf-8") as f:
                vocab = json.load(f)

            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            model = CodeQueryModel(
                cfg["vocab_size"], cfg["embed_dim"],
                cfg["hidden"], cfg["num_layers"], cfg["dropout"],
                cfg.get("num_kinds", 6),
            ).to(device)
            model.load_state_dict(
                torch.load(weights_path, map_location=device, weights_only=True)
            )
            model.eval()

            tokens = question.split()
            ids = torch.tensor(
                [vocab.get(t.lower(), UNK_IDX) for t in tokens],
                dtype=torch.long,
            ).unsqueeze(0).to(device)
            lengths = torch.tensor([len(tokens)], dtype=torch.long).to(device)

            with torch.no_grad():
                token_logits, kind_logits = model(ids, lengths)
                token_preds = token_logits.argmax(dim=-1).squeeze(0).tolist()
                kind_idx = kind_logits.argmax(dim=-1).item()

            kept = [w for w, p in zip(tokens, token_preds) if p == 1]
            if kept:
                query_text = " ".join(kept)

            predicted_kind = KIND_LABELS[kind_idx]
            if predicted_kind != "any":
                filter_kind = predicted_kind
    except Exception:
        pass

    bridge_type = args.get("bridge_type")
    if bridge_type:
        # Bridge queries override kind filtering — search bridge documents only
        filters = f"kind = 'bridge' AND bridge_type = '{bridge_type}'"
    elif filter_kind:
        filters = f"kind = '{filter_kind}'"
    else:
        filters = None
    hits = search(scoped_id, query_text, filters=filters, limit=limit)

    return {
        "query": query_text,
        "filter_kind": filter_kind,
        "results": hits,
        "count": len(hits),
    }


def _handle_impact(args: dict) -> dict:
    from usage import log_call
    log_call("atlas_impact", args.get("project_id"), _current_tier())
    from edge.impact import find_impact

    functions = args["functions"]
    project_id = args["project_id"]
    scoped_id = _scoped_project(project_id)
    max_depth = args.get("max_depth", 3)

    affected = find_impact(scoped_id, functions, max_depth=max_depth)
    return {"affected": affected, "count": len(affected)}


def _handle_edge_check(args: dict) -> dict:
    from usage import log_call
    log_call("atlas_edge_check", args.get("project_id"), _current_tier())
    from license import requires_pro
    requires_pro()
    from edge.impact import find_impact
    from edge.bridge import check_code, check_batch

    code = args["code"]
    functions = args["functions"]
    project_id = args["project_id"]
    scoped_id = _scoped_project(project_id)
    language = args.get("language", "python")

    affected = find_impact(scoped_id, functions, max_depth=3)

    verifier_result = check_code(code, language)

    # Verify up to 10 most-directly-affected functions using the batch endpoint
    candidates = [n for n in affected if n.get("body_preview")][:10]
    affected_checks = []

    if candidates:
        blocks = [
            {"code": n["body_preview"], "language": language}
            for n in candidates
        ]
        batch = check_batch(blocks)
        for node, result in zip(candidates, batch.get("results", [])):
            affected_checks.append({
                "function": node["name"],
                "file_path": node.get("file_path", ""),
                "verification": result,
            })

    return {
        "proposed_code_check": verifier_result,
        "affected_functions": len(affected),
        "affected_checks": affected_checks,
        "impact_graph": [
            {"name": n["name"], "file_path": n.get("file_path", ""),
             "kind": n.get("kind", ""), "callers": n.get("called_by", [])}
            for n in affected
        ],
    }


def _handle_status(args: dict) -> dict:
    from usage import log_call
    log_call("atlas_status", args.get("project_id"), _current_tier())
    project_id = args["project_id"]
    scoped_id = _scoped_project(project_id)
    stats = get_index_stats(scoped_id)
    return {
        "project_id": project_id,
        "indexed": stats.get("exists", False),
        "document_count": stats.get("numberOfDocuments", 0),
        "index_name": stats.get("index_name", ""),
    }


import asyncio
from mcp.server.mcpserver import MCPServer
from mcp.server.models import InitializationOptions
import mcp.types as mcp_types


def _make_mcp_server() -> MCPServer:
    mcp = MCPServer(
        name="atlas",
        version="1.0.0",
        instructions=SYSTEM_INSTRUCTION,
    )

    @mcp.tool(
        name="atlas_ingest",
        description=(
            "Crawl a codebase and build a structured knowledge base in Meilisearch. "
            "Call this before modifying any function/file to ensure the index is current. "
            "Creates one index per project — no cross-contamination between projects."
        ),
    )
    def atlas_ingest(root_path: str, project_id: str, languages: list[str] | None = None) -> str:
        result = _handle_ingest({"root_path": root_path, "project_id": project_id, "languages": languages or ["python"]})
        return json.dumps(result, indent=2)

    @mcp.tool(
        name="atlas_query",
        description=(
            "Query a codebase's Meilisearch index with a natural language question. "
            "Call this before modifying any function/file to understand what exists. "
            "Only retrieves what actually exists — never predicts or generates. "
            "Use bridge_type filter to restrict to cross-language relationships "
            "(http, env_var, config_file, db_schema, shared_type)."
        ),
    )
    def atlas_query(question: str, project_id: str, limit: int = 20, bridge_type: str | None = None) -> str:
        result = _handle_query({"question": question, "project_id": project_id, "limit": limit, "bridge_type": bridge_type})
        return json.dumps(result, indent=2)

    @mcp.tool(
        name="atlas_impact",
        description=(
            "Find all functions that depend on the given functions via call graph edges. "
            "Call this before modifying any function/file to see what will be affected."
        ),
    )
    def atlas_impact(functions: list[str], project_id: str, max_depth: int = 3) -> str:
        result = _handle_impact({"functions": functions, "project_id": project_id, "max_depth": max_depth})
        return json.dumps(result, indent=2)

    @mcp.tool(
        name="atlas_edge_check",
        description=(
            "Full edge-case pipeline: find all affected functions for a proposed change, "
            "gather their code, and send to the verifier to surface vulnerabilities. "
            "Call this before modifying any function/file."
        ),
    )
    def atlas_edge_check(code: str, functions: list[str], project_id: str, language: str = "python") -> str:
        result = _handle_edge_check({"code": code, "functions": functions, "project_id": project_id, "language": language})
        return json.dumps(result, indent=2)

    @mcp.tool(
        name="atlas_status",
        description="Check whether a project has been indexed and get index stats.",
    )
    def atlas_status(project_id: str) -> str:
        result = _handle_status({"project_id": project_id})
        return json.dumps(result, indent=2)

    return mcp


def main():
    mcp = _make_mcp_server()
    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()
