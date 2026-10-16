"""
Walk a codebase directory and find source files.
Supports 18 languages via tree-sitter.
Returns a list of absolute paths, skipping hidden dirs, node_modules, venvs, etc.
"""
import os
from pathlib import Path

SKIP_DIRS = {
    "__pycache__", ".git", ".hg", ".svn", "node_modules",
    "venv", ".venv", "env", ".env", ".tox", ".mypy_cache",
    ".pytest_cache", "dist", "build", "egg-info", "vendor",
    ".next",
}

_REPO_MARKERS = (
    ".git", ".hg", ".svn",
)


def _is_repo_root(path: Path) -> bool:
    """Return True when a directory looks like a repository root or copy."""
    for marker in _REPO_MARKERS:
        if (path / marker).exists():
            return True
    return False


def _is_nested_copy_dir(root: Path, dir_path: Path) -> bool:
    """Reject nested repo copies and duplicate worktrees under the same root."""
    if dir_path == root:
        return False

    try:
        relative = dir_path.relative_to(root)
    except ValueError:
        return False

    if not relative.parts:
        return False

    if relative.parts[0] == root.name:
        return True

    if _is_repo_root(dir_path):
        return True

    return False

SKIP_SUFFIXES = {".pyc", ".pyo", ".so", ".dll", ".exe"}

LANG_EXTENSIONS = {
    "python":     {".py"},
    "javascript": {".js"},
    "typescript": {".ts", ".tsx"},
    "go":         {".go"},
    "rust":       {".rs"},
    "java":       {".java"},
    "csharp":     {".cs"},
    "c":          {".c", ".h"},
    "cpp":        {".cpp", ".cc", ".cxx", ".hpp"},
    "ruby":       {".rb"},
    "php":        {".php"},
    "swift":      {".swift"},
    "kotlin":     {".kt", ".kts"},
    "scala":      {".scala"},
    "lua":        {".lua"},
    "elixir":     {".ex", ".exs"},
    "dart":       {".dart"},
    "zig":        {".zig"},
}


def crawl(root_path: str, languages: list[str] | None = None) -> list[str]:
    """
    Recursively find source files under root_path.
    Supports all 18 languages in LANG_EXTENSIONS.
    Returns sorted list of absolute paths.
    """
    if languages is None:
        languages = list(LANG_EXTENSIONS.keys())

    extensions = set()
    for lang in languages:
        extensions.update(LANG_EXTENSIONS.get(lang, set()))

    if not extensions:
        return []

    root = Path(root_path).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Root path does not exist: {root}")

    found = []
    seen_files = set()
    for dirpath, dirnames, filenames in os.walk(root):
        current_dir = Path(dirpath).resolve()
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS
            and not d.startswith(".")
            and not _is_nested_copy_dir(root, (current_dir / d).resolve())
        ]

        for fname in filenames:
            fpath = (Path(dirpath) / fname).resolve()
            if fpath.suffix in extensions and fpath.suffix not in SKIP_SUFFIXES:
                if fpath in seen_files:
                    continue
                seen_files.add(fpath)
                found.append(str(fpath))

    return sorted(found)
