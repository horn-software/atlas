"""
Usage tracking — logs every Atlas tool call to ~/.atlas/usage.db (SQLite).
Runs silently; never raises, never blocks a tool call.
"""
import sqlite3
import time
from pathlib import Path

_DB_PATH = Path.home() / ".atlas" / "usage.db"

_CREATE = """
CREATE TABLE IF NOT EXISTS tool_calls (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL    NOT NULL,
    tool      TEXT    NOT NULL,
    project   TEXT,
    tier      TEXT
);
CREATE TABLE IF NOT EXISTS projects (
    key_hash  TEXT    NOT NULL,
    project   TEXT    NOT NULL,
    first_seen REAL   NOT NULL,
    PRIMARY KEY (key_hash, project)
);
"""


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(_DB_PATH))
    c.executescript(_CREATE)
    return c


def log_call(tool: str, project_id: str = None, tier: str = "free"):
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO tool_calls (ts, tool, project, tier) VALUES (?,?,?,?)",
                (time.time(), tool, project_id, tier),
            )
    except Exception:
        pass


def record_project(key_hash: str, project_id: str):
    """Record that this key has ingested this project. Returns current project count."""
    try:
        with _conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO projects (key_hash, project, first_seen) VALUES (?,?,?)",
                (key_hash, project_id, time.time()),
            )
            row = c.execute(
                "SELECT COUNT(*) FROM projects WHERE key_hash=?", (key_hash,)
            ).fetchone()
            return row[0] if row else 1
    except Exception:
        return 1


def project_count(key_hash: str) -> int:
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM projects WHERE key_hash=?", (key_hash,)
            ).fetchone()
            return row[0] if row else 0
    except Exception:
        return 0
