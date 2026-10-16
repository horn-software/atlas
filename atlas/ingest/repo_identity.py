"""
Repository identity and monorepo boundary detection.

Provides:
  fingerprint_repo(path)  — stable repo_id from remote URL + root commit
  detect_package_edges(path, project_id)  — monorepo package→package dependency edges
  detect_duplicate_repos(root_path)  — warns when nested repo copies are found
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Repo fingerprinting ───────────────────────────────────────────────────────

def _git_remote_url(repo_path: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _git_root_commit(repo_path: str) -> str:
    """Return the SHA of the first commit (stable across clones)."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "rev-list", "--max-parents=0", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip().split("\n")[0] if result.returncode == 0 else ""
    except Exception:
        return ""


def fingerprint_repo(repo_path: str) -> str:
    """
    Return a stable 16-char hex repo_id.

    Derived from: remote origin URL (if present) + root commit SHA.
    Falls back to a hash of the absolute path when git is unavailable.
    """
    remote = _git_remote_url(repo_path)
    root_commit = _git_root_commit(repo_path)

    if remote or root_commit:
        raw = f"{remote}|{root_commit}"
    else:
        raw = str(Path(repo_path).resolve())

    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── Duplicate repo detection ──────────────────────────────────────────────────

def detect_duplicate_repos(root_path: str) -> list[dict]:
    """
    Walk root_path and find nested directories that look like copies of the
    same repository (same repo_id as root or as each other).

    Returns a list of warning dicts with keys: path, repo_id, duplicate_of.
    """
    root = Path(root_path).resolve()
    root_id = fingerprint_repo(str(root))

    warnings: list[dict] = []
    seen: dict[str, str] = {root_id: str(root)}

    _REPO_MARKERS = (
        ".git", "pyproject.toml", "setup.py", "package.json",
        "go.mod", "Cargo.toml",
    )

    for dirpath, dirnames, _ in os.walk(root):
        current = Path(dirpath).resolve()
        if current == root:
            continue
        is_repo = any((current / m).exists() for m in _REPO_MARKERS)
        if not is_repo:
            continue

        rid = fingerprint_repo(str(current))
        if rid in seen:
            warnings.append({
                "path": str(current),
                "repo_id": rid,
                "duplicate_of": seen[rid],
                "message": f"Nested repo copy detected at {current}; same repo_id as {seen[rid]}",
            })
        else:
            seen[rid] = str(current)

        # Don't recurse into detected nested repos
        dirnames[:] = []

    return warnings


# ── Monorepo / workspace package boundaries ───────────────────────────────────

def _read_pyproject_packages(path: Path) -> list[str]:
    """Extract workspace members from pyproject.toml."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"members\s*=\s*\[([^\]]+)\]", text)
        if m:
            return [s.strip().strip("'\"") for s in m.group(1).split(",") if s.strip().strip("'\"")]
    except OSError:
        pass
    return []


def _read_npm_workspaces(path: Path) -> list[str]:
    """Extract workspace globs from package.json."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ws = data.get("workspaces", [])
        if isinstance(ws, dict):
            ws = ws.get("packages", [])
        return ws if isinstance(ws, list) else []
    except Exception:
        return []


def _read_cargo_members(path: Path) -> list[str]:
    """Extract members from Cargo.toml workspace."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"\[workspace\].*?members\s*=\s*\[([^\]]+)\]", text, re.S)
        if m:
            return [s.strip().strip("'\"") for s in m.group(1).split(",") if s.strip().strip("'\"")]
    except OSError:
        pass
    return []


def _pkg_edge_id(*parts: str) -> str:
    return "pkg_" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:28]


def detect_package_edges(root_path: str, project_id: str) -> list[dict]:
    """
    Detect workspace / monorepo package boundaries and emit `package_edge` documents.

    Supports: pyproject.toml workspaces, npm workspaces, Cargo workspace members.
    """
    root = Path(root_path).resolve()
    packages: list[dict] = []  # {name, path, kind}
    edges: list[dict] = []

    # pyproject.toml workspaces
    for pyproject in root.glob("*/pyproject.toml"):
        members = _read_pyproject_packages(pyproject)
        for member in members:
            pkg_path = (pyproject.parent / member).resolve()
            if pkg_path.is_dir():
                packages.append({"name": member, "path": str(pkg_path), "kind": "python_package"})

    # npm workspaces
    for pkg_json in root.glob("*/package.json"):
        members = _read_npm_workspaces(pkg_json)
        for member in members:
            pkg_path = (pkg_json.parent / member).resolve()
            if pkg_path.is_dir():
                packages.append({"name": member, "path": str(pkg_path), "kind": "npm_package"})

    # Cargo workspace
    cargo_toml = root / "Cargo.toml"
    if cargo_toml.exists():
        members = _read_cargo_members(cargo_toml)
        for member in members:
            pkg_path = (root / member).resolve()
            if pkg_path.is_dir():
                packages.append({"name": member, "path": str(pkg_path), "kind": "rust_crate"})

    # Emit package node documents
    for pkg in packages:
        pid = _pkg_edge_id(project_id, pkg["path"])
        edges.append({
            "id": pid,
            "kind": "package_node",
            "name": pkg["name"],
            "file_path": pkg["path"],
            "language": pkg["kind"].split("_")[0],
            "package_kind": pkg["kind"],
            "line_start": 0,
            "line_end": 0,
            "signature": f"package:{pkg['name']}",
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
            "provenance": "file_scan",
            "evidence": [{"file": pkg["path"], "line": 0}],
        })

    return edges


# ── Convenience: attach repo_id to all entities ───────────────────────────────

def annotate_entities(entities: list[dict], repo_id: str) -> list[dict]:
    """Add repo_id field to every entity document (in-place)."""
    for ent in entities:
        ent["repo_id"] = repo_id
    return entities
