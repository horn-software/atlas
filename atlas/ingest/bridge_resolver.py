"""
Cross-language bridge resolver for Atlas.

Post-processing pass that detects cross-language relationships and adds
bridge documents to the same Meilisearch index as entity documents.

Five bridge types:
  1. http       — HTTP/REST route caller → route handler
  2. env_var    — same .env variable read in multiple languages
  3. config_file — same config file referenced in multiple languages
  4. db_schema  — same DB table/ORM model used in multiple languages
  5. shared_type — same struct/interface name in multiple languages (low confidence)

Limitation: dynamically-constructed paths (app.use('/' + prefix, router))
and runtime-evaluated env var names cannot be detected statically.
"""
import hashlib
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bridge_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def _norm_route(path: str) -> str:
    """
    Normalise a URL path for route matching.
    - Extracts path from full URL if needed
    - Lowercases and strips trailing slash
    - Converts :id / {id} / <id> / <int:id> to :param
    Returns '' if not a valid internal path.
    """
    path = path.strip()
    if path.startswith(("http://", "https://")):
        path = urlparse(path).path
    path = path.lower()
    if path.endswith("/") and path != "/":
        path = path[:-1]
    path = re.sub(r":[A-Za-z_]\w*", ":param", path)
    path = re.sub(r"\{[^}]+\}", ":param", path)
    path = re.sub(r"<[^>]+>", ":param", path)
    return path


def _is_api_path(path: str) -> bool:
    if not path.startswith("/") or path in ("/", ""):
        return False
    # Reject paths where the first segment looks like a domain (contains a dot)
    first_seg = path.split("/")[1] if "/" in path[1:] else path[1:]
    if "." in first_seg:
        return False
    return True


def _entity_text(ent: dict) -> str:
    """Combine all searchable text fields for pattern matching."""
    return "\n".join([
        ent.get("body_preview") or "",
        ent.get("signature") or "",
        "\n".join(ent.get("decorators") or []),
    ])


def _base_bridge(bid: str, bridge_type: str, project_id: str,
                  name: str, language: str, file_path: str,
                  signature: str, confidence: str,
                  provenance: str = "ast") -> dict:
    return {
        "id": bid,
        "kind": "bridge",
        "bridge_type": bridge_type,
        "project_id": project_id,
        "name": name,
        "language": language,
        "file_path": file_path,
        "signature": signature,
        "docstring": "",
        "body_preview": "",
        "calls": [],
        "called_by": [],
        "called_by_ids": [],
        "imports": [],
        "parent_class": "",
        "decorators": [],
        "line_start": 0,
        "line_end": 0,
        "confidence": confidence,
        "provenance": provenance,
        "evidence": [{"file": file_path, "line": 0}],
    }


# ── BRIDGE 1 — HTTP/REST route matching ───────────────────────────────────────

# HTTP client call patterns — extract the URL/path argument
_HTTP_CALLER_RE: dict[str, list] = {
    "python": [
        re.compile(r"""(?:requests|httpx|aiohttp)\s*\.\s*(?:get|post|put|patch|delete|request)\s*\(\s*['"`]([^'"`?\s]+)""", re.I),
        re.compile(r"""client\s*\.\s*(?:get|post|put|patch|delete)\s*\(\s*['"`]([^'"`?\s]+)""", re.I),
    ],
    "javascript": [
        re.compile(r"""(?:fetch|axios\.(?:get|post|put|patch|delete)|request)\s*\(\s*['"`]([^'"`?\s]+)""", re.I),
        re.compile(r"""\$(?:http|axios)\s*\.\s*(?:get|post|put|patch|delete)\s*\(\s*['"`]([^'"`?\s]+)""", re.I),
        re.compile(r"""\.(?:get|post|put|patch|delete)\s*\(\s*`([^`?{]+)""", re.I),
    ],
    "go": [
        re.compile(r"""http\.(?:Get|Post)\s*\(\s*"([^"?\s]+)"""),
        re.compile(r"""client\.(?:Get|Post|Put|Delete|Do)\s*\(\s*"([^"?\s]+)"""),
        re.compile(r"""http\.NewRequest\s*\(\s*"\w+"\s*,\s*"([^"?\s]+)"""),
    ],
    "ruby": [
        re.compile(r"""(?:Net::HTTP|RestClient|HTTParty)\s*\.\s*(?:get|post|put|delete)\s*\(\s*['"]([^'"?\s]+)""", re.I),
        re.compile(r"""Faraday\.new.*?\.(?:get|post|put|delete)\s*\(\s*['"]([^'"?\s]+)""", re.I),
    ],
}
_HTTP_CALLER_RE["typescript"] = _HTTP_CALLER_RE["javascript"]

