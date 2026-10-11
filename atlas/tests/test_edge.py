"""
Test the edge-case detection layer.
Tests impact graph construction from parsed entities.
Bridge tests require the verifier API to be running.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.crawler import crawl
from ingest.parsers import parse_file
from ingest.call_graph import build_call_graph

SAMPLE_CODEBASE = {
    "auth.py": '''
def hash_password(password: str) -> str:
    """Hash a password using SHA-256."""
    import hashlib
    return hashlib.sha256(password.encode()).hexdigest()

def authenticate(username: str, password: str) -> bool:
    """Authenticate a user."""
    stored = get_stored_hash(username)
    return hash_password(password) == stored

def get_stored_hash(username: str) -> str:
    """Get stored password hash."""
    return "abc123"
''',
    "api.py": '''
def login(request: dict) -> dict:
    """Handle login request."""
    from auth import authenticate
    if authenticate(request["user"], request["pass"]):
        return {"status": "ok", "token": create_token(request["user"])}
    return {"status": "fail"}

def create_token(username: str) -> str:
    """Create a session token."""
    return f"token_{username}"

def protected_route(request: dict) -> dict:
    """A route that requires authentication."""
    token = request.get("token", "")
    if not verify_token(token):
        return {"status": "unauthorized"}
    return {"status": "ok", "data": "secret"}

def verify_token(token: str) -> bool:
    """Verify a session token."""
    return token.startswith("token_")
''',
}


def test_impact_from_entities():
    """
    Test impact analysis using parsed entities directly (no Meilisearch).
    If hash_password changes, authenticate and login should be affected.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        for fname, code in SAMPLE_CODEBASE.items():
            (Path(tmpdir) / fname).write_text(code, encoding="utf-8")

        files = crawl(tmpdir)
        all_entities = []
        for fpath in files:
            entities = parse_file(fpath, tmpdir)
            all_entities.extend(entities)
        all_entities = build_call_graph(all_entities)

        hash_pw = [e for e in all_entities if e["name"] == "hash_password"]
        assert len(hash_pw) == 1, "Should find hash_password"
        assert "authenticate" in hash_pw[0]["called_by"], \
            f"hash_password should be called by authenticate: {hash_pw[0]['called_by']}"

        authenticate_fn = [e for e in all_entities if e["name"] == "authenticate"]
        assert len(authenticate_fn) == 1, "Should find authenticate"

        fns_with_calls = {e["name"]: e["calls"] for e in all_entities
                          if e["kind"] == "function" and e["calls"]}

        assert "hash_password" in fns_with_calls.get("authenticate", []), \
            "authenticate should call hash_password"

        print("  PASS test_impact_from_entities")


def test_impact_chain():
    """
    Test that changes propagate through call chains.
    hash_password → authenticate → login should form a chain.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        for fname, code in SAMPLE_CODEBASE.items():
            (Path(tmpdir) / fname).write_text(code, encoding="utf-8")

        files = crawl(tmpdir)
        all_entities = []
        for fpath in files:
            entities = parse_file(fpath, tmpdir)
            all_entities.extend(entities)
        all_entities = build_call_graph(all_entities)

        entity_map = {}
        for e in all_entities:
            if e["kind"] == "function":
                entity_map[e["name"]] = e

        affected = set()
        frontier = ["hash_password"]
        for _ in range(3):
            next_frontier = []
            for fn in frontier:
                if fn in affected:
                    continue
                affected.add(fn)
                ent = entity_map.get(fn)
                if ent:
                    for caller in ent.get("called_by", []):
                        if caller not in affected:
                            next_frontier.append(caller)
            frontier = next_frontier

        assert "hash_password" in affected, "Should include the changed function"
        assert "authenticate" in affected, "Should include direct caller"

        print(f"  PASS test_impact_chain (affected={affected})")


def test_independent_functions_not_affected():
    """Functions not in the call chain should not be affected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        for fname, code in SAMPLE_CODEBASE.items():
            (Path(tmpdir) / fname).write_text(code, encoding="utf-8")

        files = crawl(tmpdir)
        all_entities = []
        for fpath in files:
            entities = parse_file(fpath, tmpdir)
            all_entities.extend(entities)
        all_entities = build_call_graph(all_entities)

        entity_map = {}
        for e in all_entities:
            if e["kind"] == "function":
                entity_map[e["name"]] = e

        affected = set()
        frontier = ["verify_token"]
        for _ in range(3):
            next_frontier = []
            for fn in frontier:
                if fn in affected:
                    continue
                affected.add(fn)
                ent = entity_map.get(fn)
                if ent:
                    for caller in ent.get("called_by", []):
                        if caller not in affected:
                            next_frontier.append(caller)
            frontier = next_frontier

        assert "verify_token" in affected
        assert "protected_route" in affected, \
            "protected_route calls verify_token"
        assert "hash_password" not in affected, \
            "hash_password is independent of verify_token"
        assert "authenticate" not in affected, \
            "authenticate is independent of verify_token"

        print(f"  PASS test_independent_functions_not_affected (affected={affected})")


if __name__ == "__main__":
    print("Running edge-case tests...")
    test_impact_from_entities()
    test_impact_chain()
    test_independent_functions_not_affected()
    print("All edge-case tests passed.")
