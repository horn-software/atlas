"""
Route graph builder.

Parses route registration sites across Python, JavaScript/TypeScript, Go, and Ruby
and emits structured `route_edge` documents for indexing.

Edge schema (kind = "route_edge"):
  route_path    — normalised URL path (e.g. "/api/users/:param")
  http_method   — GET / POST / PUT / PATCH / DELETE / *
  handler_name  — bare function/method name of the handler
  handler_qname — qualified name of the handler
  handler_file  — file path of the handler
  handler_id    — entity ID of the handler (if resolved)
  middleware    — list of middleware/auth wrapper names detected
  confidence    — "exact" (decorator/registration parsed) | "heuristic" (route string inferred)
  provenance    — "ast"
"""
import hashlib
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Normalisation helpers ─────────────────────────────────────────────────────

def _norm_route(path: str) -> str:
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


def _is_route_path(path: str) -> bool:
    if not path.startswith("/") or path in ("/", ""):
        return False
    first_seg = path.split("/")[1] if "/" in path[1:] else path[1:]
    return "." not in first_seg


def _route_id(*parts: str) -> str:
    return "route_" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:28]


def _http_method_from_match(text: str) -> str:
    t = text.lower()
    if "post" in t:
        return "POST"
    if "put" in t:
        return "PUT"
    if "delete" in t:
        return "DELETE"
    if "patch" in t:
        return "PATCH"
    if "head" in t:
        return "HEAD"
    if "options" in t:
        return "OPTIONS"
    return "GET"


# ── Route registration patterns ───────────────────────────────────────────────

_ROUTE_RE: dict[str, list] = {
    "python": [
        re.compile(r"""@[\w.]+\.(?:route|get|post|put|patch|delete|head|options)\s*\(\s*['"`]([^'"`]+)""", re.I),
        re.compile(r"""(?:path|re_path|url)\s*\(\s*r?['"`]([^'"`\^$]+)['"`]""", re.I),
        re.compile(r"""@(?:app|router|blueprint)\s*\.\s*(?:get|post|put|patch|delete|route)\s*\(\s*['"`]([^'"`]+)""", re.I),
    ],
    "javascript": [
        re.compile(r"""(?:app|router|server)\s*\.\s*(?:get|post|put|patch|delete|use|all)\s*\(\s*['"`]([^'"`]+)""", re.I),
        re.compile(r"""fastify\s*\.\s*(?:get|post|put|patch|delete|route)\s*\(\s*['"`]([^'"`]+)""", re.I),
        re.compile(r"""\.(?:get|post|put|patch|delete)\s*\(\s*`([^`?{]+)""", re.I),
    ],
    "typescript": [
        re.compile(r"""(?:app|router|server)\s*\.\s*(?:get|post|put|patch|delete|use|all)\s*\(\s*['"`]([^'"`]+)""", re.I),
        re.compile(r"""@(?:Get|Post|Put|Patch|Delete|Controller)\s*\(\s*['"`]([^'"`]*)['"`]"""),
    ],
    "go": [
        re.compile(r"""(?:http\.HandleFunc|mux\.HandleFunc|r\.HandleFunc|router\.HandleFunc)\s*\(\s*"([^"]+)"""),
        re.compile(r"""(?:r|router|mux)\s*\.\s*(?:GET|POST|PUT|PATCH|DELETE|Handle)\s*\(\s*"([^"]+)"""),
    ],
    "ruby": [
        re.compile(r"""(?:^|\s)(?:get|post|put|patch|delete)\s+['"]([^'"]+)['"]""", re.M | re.I),
        re.compile(r"""resources\s+:(\w+)""", re.I),
    ],
}


_MIDDLEWARE_RE = re.compile(
    r"(?:middleware|use|before_action|guard|protect|require)\s*\(\s*['\"]([^'\"]+)['\"]", re.I
)
_AUTH_DECORATOR_RE = re.compile(
    r"@(?:UseGuards|Middleware|login_required|require_http_methods|permission_required)\b", re.I
)


def _extract_middleware(text: str) -> list[str]:
    seen: set[str] = set()
    found: list[str] = []
    for m in _MIDDLEWARE_RE.finditer(text):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            found.append(name)
    for m in _AUTH_DECORATOR_RE.finditer(text):
        name = m.group(0).lstrip("@")
        if name not in seen:
            seen.add(name)
            found.append(name)
    return found


# ── Public builder ────────────────────────────────────────────────────────────

def build_route_edges(entities: list[dict], project_id: str) -> list[dict]:
    """
    Parse route registration patterns from entity body previews and decorators.

    Returns a list of `route_edge` documents ready for indexing.
    Each document captures the route path, HTTP method, handler function,
    middleware, and provenance.
    """
    # Build a quick name→entity lookup for handler ID resolution
    name_to_id: dict[str, str] = {}
    name_to_qname: dict[str, str] = {}
    for ent in entities:
        name = ent.get("name") or ""
        eid = ent.get("id") or ""
        qname = ent.get("qualified_name") or ""
        if name and eid:
            name_to_id[name] = eid
        if name and qname:
            name_to_qname[name] = qname

    edges: list[dict] = []
    seen_ids: set[str] = set()

    for ent in entities:
        lang = ent.get("language", "")
        pats = _ROUTE_RE.get(lang, [])
        if not pats:
            continue

        text = "\n".join([
            ent.get("body_preview") or "",
            ent.get("signature") or "",
            "\n".join(ent.get("decorators") or []),
        ])
        if not text.strip():
            continue

        handler_name = ent.get("name") or ""
        handler_file = ent.get("file_path") or ""
        handler_id = name_to_id.get(handler_name, ent.get("id") or "")
        handler_qname = name_to_qname.get(handler_name, ent.get("qualified_name") or "")
        middleware = _extract_middleware(text)

        for pat in pats:
            for m in pat.finditer(text):
                raw_path = m.group(1)
                route_path = _norm_route(raw_path)
                if not _is_route_path(route_path):
                    continue

                http_method = _http_method_from_match(m.group(0))
                edge_id = _route_id(project_id, handler_file, route_path, http_method)
                if edge_id in seen_ids:
                    continue
                seen_ids.add(edge_id)

                edges.append({
                    "id": edge_id,
                    "kind": "route_edge",
                    "name": f"{http_method} {route_path}",
                    "route_path": route_path,
                    "http_method": http_method,
                    "handler_name": handler_name,
                    "handler_qname": handler_qname,
                    "handler_file": handler_file,
                    "handler_id": handler_id,
                    "middleware": middleware,
                    "language": lang,
                    "file_path": handler_file,
                    "line_start": ent.get("line_start", 0),
                    "line_end": ent.get("line_end", 0),
                    "signature": f"{http_method} {route_path} → {handler_name}",
                    "docstring": "",
                    "body_preview": "",
                    "calls": [],
                    "called_by": [],
                    "called_by_ids": [],
                    "imports": [],
                    "parent_class": "",
                    "decorators": list(ent.get("decorators") or []),
                    "project_id": project_id,
                    "confidence": "exact",
                    "provenance": "ast",
                    "evidence": [{"file": handler_file, "line": ent.get("line_start", 0)}],
                })

    return edges