# Route definition patterns — extract the path from route registrations
_ROUTE_DEF_RE: dict[str, list] = {
    "python": [
        # Flask / FastAPI / Starlette decorators
        re.compile(r"""@[\w.]+\.(?:route|get|post|put|patch|delete|head|options)\s*\(\s*['"`]([^'"`]+)""", re.I),
        # Django path() / re_path() / url()
        re.compile(r"""(?:path|re_path|url)\s*\(\s*r?['"`]([^'"`\^$]+)['"`]""", re.I),
    ],
    "javascript": [
        # Express / Koa-router / Hapi
        re.compile(r"""(?:app|router|server|express\(\))\s*\.\s*(?:get|post|put|patch|delete|use|all)\s*\(\s*['"`]([^'"`]+)""", re.I),
        # fastify
        re.compile(r"""fastify\s*\.\s*(?:get|post|put|patch|delete|route)\s*\(\s*['"`]([^'"`]+)""", re.I),
    ],
    "typescript": [
        re.compile(r"""(?:app|router|server)\s*\.\s*(?:get|post|put|patch|delete|use|all)\s*\(\s*['"`]([^'"`]+)""", re.I),
        # NestJS decorators
        re.compile(r"""@(?:Get|Post|Put|Patch|Delete|Controller)\s*\(\s*['"`]([^'"`]*)['"`]"""),
    ],
    "go": [
        re.compile(r"""(?:http\.HandleFunc|mux\.HandleFunc|r\.HandleFunc|router\.HandleFunc)\s*\(\s*"([^"]+)"""),
        re.compile(r"""(?:r|router|mux)\s*\.\s*(?:GET|POST|PUT|PATCH|DELETE|Handle)\s*\(\s*"([^"]+)"""),
    ],
    "ruby": [
        re.compile(r"""(?:^|\s)(?:get|post|put|patch|delete)\s+['"]([^'"]+)['"]""", re.M | re.I),
        re.compile(r"""resources\s+:(\w+)""", re.I),  # Rails resources (plural resource name)
    ],
}


def build_route_registry(entities: list[dict]) -> list[dict]:
    """Build a route registry with method, handler, file, and middleware metadata."""
    routes: list[dict] = []
    for ent in entities:
        lang = ent.get("language", "")
        pats = _ROUTE_DEF_RE.get(lang, [])
        if not pats:
            continue
        text = _entity_text(ent)
        for pat in pats:
            for m in pat.finditer(text):
                route_path = _norm_route(m.group(1))
                if not _is_api_path(route_path):
                    continue
                method = "GET"
                if "post" in (m.group(0) or "").lower():
                    method = "POST"
                elif "put" in (m.group(0) or "").lower():
                    method = "PUT"
                elif "delete" in (m.group(0) or "").lower():
                    method = "DELETE"
                elif "patch" in (m.group(0) or "").lower():
                    method = "PATCH"

                middleware = []
                if "middleware" in text.lower() or "use(" in text:
                    for mw in re.finditer(r"(?:middleware|use)\s*\(\s*['\"]([^'\"]+)['\"]", text, re.I):
                        middleware.append(mw.group(1))

                routes.append({
                    "language": lang,
                    "file_path": ent.get("file_path", ""),
                    "function": ent.get("name", ""),
                    "qual_name": ent.get("qualified_name", ent.get("name", "")),
                    "route_path": route_path,
                    "method": method,
                    "middleware": sorted(set(middleware)),
                })
    return routes


