"""
Env/config/deployment provenance graph builder.

Parses env-var accesses, .env file declarations, config file reads, and
settings accesses. Emits structured `config_edge` documents for indexing.

Edge schema (kind = "config_edge"):
  config_type   — "env_var" | "config_key" | "settings_attr" | "dotenv_decl"
  config_key    — the variable / key / attribute name
  consumer_name — the function that accesses it
  consumer_id   — entity ID of the consumer function
  consumer_file — file path of the consumer
  source_file   — .env or config file that declares it (if found)
  confidence    — "exact" (static access) | "heuristic" (dynamic/inferred)
  provenance    — "ast" | "file_scan"
"""
import hashlib
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _config_id(*parts: str) -> str:
    return "cfg_" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:28]


# ── Access patterns ───────────────────────────────────────────────────────────

_ENV_ACCESS: dict[str, list] = {
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
_ENV_ACCESS["typescript"] = _ENV_ACCESS["javascript"]

_CONFIG_KEY_ACCESS: dict[str, list] = {
    "python": [
        re.compile(r"""config\s*\[\s*['"`]([A-Za-z_][A-Za-z0-9_.]+)['"`]\s*\]"""),
        re.compile(r"""settings\s*\.\s*([A-Z][A-Z0-9_]+)\b"""),
        re.compile(r"""app\.config\s*\[\s*['"`]([^'"`]+)['"`]\s*\]"""),
        re.compile(r"""current_app\.config\s*\[\s*['"`]([^'"`]+)['"`]\s*\]"""),
        re.compile(r"""(?:get_from_config|config\.get)\s*\(\s*['"`]([^'"`]+)['"`]"""),
    ],
    "javascript": [
        re.compile(r"""config\s*\.\s*get\s*\(\s*['"`]([^'"`]+)['"`]"""),
        re.compile(r"""process\.env\.([A-Za-z_][A-Za-z0-9_]*)"""),
    ],
    "typescript": [],
}
_CONFIG_KEY_ACCESS["typescript"] = _CONFIG_KEY_ACCESS["javascript"]

_DOTENV_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)?$")

# File extensions → language name for file-level scanning
_EXT_LANG: dict[str, str] = {
    ".py": "python", ".pyw": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go",
    ".rb": "ruby",
    ".rs": "rust",
}

# Directories to skip when scanning source files
_SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build"}


# ── .env and config file scanner ──────────────────────────────────────────────

def _scan_dotenv_files(root_path: str) -> dict[str, str]:
    """Scan for .env files and return {key: source_file} mapping."""
    declarations: dict[str, str] = {}
    root = Path(root_path)
    for env_file in root.rglob(".env*"):
        if env_file.is_file() and env_file.stat().st_size < 1_000_000:
            try:
                for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    m = _DOTENV_LINE_RE.match(line)
                    if m:
                        key = m.group(1)
                        declarations[key] = str(env_file)
            except OSError:
                pass
    return declarations


# ── File-level source scanner ─────────────────────────────────────────────────

def _scan_source_files(
    root_path: str,
    project_id: str,
    dotenv_sources: dict[str, str],
    seen_ids: set[str],
) -> list[dict]:
    """
    Walk source files and emit config_edge docs for env-var accesses that fall
    outside any function body (module-level assignments, top-level constants).

    We scan every source line; edges already emitted by entity-level scanning
    (same id) are skipped via seen_ids.
    """
    edges: list[dict] = []
    root = Path(root_path)

    for src_file in root.rglob("*"):
        if not src_file.is_file():
            continue
        # Skip hidden / vendor dirs
        if any(part in _SKIP_DIRS or part.startswith(".") for part in src_file.parts):
            continue
        lang = _EXT_LANG.get(src_file.suffix.lower())
        if not lang:
            continue

        try:
            text = src_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        rel_path = str(src_file.relative_to(root)).replace("\\", "/")
        patterns = _ENV_ACCESS.get(lang, [])
        if not patterns:
            continue

        for lineno, line in enumerate(text.splitlines(), start=1):
            for pat in patterns:
                for m in pat.finditer(line):
                    key = m.group(1)
                    if len(key) < 2:
                        continue
                    # Use "<module>" as consumer for non-function scope
                    edge_id = _config_id(project_id, rel_path, "<module>", "env_var", key)
                    if edge_id in seen_ids:
                        continue
                    seen_ids.add(edge_id)
                    source_file = dotenv_sources.get(key, "")
                    edges.append({
                        "id": edge_id,
                        "kind": "config_edge",
                        "name": f"ENV:{key}",
                        "config_type": "env_var",
                        "config_key": key,
                        "consumer_name": "<module>",
                        "consumer_id": "",
                        "consumer_file": rel_path,
                        "source_file": source_file,
                        "language": lang,
                        "file_path": rel_path,
                        "line_start": lineno,
                        "line_end": lineno,
                        "signature": f"ENV:{key} → {rel_path}:{lineno}",
                        "docstring": "",
                        "body_preview": line.strip(),
                        "calls": [],
                        "called_by": [],
                        "called_by_ids": [],
                        "imports": [],
                        "parent_class": "",
                        "decorators": [],
                        "project_id": project_id,
                        "confidence": "exact",
                        "provenance": "file_scan",
                        "evidence": [{"file": rel_path, "line": lineno}],
                    })
    return edges


