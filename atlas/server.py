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
import re
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT.parent))

try:
    from dotenv import load_dotenv
    # Load project .env first (cwd), then fall back to atlas parent .env
    _cwd_env = Path.cwd() / ".env"
    if _cwd_env.exists():
        load_dotenv(str(_cwd_env), override=True)
    else:
        load_dotenv(_ROOT.parent / ".env")
except ImportError:
    pass

from ingest.crawler import crawl
from ingest.parsers import parse_file
from ingest.call_graph import build_call_graph
from ingest.indexer import (
    create_index, index_documents, delete_index,
    delete_stale_documents, get_index_stats, search, search_many,
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
            "Query a codebase's structured index with a natural language question. "
            "Returns indexed functions, classes, routes, and variables — only what actually exists. "
            "PRECISION TIP: use specific symbol names (signIn, useAuth, onAuthStateChanged) rather "
            "than conceptual terms (authentication functions). The query model extracts key tokens "
            "and searches against function names, signatures, call graph (calls/called_by), and "
            "body previews. For a list of symbols or search terms, separate them with spaces or "
            "pipes; Atlas searches each "
            "name independently and prioritizes exact matches. Broad auth searches also cover "
            "password, MFA, profile, and token flows. "
            "For discovery, use broad terms. "
            "Use bridge_type filter to restrict to cross-language relationships "
            "(http, env_var, config_file, db_schema, shared_type)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "Natural language question or a space/pipe-separated symbol or keyword list. "
                        "Examples: 'signIn signOut handleSubmit' (precise) or "
                        "'what handles user authentication' (discovery)."
                    ),
                },
                "project_id": {
                    "type": "string",
                    "description": "Project identifier (must match a previously ingested project)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results for natural-language queries (default 10). Symbol lists and auth discovery can return up to 50 results.",
                    "default": 10,
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
            "Find the given functions and their transitive callers. Impact is caller-only; "
            "use atlas_dependencies for bidirectional dependency exploration."
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
                "include_heuristic": {
                    "type": "boolean",
                    "description": (
                        "Also follow low-confidence edges (calls to a name defined in "
                        "several files with no import evidence). Off by default."
                    ),
                    "default": False,
                },
            },
            "required": ["functions", "project_id"],
        },
    },
    {
        "name": "atlas_dependencies",
        "description": (
            "Explore functions connected to the given functions through caller and callee "
            "edges. This is bidirectional dependency exploration, not change impact."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "functions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Entity IDs, qualified names, or unique function names",
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
    """Return the project id used for Meilisearch operations.
    Namespace prefix is only applied in hosted multi-tenant deployments.
    For local single-user installs, index names match what atlas init creates.
    """
    return project_id


_meili_started = False

def _ensure_meilisearch() -> bool:
    """Start Meilisearch if it isn't already running. Returns True if up."""
    global _meili_started
    import urllib.request as _ur
    meili_url = os.environ.get("MEILI_URL", "http://localhost:7700")
    meili_key = os.environ.get("MEILI_KEY", "poc_master_key_local_1234567890")

    def _is_up():
        try:
            req = _ur.Request(f"{meili_url}/health",
                              headers={"Authorization": f"Bearer {meili_key}"})
            with _ur.urlopen(req, timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    if _is_up():
        return True
    if _meili_started:
        return False  # already tried this session, don't loop

    # Try to find and launch the Meilisearch binary atlas init downloaded
    import platform, subprocess, time
    suffix = ".exe" if platform.system().lower() == "windows" else ""
    # Search common locations: project .atlas/bin, then home .atlas/bin
    cwd = Path.cwd()
    candidates = [
        cwd / ".atlas" / "bin" / f"meilisearch{suffix}",
        cwd.parent / ".atlas" / "bin" / f"meilisearch{suffix}",
        Path.home() / ".atlas" / "bin" / f"meilisearch{suffix}",
    ]
    bin_path = next((p for p in candidates if p.exists()), None)
    if bin_path is None:
        return False

    data_dir = bin_path.parent.parent / "meili_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [str(bin_path), "--db-path", str(data_dir),
         "--master-key", meili_key, "--no-analytics"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _meili_started = True
    for _ in range(20):
        time.sleep(1)
        if _is_up():
            return True
    return False


def _handle_ingest(args: dict) -> dict:
    _ensure_meilisearch()
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

    # Attach repo identity to all entities
    repo_id = ""
    duplicate_warnings: list[str] = []
    try:
        from ingest.repo_identity import fingerprint_repo, annotate_entities, detect_duplicate_repos
        repo_id = fingerprint_repo(root_path)
        annotate_entities(all_entities, repo_id)
        dups = detect_duplicate_repos(root_path)
        duplicate_warnings = [d["message"] for d in dups]
    except Exception:
        pass

    all_entities = build_call_graph(all_entities)

    idx_name = create_index(scoped_id)
    count = index_documents(scoped_id, all_entities)

    extra_docs: list[dict] = []

    # Cross-language bridges
    bridges_count = 0
    try:
        from ingest.bridge_resolver import resolve_all_bridges
        bridges = resolve_all_bridges(all_entities, scoped_id)
        extra_docs.extend(bridges)
        bridges_count = len(bridges)
    except Exception:
        pass

    # Route graph edges
    route_count = 0
    try:
        from ingest.route_graph import build_route_edges
        routes = build_route_edges(all_entities, scoped_id)
        extra_docs.extend(routes)
        route_count = len(routes)
    except Exception:
        pass

    # Event / callback / scheduler edges
    event_count = 0
    try:
        from ingest.event_graph import build_event_edges
        events = build_event_edges(all_entities, scoped_id)
        extra_docs.extend(events)
        event_count = len(events)
    except Exception:
        pass

    # Env / config provenance edges
    config_count = 0
    try:
        from ingest.config_graph import build_config_edges
        configs = build_config_edges(all_entities, scoped_id, root_path=root_path)
        extra_docs.extend(configs)
        config_count = len(configs)
    except Exception:
        pass

    # Monorepo / workspace package boundary edges
    pkg_count = 0
    try:
        from ingest.repo_identity import detect_package_edges
        pkgs = detect_package_edges(root_path, scoped_id)
        extra_docs.extend(pkgs)
        pkg_count = len(pkgs)
    except Exception:
        pass

    if extra_docs:
        index_documents(scoped_id, extra_docs)

    stale_removed = delete_stale_documents(scoped_id, all_entities + extra_docs)

    result: dict = {
        "indexed": count + len(extra_docs),
        "project_id": project_id,
        "index_name": idx_name,
        "files_crawled": len(py_files),
        "entities_parsed": len(all_entities),
        "bridges_detected": bridges_count,
        "route_edges": route_count,
        "event_edges": event_count,
        "config_edges": config_count,
        "package_edges": pkg_count,
        "stale_removed": stale_removed,
    }
    if repo_id:
        result["repo_id"] = repo_id
    if duplicate_warnings:
        result["warnings"] = duplicate_warnings
    return result


def _compact_hit(hit: dict) -> dict:
    """Strip empty fields and cap body_preview to reduce response token cost."""
    out = {
        "kind": hit.get("kind", ""),
        "name": hit.get("name", ""),
        "file_path": hit.get("file_path", ""),
        "line_start": hit.get("line_start"),
        "line_end": hit.get("line_end"),
    }
    sig = hit.get("signature", "")
    if sig:
        out["signature"] = sig
    doc = hit.get("docstring", "")
    if doc:
        out["docstring"] = doc
    preview = hit.get("body_preview", "")
    if preview:
        out["body_preview"] = preview[:200]
    calls = hit.get("calls") or []
    if calls:
        out["calls"] = calls
    called_by = hit.get("called_by") or []
    if called_by:
        out["called_by"] = called_by
    parent = hit.get("parent_class", "")
    if parent:
        out["parent_class"] = parent
    # Bridge-specific fields
    for key in ("bridge_type", "confidence", "variable_name", "route_path",
                "languages", "files", "uses"):
        val = hit.get(key)
        if val:
            out[key] = val
    return out


_QUERY_LANGUAGE_WORDS = {
    "a", "about", "all", "an", "and", "are", "do", "does", "find", "for",
    "function", "functions", "handle", "handles", "handling", "how", "i",
    "in", "is", "list", "look", "looking", "me", "of", "or", "please",
    "related", "search", "show", "that", "the", "to", "user", "users",
    "what", "where", "which", "who", "with", "find", "deal", "deals",
    "manage", "manages", "managing", "perform", "performs", "verify",
    "verifies", "verified", "used", "using", "part", "parts", "application",
    "project", "code", "codebase", "source", "implementation", "logic",
    "system", "responsible", "handler", "handlers", "across", "access",
    "how", "does", "work", "works", "related", "for", "that", "and",
}

_AUTH_QUERY_MARKERS = {
    "auth", "authentication", "authenticate", "authenticated", "login",
    "logout", "signin", "signout", "session", "token", "password",
    "credential", "credentials", "jwt", "oauth", "identity", "firebase",
    "mfa", "multifactor",
}
_AUTH_EXPANSION_TERMS = (
    "auth", "login", "signin", "signout", "password", "credential",
    "authentication", "authorization", "authorize", "admin",
    "verify", "verified", "verifyIdToken", "bearer", "middleware",
    "guard", "firebase", "token", "session", "password", "credential",
    "mfa", "multifactor", "recaptcha", "profile", "reauthenticate",
    "permission", "role",
)


def _identifier_query_tokens(question: str) -> list[str]:
    """Extract symbol/keyword terms from a whitespace, comma, or regex OR list."""
    normalized = re.sub(r"\(\?[a-zA-Z]+\)", " ", question)
    normalized = re.sub(r"\bsign[- ]?in\b", "signin", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"[.?*+]", "", normalized)
    tokens = re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", normalized)
    return list(dict.fromkeys(
        token for token in tokens if len(token) > 1
    ))


def _fallback_search_tokens(question: str) -> list[str]:
    tokens = []
    for token in _identifier_query_tokens(question):
        normalized = token.casefold()
        if normalized in _QUERY_LANGUAGE_WORDS:
            continue
        if normalized in {"authentication", "authenticated", "authenticate"}:
            token = "auth"
        if token not in tokens:
            tokens.append(token)
    return tokens


def _model_search_terms(tokens: list[str], kept_owners: set[int]) -> list[str]:
    """Discard query phrasing the model may retain on unfamiliar wording."""
    return [
        token for index, token in enumerate(tokens)
        if index in kept_owners and token.casefold() not in _QUERY_LANGUAGE_WORDS
    ]


def _auth_discovery_tokens(question: str,
                           tokens: list[str] | None = None) -> list[str] | None:
    """Expand broad auth questions to cover login, account, MFA, and token flows."""
    tokens = tokens if tokens is not None else _fallback_search_tokens(question)
    normalized = {token.casefold() for token in tokens}
    if not normalized.intersection(_AUTH_QUERY_MARKERS):
        return None

    camel_case_symbols = sum(
        bool(re.search(r"[a-z][A-Z]", token))
        for token in _identifier_query_tokens(question)
    )
    if camel_case_symbols >= 2:
        return None

    return list(dict.fromkeys([*tokens, *_AUTH_EXPANSION_TERMS]))


def _is_identifier_list_query(question: str) -> bool:
    tokens = _identifier_query_tokens(question)
    if not tokens:
        return False

    explicit_list = any(separator in question for separator in ("|", ",", ";"))
    if explicit_list:
        return not any(token.casefold() in _QUERY_LANGUAGE_WORDS for token in tokens)

    if len(tokens) < 2 or not all(
        re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", token) for token in tokens
    ) or any(token.casefold() in _QUERY_LANGUAGE_WORDS for token in tokens):
        return False

    styled_identifiers = sum(
        any(char.isupper() for char in token[1:]) or "_" in token or "$" in token
        for token in tokens
    )
    return (
        styled_identifiers >= min(2, len(tokens))
        or (len(tokens) >= 7 and styled_identifiers == 0)
    )


def _search_term_matches(term: str, text: str) -> bool:
    """Match whole words and camelCase components, not arbitrary substrings."""
    query = re.sub(r"[^A-Za-z0-9_$]", "", term).casefold()
    for identifier in re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", text):
        normalized = re.sub(r"[^A-Za-z0-9_$]", "", identifier).casefold()
        if normalized == query:
            return True
        author_prefix = (
            query == "auth"
            and normalized.startswith("author")
            and not normalized.startswith(("authorization", "authorize"))
        )
        if (len(query) >= 4 and normalized.startswith(query)
                and not author_prefix):
            return True
        parts = re.findall(
            r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|[0-9]+",
            identifier,
        )
        for part in parts:
            word = part.casefold()
            if word == query:
                return True
            if (len(query) >= 4 and word.startswith(query)
                    and word not in {"author", "authors", "authored", "authoring"}):
                return True
    return False


def _merge_identifier_searches(tokens: list[str],
                               search_results: list[list[dict]],
                               limit: int) -> list[dict]:
    """Prefer exact symbol matches while retaining useful per-symbol context."""
    path_tokens = {}
    for token, results in zip(tokens, search_results):
        for hit in results:
            path = hit.get("file_path")
            if (path and hit.get("kind") != "file"
                    and (hit.get("name") or "").casefold() == token.casefold()):
                path_tokens.setdefault(path, set()).add(token.casefold())

    merged = {}
    for token_index, (token, results) in enumerate(zip(tokens, search_results)):
        for rank, hit in enumerate(results):
            if hit.get("kind") == "file":
                continue
            searchable_text = " ".join(
                str(hit.get(field, ""))
                for field in (
                    "name", "signature", "docstring", "body_preview",
                    "calls", "called_by", "imports", "variable_name",
                    "search_terms",
                    "route_path",
                )
            )
            if not _search_term_matches(token, searchable_text):
                continue

            key = hit.get("id") or (
                hit.get("file_path"), hit.get("kind"), hit.get("name"),
                hit.get("line_start"),
            )
            item = merged.setdefault(key, {
                "hit": hit,
                "exact_matches": 0,
                "name_matches": set(),
                "matched_tokens": set(),
                "rank_score": 0.0,
                "first_seen": token_index,
                "path_coverage": len(path_tokens.get(hit.get("file_path"), set())),
            })
            item["matched_tokens"].add(token.casefold())
            item["rank_score"] += 1 / (rank + 1)
            hit_name = (hit.get("name") or "").casefold()
            if _search_term_matches(token, hit.get("name") or ""):
                item["name_matches"].add(token.casefold())
            if hit_name == token.casefold():
                item["exact_matches"] += 1

    ranked = sorted(
        merged.values(),
        key=lambda item: (
            -len(item["name_matches"]),
            -item["path_coverage"],
            -item["exact_matches"],
            -len(item["matched_tokens"]),
            -item["rank_score"],
            item["first_seen"],
        ),
    )
    return [item["hit"] for item in ranked[:limit]]


def _auth_relevance_score(hit: dict) -> int:
    """Score concrete auth evidence to keep broad auth discovery precise."""
    name = hit.get("name") or ""
    path = (hit.get("file_path") or "").casefold()
    calls = " ".join(hit.get("calls") or [])
    body = hit.get("body_preview") or ""
    evidence = f"{calls} {body}"
    score = 0
    normalized_name = re.sub(r"[^A-Za-z0-9_$]", "", name).casefold()

    for term in (
        "auth", "login", "logout", "signin", "signout", "password",
        "mfa", "recaptcha", "reauthenticate", "admin", "authorize",
    ):
        if normalized_name != term and _search_term_matches(term, name):
            score += 4

    if re.search(
        r"(?:require|verify|validate|check|authorize|ensure).{0,32}"
        r"(?:auth|admin|token|identity|permission|role)"
        r"|(?:auth|admin|token|identity|permission|role).{0,32}"
        r"(?:require|verify|validate|check|authorize|ensure)",
        normalized_name,
    ):
        score += 4
    if "profile" in name.casefold() and "components/settings/" in path:
        score += 4
    if "components/auth/" in path:
        score += 3
    if "verifyidtoken" in evidence.casefold() or "verifyidtoken" in normalized_name:
        score += 8
    if "server." in path and re.search(
        r"(?:authorization|bearer|verifyIdToken|global.?admin|authenticated)",
        f"{name} {evidence}",
        re.IGNORECASE,
    ):
        score += 4
    if re.search(
        r"signInWith|createUserWith|sendPasswordReset|updatePassword|"
        r"reauthenticateWith|multiFactor|PhoneAuthProvider|RecaptchaVerifier|"
        r"deleteUser|signOut\s*\(",
        evidence,
        re.IGNORECASE,
    ):
        score += 4

    return score


def _handle_query(args: dict) -> dict:
    _ensure_meilisearch()
    from usage import log_call
    log_call("atlas_query", args.get("project_id"), _current_tier())
    question = args["question"]
    project_id = args["project_id"]
    scoped_id = _scoped_project(project_id)
    limit = args.get("limit", 10)

    query_text = question
    filter_kind = None
    ranking_warning = None

    identifier_tokens = None
    fallback_tokens = None
    if _is_identifier_list_query(question):
        identifier_tokens = _identifier_query_tokens(question)
    else:
        try:
            from query.net import (
                CodeQueryModel, PAD_IDX, UNK_IDX, KIND_LABELS, query_subtokens,
            )
            import torch

            # Per-project model: <project_cwd>/.atlas/model/
            # Fall back to the shared atlas/query/model/ for backwards compat.
            _project_model = Path.cwd() / ".atlas" / "model"
            model_dir = _project_model if _project_model.exists() else _ROOT / "query" / "model"
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
                if cfg.get("tokenizer_version", 1) >= 2:
                    model_tokens = []
                    token_owners = []
                    for owner, token in enumerate(tokens):
                        pieces = query_subtokens(token)
                        model_tokens.extend(pieces)
                        token_owners.extend([owner] * len(pieces))
                else:
                    model_tokens = tokens
                    token_owners = list(range(len(tokens)))
                ids = torch.tensor(
                    [vocab.get(t.lower(), UNK_IDX) for t in model_tokens],
                    dtype=torch.long,
                ).unsqueeze(0).to(device)
                lengths = torch.tensor(
                    [len(model_tokens)], dtype=torch.long
                ).to(device)

                with torch.no_grad():
                    token_logits, kind_logits = model(ids, lengths)
                    token_preds = token_logits.argmax(dim=-1).squeeze(0).tolist()
                    kind_idx = kind_logits.argmax(dim=-1).item()
                    import torch.nn.functional as F
                    kind_conf = F.softmax(kind_logits, dim=-1)[0, kind_idx].item()

                kept_owners = {
                    owner for owner, prediction in zip(token_owners, token_preds)
                    if prediction == 1
                }
                kept = _model_search_terms(tokens, kept_owners)
                if kept and any(
                    token.casefold() not in _QUERY_LANGUAGE_WORDS
                    for token in kept
                ):
                    query_text = " ".join(kept)
                else:
                    fallback_tokens = _fallback_search_tokens(question)
                    if fallback_tokens:
                        query_text = " ".join(fallback_tokens)

                predicted_kind = KIND_LABELS[kind_idx]
                # Only filter by kind when the model is confident and the
                # prediction is a meaningful structural kind (not "any" or
                # "import", which appear spuriously on cross-codebase queries).
                if predicted_kind not in ("any", "import") and kind_conf >= 0.80:
                    filter_kind = predicted_kind
        except Exception:
            pass

    auth_tokens = _auth_discovery_tokens(question)
    if auth_tokens:
        fallback_tokens = auth_tokens
        identifier_tokens = None
        query_text = " ".join(auth_tokens)
        filter_kind = None

    bridge_type = args.get("bridge_type")
    if bridge_type:
        # Bridge queries override kind filtering — search bridge documents only
        filters = f"kind = 'bridge' AND bridge_type = '{bridge_type}'"
    elif filter_kind:
        filters = f"kind = '{filter_kind}'"
    else:
        filters = None
    independent_tokens = identifier_tokens or fallback_tokens
    if independent_tokens:
        auth_discovery = fallback_tokens == auth_tokens and auth_tokens is not None
        result_limit = min(
            50, max(limit, len(independent_tokens), 50 if auth_discovery else limit)
        )
        per_token_limit = 50 if auth_discovery else min(10, max(3, limit))
        token_results = []
        for offset in range(0, len(independent_tokens), 50):
            token_results.extend(search_many(
                scoped_id, independent_tokens[offset:offset + 50],
                filters=filters, limit=per_token_limit,
            ))
        hits = _merge_identifier_searches(
            independent_tokens, token_results, result_limit
        )
        if auth_discovery:
            hits = [
                hit for hit in hits
                if _auth_relevance_score(hit) >= 4
            ]
            hits.sort(key=_auth_relevance_score, reverse=True)
    else:
        from query.synonyms import expand_query_terms

        search_terms = expand_query_terms(
            _fallback_search_tokens(query_text)
        )[:50]
        candidate_limit = min(50, max(20, limit * 5))
        if search_terms:
            candidate_results = []
            for offset in range(0, len(search_terms), 50):
                candidate_results.extend(search_many(
                    scoped_id,
                    search_terms[offset:offset + 50],
                    filters=filters,
                    limit=candidate_limit,
                ))
            candidates = _merge_identifier_searches(
                search_terms,
                candidate_results,
                candidate_limit,
            )
        else:
            candidates = []

        if not candidates:
            candidates = search(
                scoped_id,
                query_text,
                filters=filters,
                limit=candidate_limit,
            )

        from query.semantic import rerank
        try:
            hits = rerank(question, candidates, limit)
        except (ImportError, OSError, ConnectionError) as exc:
            hits = candidates[:limit]
            ranking_warning = (
                "MiniLM semantic reranking was unavailable; returned lexical "
                f"results instead ({type(exc).__name__}: {exc})"
            )

    response = {
        "query": query_text,
        "filter_kind": filter_kind,
        "results": [_compact_hit(h) for h in hits],
        "count": len(hits),
    }
    if ranking_warning:
        response["ranking_warning"] = ranking_warning
    return response


def _handle_impact(args: dict) -> dict:
    _ensure_meilisearch()
    from usage import log_call
    log_call("atlas_impact", args.get("project_id"), _current_tier())
    from edge.impact import find_impact

    functions = args["functions"]
    project_id = args["project_id"]
    scoped_id = _scoped_project(project_id)
    max_depth = args.get("max_depth", 3)

    affected = find_impact(
        scoped_id, functions, max_depth=max_depth,
        include_heuristic=bool(args.get("include_heuristic", False)),
    )
    return {"affected": affected, "count": len(affected)}


def _handle_dependencies(args: dict) -> dict:
    _ensure_meilisearch()
    from usage import log_call
    log_call("atlas_dependencies", args.get("project_id"), _current_tier())
    from edge.impact import find_dependencies

    functions = args["functions"]
    project_id = args["project_id"]
    scoped_id = _scoped_project(project_id)
    max_depth = args.get("max_depth", 3)

    dependencies = find_dependencies(scoped_id, functions, max_depth=max_depth)
    return {"dependencies": dependencies, "count": len(dependencies)}


def _handle_edge_check(args: dict) -> dict:
    _ensure_meilisearch()
    from usage import log_call
    log_call("atlas_edge_check", args.get("project_id"), _current_tier())
    from license import requires_pro
    requires_pro()
    from edge.impact import find_impact
    try:
        from edge.bridge import check_code, check_batch
    except ImportError:
        from license import pro_component_missing_message
        return {"error": pro_component_missing_message("atlas_edge_check")}

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
    _ensure_meilisearch()
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
        result = _handle_ingest({"root_path": root_path, "project_id": project_id, "languages": languages or ["python", "javascript", "typescript"]})
        return json.dumps(result, indent=2)

    @mcp.tool(
        name="atlas_query",
        description=(
            "Query a codebase's Meilisearch index with a natural language question. "
            "Call this before modifying any function/file to understand what exists. "
            "Only retrieves what actually exists — never predicts or generates. "
            "For space- or pipe-separated symbol/keyword lists, Atlas searches each independently "
            "and prioritizes exact matches. "
            "Broad auth searches also include password, MFA, profile, and token flows. "
            "Use bridge_type filter to restrict to cross-language relationships "
            "(http, env_var, config_file, db_schema, shared_type)."
        ),
    )
    def atlas_query(question: str, project_id: str, limit: int = 10, bridge_type: str | None = None) -> str:
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
        name="atlas_dependencies",
        description=(
            "Explore dependencies in both caller and callee directions. "
            "Use atlas_impact for caller-only change impact."
        ),
    )
    def atlas_dependencies(functions: list[str], project_id: str, max_depth: int = 3) -> str:
        result = _handle_dependencies({
            "functions": functions,
            "project_id": project_id,
            "max_depth": max_depth,
        })
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