def detect_http_bridges(entities: list[dict], project_id: str) -> list[dict]:
    """Bridge 1 — detect HTTP caller → route handler cross-language links."""
    callers: list[dict] = []
    for ent in entities:
        lang = ent.get("language", "")
        pats = _HTTP_CALLER_RE.get(lang, [])
        if not pats:
            continue
        text = _entity_text(ent)
        paths: set[str] = set()
        for pat in pats:
            for m in pat.finditer(text):
                norm = _norm_route(m.group(1))
                if _is_api_path(norm):
                    paths.add(norm)
        if paths:
            callers.append({
                "language": lang,
                "file": ent.get("file_path", ""),
                "function": ent.get("name", ""),
                "qual_name": ent.get("qualified_name", ent.get("name", "")),
                "paths": paths,
            })

    routes = build_route_registry(entities)

    seen: dict[str, dict] = {}
    for caller in callers:
        for cpath in caller["paths"]:
            for route in routes:
                if route["file_path"] == caller["file"]:
                    continue
                rpath = route["route_path"]
                if cpath == rpath or cpath.startswith(rpath.rstrip("/") + "/"):
                    bid = _bridge_id(caller["file"], route["file_path"], rpath, project_id)
                    if bid not in seen:
                        b = _base_bridge(
                            bid=bid,
                            bridge_type="http",
                            project_id=project_id,
                            name=f"HTTP {rpath}",
                            language=caller["language"],
                            file_path=caller["file"],
                            signature=(
                                f"{caller['language']}:{caller['function']}"
                                f" → {route['language']}:{route['function']}"
                                f" via {rpath}"
                            ),
                            confidence="exact",
                            provenance="ast",
                        )
                        b.update({
                            "route_path": rpath,
                            "method": route.get("method", "GET"),
                            "caller_language": caller["language"],
                            "caller_file": caller["file"],
                            "caller_function": caller["function"],
                            "caller_qual_name": caller.get("qual_name"),
                            "target_language": route["language"],
                            "target_file": route["file_path"],
                            "target_function": route["function"],
                            "target_qual_name": route.get("qual_name"),
                            "middleware": route.get("middleware", []),
                        })
                        seen[bid] = b
    return list(seen.values())


# ── BRIDGE 2 — Shared environment variables ────────────────────────────────────

_ENV_RE: dict[str, list] = {
    "python": [
        re.compile(r"""os\.environ\.get\s*\(\s*['"`]([A-Za-z_][A-Za-z0-9_]*)['"`]"""),
        re.compile(r"""os\.getenv\s*\(\s*['"`]([A-Za-z_][A-Za-z0-9_]*)['"`]"""),
        re.compile(r"""os\.environ\s*\[\s*['"`]([A-Za-z_][A-Za-z0-9_]*)['"`]\s*\]"""),
        re.compile(r"""environ\s*\[\s*['"`]([A-Za-z_][A-Za-z0-9_]*)['"`]\s*\]"""),
    ],
    "javascript": [
        re.compile(r"""process\.env\.([A-Z][A-Z0-9_]+)"""),
        re.compile(r"""process\.env\s*\[\s*['"`]([A-Za-z_][A-Za-z0-9_]*)['"`]\s*\]"""),
    ],
    "go": [
        re.compile(r'os\.Getenv\s*\(\s*"([A-Za-z_][A-Za-z0-9_]*)"'),
        re.compile(r'os\.LookupEnv\s*\(\s*"([A-Za-z_][A-Za-z0-9_]*)"'),
    ],
    "ruby": [
        re.compile(r"""ENV\s*\[\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""),
        re.compile(r"""ENV\.fetch\s*\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""),
    ],
    "rust": [
        re.compile(r'env::var\s*\(\s*"([A-Za-z_][A-Za-z0-9_]*)"'),
    ],
}
_ENV_RE["typescript"] = _ENV_RE["javascript"]