# ── Public builder ────────────────────────────────────────────────────────────

def build_config_edges(entities: list[dict], project_id: str,
                       root_path: str = "") -> list[dict]:
    """
    Parse env-var and config-key accesses from entity body previews.

    Also scans .env files in root_path for declaration provenance.

    Returns a list of `config_edge` documents ready for indexing.

    Two-pass strategy:
    1. Entity pass  — scans body_preview of indexed functions/methods (exact line attribution).
    2. File-level pass — scans raw source files so module-level accesses (top-level
       constants like MEILI_URL = os.environ.get(...)) are never missed.

    Duplicate suppression: both passes share seen_ids so the same (file, key) pair
    is only emitted once.
    """
    # Scan for .env declarations
    dotenv_sources: dict[str, str] = {}
    if root_path:
        dotenv_sources = _scan_dotenv_files(root_path)

    edges: list[dict] = []
    seen_ids: set[str] = set()

    for ent in entities:
        if ent.get("kind") not in ("function", "method", "file"):
            continue

        lang = ent.get("language", "")
        text = "\n".join([
            ent.get("body_preview") or "",
            ent.get("signature") or "",
        ])
        if not text.strip():
            continue

        consumer_name = ent.get("name") or ""
        consumer_file = ent.get("file_path") or ""
        consumer_id = ent.get("id") or ""

        # Env-var accesses
        for pat in _ENV_ACCESS.get(lang, []):
            for m in pat.finditer(text):
                key = m.group(1)
                if len(key) < 2:
                    continue
                source_file = dotenv_sources.get(key, "")
                edge_id = _config_id(project_id, consumer_file, consumer_name, "env_var", key)
                if edge_id in seen_ids:
                    continue
                seen_ids.add(edge_id)
                edges.append({
                    "id": edge_id,
                    "kind": "config_edge",
                    "name": f"ENV:{key}",
                    "config_type": "env_var",
                    "config_key": key,
                    "consumer_name": consumer_name,
                    "consumer_id": consumer_id,
                    "consumer_file": consumer_file,
                    "source_file": source_file,
                    "language": lang,
                    "file_path": consumer_file,
                    "line_start": ent.get("line_start", 0),
                    "line_end": ent.get("line_end", 0),
                    "signature": f"ENV:{key} → {consumer_name}",
                    "docstring": "",
                    "body_preview": "",
                    "calls": [],
                    "called_by": [],
                    "called_by_ids": [],
                    "imports": [],
                    "parent_class": "",
                    "decorators": [],
                    "project_id": project_id,
                    "confidence": "exact",
                    "provenance": "ast",
                    "evidence": [{"file": consumer_file, "line": ent.get("line_start", 0)}],
                })

        # Config-key accesses
        for pat in _CONFIG_KEY_ACCESS.get(lang, []):
            for m in pat.finditer(text):
                key = m.group(1)
                if len(key) < 2:
                    continue
                edge_id = _config_id(project_id, consumer_file, consumer_name, "config_key", key)
                if edge_id in seen_ids:
                    continue
                seen_ids.add(edge_id)
                edges.append({
                    "id": edge_id,
                    "kind": "config_edge",
                    "name": f"CONFIG:{key}",
                    "config_type": "config_key",
                    "config_key": key,
                    "consumer_name": consumer_name,
                    "consumer_id": consumer_id,
                    "consumer_file": consumer_file,
                    "source_file": "",
                    "language": lang,
                    "file_path": consumer_file,
                    "line_start": ent.get("line_start", 0),
                    "line_end": ent.get("line_end", 0),
                    "signature": f"CONFIG:{key} → {consumer_name}",
                    "docstring": "",
                    "body_preview": "",
                    "calls": [],
                    "called_by": [],
                    "called_by_ids": [],
                    "imports": [],
                    "parent_class": "",
                    "decorators": [],
                    "project_id": project_id,
                    "confidence": "exact",
                    "provenance": "ast",
                    "evidence": [{"file": consumer_file, "line": ent.get("line_start", 0)}],
                })

    # Pass 2: file-level scan for module-level accesses not covered by entity bodies
    if root_path:
        edges.extend(_scan_source_files(root_path, project_id, dotenv_sources, seen_ids))

    return edges
