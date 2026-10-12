"""
Write structured entity documents to a Meilisearch index.
One index per codebase: code_{project_id}.
"""
import os
import time

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
except ImportError:
    pass

try:
    import requests as _requests
except ImportError:
    _requests = None

MEILI_URL = os.environ.get("MEILI_URL", "http://localhost:7700")
MEILI_KEY = os.environ.get("MEILI_KEY", "")

INDEX_SETTINGS = {
    "searchableAttributes": [
        "name", "signature", "docstring",
        "body_preview", "calls", "called_by",
        "imports", "file_path",
        "route_path", "variable_name",
    ],
    "filterableAttributes": [
        "kind", "name", "file_path", "language",
        "line_start", "calls", "called_by",
        "imports", "parent_class", "decorators",
        "bridge_type", "route_path", "caller_language",
        "target_language", "variable_name", "target_function",
    ],
    "sortableAttributes": ["file_path", "line_start"],
}


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if MEILI_KEY:
        h["Authorization"] = f"Bearer {MEILI_KEY}"
    return h


def _index_name(project_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in project_id)
    return f"code_{safe}"


def create_index(project_id: str) -> str:
    """Create a Meilisearch index for a project. Returns the index name."""
    if _requests is None:
        raise RuntimeError("requests library not installed")

    idx = _index_name(project_id)
    url = f"{MEILI_URL}/indexes"

    resp = _requests.post(
        url,
        json={"uid": idx, "primaryKey": "id"},
        headers=_headers(),
        timeout=10,
    )
    if resp.status_code not in (200, 201, 202):
        if "already_exists" not in resp.text.lower() and "index_already_exists" not in resp.text.lower():
            resp.raise_for_status()

    _wait_for_task(resp)

    settings_url = f"{MEILI_URL}/indexes/{idx}/settings"
    resp = _requests.patch(
        settings_url,
        json=INDEX_SETTINGS,
        headers=_headers(),
        timeout=10,
    )
    _wait_for_task(resp)

    return idx


def _wait_for_task(resp, timeout: int = 30):
    """Wait for a Meilisearch async task to complete."""
    try:
        data = resp.json()
    except Exception:
        return

    task_uid = data.get("taskUid") or data.get("uid")
    if task_uid is None:
        return

    deadline = time.time() + timeout
    while time.time() < deadline:
        tr = _requests.get(
            f"{MEILI_URL}/tasks/{task_uid}",
            headers=_headers(),
            timeout=5,
        )
        status = tr.json().get("status", "")
        if status in ("succeeded", "failed"):
            return
        time.sleep(0.3)


def index_documents(project_id: str, documents: list[dict], batch_size: int = 500) -> int:
    """
    Index entity documents into the project's Meilisearch index.
    Returns total number of documents indexed.
    """
    if _requests is None:
        raise RuntimeError("requests library not installed")

    idx = _index_name(project_id)
    url = f"{MEILI_URL}/indexes/{idx}/documents"
    total = 0

    for i in range(0, len(documents), batch_size):
        batch = documents[i : i + batch_size]
        resp = _requests.post(
            url,
            json=batch,
            headers=_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        _wait_for_task(resp)
        total += len(batch)

    return total


def delete_index(project_id: str):
    """Delete a project's Meilisearch index."""
    if _requests is None:
        raise RuntimeError("requests library not installed")

    idx = _index_name(project_id)
    resp = _requests.delete(
        f"{MEILI_URL}/indexes/{idx}",
        headers=_headers(),
        timeout=10,
    )
    if resp.status_code != 404:
        resp.raise_for_status()
        _wait_for_task(resp)


def get_index_stats(project_id: str) -> dict:
    """Get stats for a project's index."""
    if _requests is None:
        raise RuntimeError("requests library not installed")

    idx = _index_name(project_id)
    resp = _requests.get(
        f"{MEILI_URL}/indexes/{idx}/stats",
        headers=_headers(),
        timeout=5,
    )
    if resp.status_code == 404:
        return {"exists": False, "numberOfDocuments": 0}
    resp.raise_for_status()
    data = resp.json()
    data["exists"] = True
    data["index_name"] = idx
    return data


def search(project_id: str, query: str, filters: str | None = None, limit: int = 10,
           min_score: float = 0.05) -> list[dict]:
    """Search a project's codebase index."""
    if _requests is None:
        raise RuntimeError("requests library not installed")

    idx = _index_name(project_id)
    url = f"{MEILI_URL}/indexes/{idx}/search"
    body: dict = {
        "q": query,
        "limit": limit,
        "showRankingScore": True,
    }
    if filters:
        body["filter"] = filters

    resp = _requests.post(url, json=body, headers=_headers(), timeout=10)
    resp.raise_for_status()
    hits = resp.json().get("hits", [])
    # Drop low-relevance hits; remove the internal score field before returning
    out = []
    for h in hits:
        score = h.pop("_rankingScore", 1.0)
        if score >= min_score:
            out.append(h)
    return out