def detect_env_bridges(entities: list[dict], project_id: str) -> list[dict]:
    """Bridge 2 — detect same .env variable referenced in multiple languages."""
    var_uses: dict[str, list[dict]] = {}
    for ent in entities:
        lang = ent.get("language", "")
        pats = _ENV_RE.get(lang, [])
        if not pats:
            continue
        text = _entity_text(ent)
        for pat in pats:
            for m in pat.finditer(text):
                var = m.group(1)
                if len(var) < 2:
                    continue
                var_uses.setdefault(var, []).append({
                    "language": lang,
                    "file": ent.get("file_path", ""),
                    "function": ent.get("name", ""),
                })

    bridges = []
    for var, uses in var_uses.items():
        langs = {u["language"] for u in uses}
        if len(langs) < 2:
            continue
        files = sorted({u["file"] for u in uses})
        bid = _bridge_id("env", var, project_id)
        b = _base_bridge(
            bid=bid,
            bridge_type="env_var",
            project_id=project_id,
            name=f"ENV:{var}",
            language=sorted(langs)[0],
            file_path=files[0],
            signature=f"{var} shared across {', '.join(sorted(langs))}",
            confidence="exact",
            provenance="ast",
        )
        b.update({
            "variable_name": var,
            "languages": sorted(langs),
            "files": files,
            "uses": uses,
        })
        bridges.append(b)
    return bridges


# ── BRIDGE 3 — Shared config file references ──────────────────────────────────

_CONFIG_PATH_RE = re.compile(
    r"""['"`]([./][^'"`\s]+\.(?:json|yaml|yml|toml|ini))['"`]""", re.I
)


def detect_config_bridges(entities: list[dict], project_id: str) -> list[dict]:
    """Bridge 3 — detect same config file opened by multiple languages."""
    config_uses: dict[str, list[dict]] = {}
    for ent in entities:
        lang = ent.get("language", "")
        text = _entity_text(ent)
        for m in _CONFIG_PATH_RE.finditer(text):
            raw = m.group(1).lstrip("./").replace("\\", "/").lower()
            if not raw:
                continue
            config_uses.setdefault(raw, []).append({
                "language": lang,
                "file": ent.get("file_path", ""),
                "function": ent.get("name", ""),
            })

    bridges = []
    for cfg, uses in config_uses.items():
        langs = {u["language"] for u in uses}
        if len(langs) < 2:
            continue
        files = sorted({u["file"] for u in uses})
        bid = _bridge_id("config", cfg, project_id)
        b = _base_bridge(
            bid=bid,
            bridge_type="config_file",
            project_id=project_id,
            name=f"CONFIG:{cfg}",
            language=sorted(langs)[0],
            file_path=files[0],
            signature=f"{cfg} shared across {', '.join(sorted(langs))}",
            confidence="exact",
            provenance="ast",
        )
        b.update({
            "config_file": cfg,
            "languages": sorted(langs),
            "files": files,
        })
        bridges.append(b)
    return bridges


# ── BRIDGE 4 — Database schema bridges ────────────────────────────────────────

_DB_RE: dict[str, list] = {
    "python": [
        re.compile(r"""__tablename__\s*=\s*['"`](\w+)['"`]"""),
        re.compile(r"""class\s+(\w+)\s*\(\s*(?:models\.Model|Base|db\.Model|DeclarativeBase)"""),
        re.compile(r"""Table\s*\(\s*['"`](\w+)['"`]""", re.I),
        re.compile(r"""FROM\s+["`]?(\w+)["`]?(?:\s|;|$)""", re.I),
        re.compile(r"""INSERT\s+INTO\s+["`]?(\w+)["`]?""", re.I),
        re.compile(r"""UPDATE\s+["`]?(\w+)["`]?\s+SET""", re.I),
        re.compile(r"""CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["`]?(\w+)["`]?""", re.I),
    ],
    "typescript": [
        re.compile(r"""model\s+(\w+)\s*\{"""),                         # Prisma
        re.compile(r"""@Entity\s*\(\s*['"`](\w+)['"`]"""),             # TypeORM explicit name
        re.compile(r"""@Entity\s*\(\s*\)[\s\S]{0,50}?class\s+(\w+)"""),  # TypeORM implicit
        re.compile(r"""createTable\s*\(\s*['"`](\w+)['"`]""", re.I),   # Knex migrations
        re.compile(r"""FROM\s+["`]?(\w+)["`]?(?:\s|;|$)""", re.I),
        re.compile(r"""INSERT\s+INTO\s+["`]?(\w+)["`]?""", re.I),
    ],
    "go": [
        re.compile(r"""gorm:\s*"[^"]*table:(\w+)"""),
        re.compile(r'db\.Table\s*\(\s*"(\w+)"'),
        re.compile(r"""FROM\s+"?(\w+)"?(?:\s|;|$)""", re.I),
        re.compile(r"""INSERT\s+INTO\s+"?(\w+)"?""", re.I),
    ],
    "javascript": [
        re.compile(r"""sequelize\.define\s*\(\s*['"`](\w+)['"`]""", re.I),
        re.compile(r"""knex\s*\(\s*['"`](\w+)['"`]""", re.I),
        re.compile(r"""FROM\s+["`]?(\w+)["`]?(?:\s|;|$)""", re.I),
        re.compile(r"""INSERT\s+INTO\s+["`]?(\w+)["`]?""", re.I),
    ],
    "java": [
        re.compile(r"""@Table\s*\(\s*name\s*=\s*['"`](\w+)['"`]"""),
        re.compile(r"""@Entity"""),  # class name used as table name if no @Table
    ],
}
_DB_RE["typescript"] = _DB_RE.get("typescript", [])

