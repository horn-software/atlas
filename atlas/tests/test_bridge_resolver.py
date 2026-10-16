"""
Tests for the cross-language bridge resolver.
Pure unit tests — no Meilisearch required, all detection tested in-process.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.bridge_resolver import (
    detect_http_bridges,
    detect_env_bridges,
    detect_config_bridges,
    detect_db_bridges,
    detect_type_bridges,
    resolve_all_bridges,
)

PROJECT_ID = "test-bridge-project"

# ── Synthetic mixed-project entity set ────────────────────────────────────────
# Simulates a Python FastAPI backend + TypeScript React frontend

PYTHON_FILE_ENT = {
    "id": "py_file_1",
    "kind": "file",
    "name": "main.py",
    "file_path": "api/main.py",
    "language": "python",
    "line_start": 1, "line_end": 60,
    "signature": "", "docstring": "",
    "body_preview": (
        "from fastapi import FastAPI\nimport os\n\n"
        "app = FastAPI()\n\n"
        "DATABASE_URL = os.getenv('DATABASE_URL')\n"
        "SECRET_KEY = os.environ.get('SECRET_KEY')\n"
    ),
    "calls": [], "called_by": [], "imports": ["fastapi", "os"],
    "parent_class": "", "decorators": [],
}

PYTHON_LOGIN_ENT = {
    "id": "py_fn_login",
    "kind": "function",
    "name": "login",
    "file_path": "api/main.py",
    "language": "python",
    "line_start": 15, "line_end": 25,
    "signature": "login(data: LoginData)",
    "docstring": "Authenticate user and return access token.",
    "body_preview": (
        "    secret = os.environ.get('SECRET_KEY')\n"
        "    # verify credentials\n"
        "    return {'token': 'abc'}"
    ),
    "calls": [], "called_by": [],
    "imports": [], "parent_class": "",
    "decorators": ["@app.post('/api/login')"],
}

PYTHON_USERS_ENT = {
    "id": "py_fn_users",
    "kind": "function",
    "name": "get_users",
    "file_path": "api/main.py",
    "language": "python",
    "line_start": 27, "line_end": 35,
    "signature": "get_users()",
    "docstring": "Return all users.",
    "body_preview": "    return db.query(User).all()",
    "calls": [], "called_by": [],
    "imports": [], "parent_class": "",
    "decorators": ["@app.get('/api/users')"],
}

PYTHON_USER_MODEL = {
    "id": "py_cls_user",
    "kind": "class",
    "name": "User",
    "file_path": "api/models.py",
    "language": "python",
    "line_start": 5, "line_end": 15,
    "signature": "User(BaseModel)",
    "docstring": "User model.",
    "body_preview": "class User(BaseModel):\n    id: int\n    name: str\n    email: str",
    "calls": [], "called_by": [],
    "imports": [], "parent_class": "",
    "decorators": [],
}

TS_FILE_ENT = {
    "id": "ts_file_1",
    "kind": "file",
    "name": "auth.ts",
    "file_path": "frontend/src/auth.ts",
    "language": "typescript",
    "line_start": 1, "line_end": 40,
    "signature": "", "docstring": "",
    "body_preview": (
        "import axios from 'axios';\n\n"
        "const SECRET_KEY = process.env.SECRET_KEY;\n"
        "const DB_URL = process.env.DATABASE_URL;\n"
    ),
    "calls": [], "called_by": [], "imports": ["axios"],
    "parent_class": "", "decorators": [],
}

TS_LOGIN_FN = {
    "id": "ts_fn_login",
    "kind": "function",
    "name": "loginUser",
    "file_path": "frontend/src/auth.ts",
    "language": "typescript",
    "line_start": 10, "line_end": 20,
    "signature": "loginUser(credentials: LoginData): Promise<User>",
    "docstring": "Call the Python login endpoint.",
    "body_preview": (
        "    const resp = await fetch('/api/login', {\n"
        "        method: 'POST',\n"
        "        body: JSON.stringify(credentials),\n"
        "    });\n"
        "    return resp.json();"
    ),
    "calls": [], "called_by": [],
    "imports": [], "parent_class": "",
    "decorators": [],
}

TS_USERS_FN = {
    "id": "ts_fn_users",
    "kind": "function",
    "name": "getUsers",
    "file_path": "frontend/src/auth.ts",
    "language": "typescript",
    "line_start": 22, "line_end": 28,
    "signature": "getUsers(): Promise<User[]>",
    "docstring": "",
    "body_preview": "    return axios.get('/api/users');",
    "calls": [], "called_by": [],
    "imports": [], "parent_class": "",
    "decorators": [],
}

TS_USER_INTERFACE = {
    "id": "ts_cls_user",
    "kind": "class",
    "name": "User",
    "file_path": "frontend/src/types.ts",
    "language": "typescript",
    "line_start": 1, "line_end": 6,
    "signature": "interface User",
    "docstring": "",
    "body_preview": "interface User {\n    id: number;\n    name: string;\n    email: string;\n}",
    "calls": [], "called_by": [],
    "imports": [], "parent_class": "",
    "decorators": [],
}

ALL_ENTITIES = [
    PYTHON_FILE_ENT, PYTHON_LOGIN_ENT, PYTHON_USERS_ENT, PYTHON_USER_MODEL,
    TS_FILE_ENT, TS_LOGIN_FN, TS_USERS_FN, TS_USER_INTERFACE,
]


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_http_bridge_login():
    """TS fetch('/api/login') should bridge to Python @app.post('/api/login')."""
    bridges = detect_http_bridges(ALL_ENTITIES, PROJECT_ID)
    assert bridges, "No HTTP bridges found"

    http = [b for b in bridges if b["bridge_type"] == "http"]
    paths = [b["route_path"] for b in http]
    assert "/api/login" in paths, f"Login route not bridged: {paths}"

    b = next(b for b in http if b["route_path"] == "/api/login")
    assert b["caller_language"] == "typescript"
    assert b["target_language"] == "python"
    assert b["caller_function"] == "loginUser"
    assert b["target_function"] == "login"
    assert b["confidence"] == "exact"
    print("  PASS test_http_bridge_login")


def test_http_bridge_users():
    """TS axios.get('/api/users') should bridge to Python @app.get('/api/users')."""
    bridges = detect_http_bridges(ALL_ENTITIES, PROJECT_ID)
    paths = [b["route_path"] for b in bridges if b["bridge_type"] == "http"]
    assert "/api/users" in paths, f"/api/users not bridged: {paths}"
    print("  PASS test_http_bridge_users")


def test_env_bridge_secret_key():
    """SECRET_KEY used in both Python and TypeScript should produce an env bridge."""
    bridges = detect_env_bridges(ALL_ENTITIES, PROJECT_ID)
    assert bridges, "No ENV bridges found"

    var_names = [b["variable_name"] for b in bridges]
    assert "SECRET_KEY" in var_names, f"SECRET_KEY not bridged: {var_names}"

    b = next(b for b in bridges if b["variable_name"] == "SECRET_KEY")
    assert "python" in b["languages"]
    assert "typescript" in b["languages"]
    assert b["confidence"] == "exact"
    print("  PASS test_env_bridge_secret_key")


def test_env_bridge_database_url():
    """DATABASE_URL used in both Python and TypeScript should produce an env bridge."""
    bridges = detect_env_bridges(ALL_ENTITIES, PROJECT_ID)
    var_names = [b["variable_name"] for b in bridges]
    assert "DATABASE_URL" in var_names, f"DATABASE_URL not bridged: {var_names}"
    print("  PASS test_env_bridge_database_url")


def test_type_bridge_user():
    """User class (Python BaseModel) + User interface (TypeScript) → heuristic bridge."""
    bridges = detect_type_bridges(ALL_ENTITIES, PROJECT_ID)
    assert bridges, "No type bridges found"

    canonicals = [b.get("canonical_name", "").lower() for b in bridges]
    assert "user" in canonicals, f"User type not bridged: {canonicals}"

    b = next(b for b in bridges if b.get("canonical_name", "").lower() == "user")
    assert "python" in b["languages"]
    assert "typescript" in b["languages"]
    assert b["confidence"] == "heuristic"
    print("  PASS test_type_bridge_user")


def test_no_same_file_http_bridge():
    """HTTP caller and route in the same file must not produce a bridge."""
    same_file = [
        {**PYTHON_FILE_ENT, "body_preview": "requests.get('/api/internal')"},
        {**PYTHON_LOGIN_ENT, "decorators": ["@app.get('/api/internal')"]},
    ]
    bridges = detect_http_bridges(same_file, PROJECT_ID)
    assert not bridges, f"Same-file bridge should not be created: {bridges}"
    print("  PASS test_no_same_file_http_bridge")


def test_bridge_schema():
    """Every bridge document must carry all entity-compatible required fields."""
    required = {
        "id", "kind", "bridge_type", "name", "language", "file_path",
        "signature", "docstring", "body_preview", "calls", "called_by",
        "imports", "parent_class", "decorators", "line_start", "line_end",
        "confidence",
    }
    bridges = resolve_all_bridges(ALL_ENTITIES, PROJECT_ID)
    assert bridges, "resolve_all_bridges returned nothing"
    for b in bridges:
        missing = required - set(b.keys())
        assert not missing, f"Bridge {b.get('id')} missing fields: {missing}"
        assert b["kind"] == "bridge", f"kind should be 'bridge': {b['kind']}"
    print("  PASS test_bridge_schema")


def test_resolve_all_returns_multiple_types():
    """resolve_all_bridges should return HTTP, ENV, and type bridges for this fixture."""
    bridges = resolve_all_bridges(ALL_ENTITIES, PROJECT_ID)
    types_found = {b["bridge_type"] for b in bridges}
    assert "http" in types_found, f"Missing http: {types_found}"
    assert "env_var" in types_found, f"Missing env_var: {types_found}"
    assert "shared_type" in types_found, f"Missing shared_type: {types_found}"
    print("  PASS test_resolve_all_returns_multiple_types")


def test_no_duplicate_bridge_ids():
    """Each bridge in the output must have a unique id."""
    bridges = resolve_all_bridges(ALL_ENTITIES, PROJECT_ID)
    ids = [b["id"] for b in bridges]
    assert len(ids) == len(set(ids)), f"Duplicate bridge ids: {ids}"
    print("  PASS test_no_duplicate_bridge_ids")


if __name__ == "__main__":
    print("Running bridge resolver tests...")
    test_http_bridge_login()
    test_http_bridge_users()
    test_env_bridge_secret_key()
    test_env_bridge_database_url()
    test_type_bridge_user()
    test_no_same_file_http_bridge()
    test_bridge_schema()
    test_resolve_all_returns_multiple_types()
    test_no_duplicate_bridge_ids()
    print("\nAll bridge resolver tests passed.")