# Short/numeric names that are noise in SQL
_SKIP_TABLE_NAMES = {"a", "b", "t", "n", "id", "it", "in", "as", "of", "or"}


def detect_db_bridges(entities: list[dict], project_id: str) -> list[dict]:
    """Bridge 4 — detect same DB table/model referenced in multiple languages."""
    table_uses: dict[str, list[dict]] = {}
    for ent in entities:
        lang = ent.get("language", "")
        pats = _DB_RE.get(lang, [])
        if not pats:
            continue
        text = _entity_text(ent)
        for pat in pats:
            for m in pat.finditer(text):
                table = m.group(1).lower() if m.lastindex and m.lastindex >= 1 else ""
                if not table or table in _SKIP_TABLE_NAMES or len(table) < 2:
                    continue
                table_uses.setdefault(table, []).append({
                    "language": lang,
                    "file": ent.get("file_path", ""),
                    "function": ent.get("name", ""),
                })

    bridges = []
    for table, uses in table_uses.items():
        langs = {u["language"] for u in uses}
        if len(langs) < 2:
            continue
        files = sorted({u["file"] for u in uses})
        bid = _bridge_id("db", table, project_id)
        b = _base_bridge(
            bid=bid,
            bridge_type="db_schema",
            project_id=project_id,
            name=f"TABLE:{table}",
            language=sorted(langs)[0],
            file_path=files[0],
            signature=f"table {table} accessed in {', '.join(sorted(langs))}",
            confidence="exact",
            provenance="ast",
        )
        b.update({
            "table_name": table,
            "languages": sorted(langs),
            "files": files,
        })
        bridges.append(b)
    return bridges


# ── BRIDGE 5 — Shared type / struct definitions ────────────────────────────────

_TYPE_RE: dict[str, re.Pattern] = {
    "typescript": re.compile(r"""(?:interface|type)\s+([A-Z]\w+)\s*(?:=\s*\{|\{|<|extends)"""),
    "python":     re.compile(r"""class\s+([A-Z]\w+)\s*\(\s*(?:TypedDict|BaseModel|dataclass|Schema|Serializer|NamedTuple|Protocol)"""),
    "go":         re.compile(r"""type\s+([A-Z]\w+)\s+struct\b"""),
    "rust":       re.compile(r"""(?:pub\s+)?struct\s+([A-Z]\w+)\b"""),
    "java":       re.compile(r"""(?:public\s+)?(?:class|interface|record)\s+([A-Z]\w+)\b"""),
    "kotlin":     re.compile(r"""(?:data\s+)?class\s+([A-Z]\w+)\b"""),
    "csharp":     re.compile(r"""(?:public\s+)?(?:class|interface|struct|record)\s+([A-Z]\w+)\b"""),
    "swift":      re.compile(r"""(?:public\s+)?(?:class|struct|protocol)\s+([A-Z]\w+)\b"""),
}

# Common names that produce near-universal false positives
_SKIP_TYPE_NAMES = {
    "error", "exception", "request", "response", "config", "context",
    "options", "params", "result", "data", "item", "record", "node",
    "event", "handler", "service", "client", "server", "app", "main",
    "base", "abstract", "model", "schema", "dto", "vo", "entity",
    "manager", "controller", "repository", "factory", "builder",
    "utils", "util", "info", "meta", "state", "store", "view",
    "type", "interface", "class", "struct", "object", "list",
}


def _singularize(name: str) -> str:
    n = name.lower()
    if n.endswith("ies"):
        return n[:-3] + "y"
    if n.endswith("es") and len(n) > 3:
        return n[:-2]
    if n.endswith("s") and not n.endswith("ss") and len(n) > 2:
        return n[:-1]
    return n


def detect_type_bridges(entities: list[dict], project_id: str) -> list[dict]:
    """Bridge 5 — detect same struct/interface name across languages (low confidence)."""
    type_uses: dict[str, list[dict]] = {}

    for ent in entities:
        lang = ent.get("language", "")

        # Named class entities are the cleanest source
        if ent.get("kind") == "class":
            name = ent.get("name", "")
            if name and len(name) >= 3 and name[0].isupper():
                canonical = _singularize(name)
                if canonical not in _SKIP_TYPE_NAMES:
                    type_uses.setdefault(canonical, []).append({
                        "language": lang,
                        "file": ent.get("file_path", ""),
                        "name": name,
                    })

        # Also scan body_preview for inline type definitions
        pat = _TYPE_RE.get(lang)
        if pat:
            text = ent.get("body_preview", "") or ""
            for m in pat.finditer(text):
                name = m.group(1)
                canonical = _singularize(name)
                if canonical not in _SKIP_TYPE_NAMES and len(name) >= 3:
                    type_uses.setdefault(canonical, []).append({
                        "language": lang,
                        "file": ent.get("file_path", ""),
                        "name": name,
                    })

    bridges = []
    for canonical, uses in type_uses.items():
        # Deduplicate by (language, file)
        seen_pairs: set[tuple] = set()
        deduped = []
        for u in uses:
            k = (u["language"], u["file"])
            if k not in seen_pairs:
                seen_pairs.add(k)
                deduped.append(u)

        langs = {u["language"] for u in deduped}
        if len(langs) < 2:
            continue

        files = sorted({u["file"] for u in deduped})
        bid = _bridge_id("type", canonical, project_id)
        display = deduped[0]["name"]
        b = _base_bridge(
            bid=bid,
            bridge_type="shared_type",
            project_id=project_id,
            name=f"TYPE:{display}",
            language=sorted(langs)[0],
            file_path=files[0],
            signature=f"{display} defined in {', '.join(sorted(langs))}",
            confidence="heuristic",
            provenance="ast",
        )
        b.update({
            "type_name": display,
            "canonical_name": canonical,
            "languages": sorted(langs),
            "files": files,
        })
        bridges.append(b)
    return bridges


# ── Public API ─────────────────────────────────────────────────────────────────

def resolve_all_bridges(entities: list[dict], project_id: str) -> list[dict]:
    """
    Run all five bridge detectors against a parsed entity list.
    Returns flat list of bridge documents without writing to Meilisearch.
    Call index_documents(project_id, bridges) to persist.
    """
    out: list[dict] = []
    out.extend(detect_http_bridges(entities, project_id))
    out.extend(detect_env_bridges(entities, project_id))
    out.extend(detect_config_bridges(entities, project_id))
    out.extend(detect_db_bridges(entities, project_id))
    out.extend(detect_type_bridges(entities, project_id))
    return out


def find_bridge_callers(project_id: str, function_name: str) -> list[dict]:
    """
    Query Meilisearch for HTTP bridge documents where target_function matches.
    Used by impact.py to follow cross-language edges.
    Returns empty list if Meilisearch is unavailable.
    """
    try:
        from ingest.indexer import search
        safe = function_name.replace("'", "\\'")
        return search(
            project_id,
            function_name,
            filters=f"kind = 'bridge' AND bridge_type = 'http' AND target_function = '{safe}'",
            limit=50,
        )
    except Exception:
        return []
