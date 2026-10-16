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
        "route_path", "variable_name", "search_terms",
        "event_type", "handler_name",
    ],
    "filterableAttributes": [
        "id", "entity_id", "kind", "name", "qualified_name", "file_path", "language",
        "line_start", "calls", "called_by", "called_by_ids",
        "imports", "parent_class", "decorators",
        "bridge_type", "route_path", "caller_language",
        "target_language", "variable_name", "target_function",
        "confidence", "provenance",
        "repo_id", "event_type", "handler_name",
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
        batch = [
            _add_search_terms(document)
            for document in documents[i : i + batch_size]
        ]
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


def _add_search_terms(document: dict) -> dict:
    """Add identifier pieces and common aliases without mutating parser output."""
    from query.synonyms import searchable_terms

    searchable_fields = (
        "name", "signature", "docstring", "body_preview", "calls",
        "called_by", "imports", "file_path", "route_path", "variable_name",
    )
    text = " ".join(
        str(value)
        for field in searchable_fields
        if (value := document.get(field))
    )
    enriched = dict(document)
    enriched["search_terms"] = searchable_terms(text)
    return enriched


def delete_stale_documents(project_id: str, current_documents: list[dict],
                           batch_size: int = 500) -> int:
    """Remove indexed documents that are no longer present in the source tree."""
    if _requests is None:
        raise RuntimeError("requests library not installed")
    if not current_documents:
        return 0

    idx = _index_name(project_id)
    url = f"{MEILI_URL}/indexes/{idx}/documents"
    current_ids = {doc["id"] for doc in current_documents if doc.get("id") is not None}
    existing_ids = []
    offset = 0

    while True:
        resp = _requests.get(
            url,
            params={"fields": "id", "limit": batch_size, "offset": offset},
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        page = resp.json().get("results", [])
        existing_ids.extend(
            doc["id"] for doc in page if doc.get("id") is not None
        )
        if len(page) < batch_size:
            break
        offset += len(page)

    stale_ids = [doc_id for doc_id in existing_ids if doc_id not in current_ids]
    for i in range(0, len(stale_ids), batch_size):
        resp = _requests.post(
            f"{url}/delete-batch",
            json=stale_ids[i:i + batch_size],
            headers=_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        _wait_for_task(resp)

    return len(stale_ids)


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
    return _filter_hits(resp.json().get("hits", []), min_score)


def search_many(project_id: str, queries: list[str], filters: str | None = None,
                limit: int = 10, min_score: float = 0.05) -> list[list[dict]]:
    """Run independent searches in one Meilisearch multi-search request."""
    if _requests is None:
        raise RuntimeError("requests library not installed")
    if not queries:
        return []

    idx = _index_name(project_id)
    request_queries = []
    for query in queries:
        item = {
            "indexUid": idx,
            "q": query,
            "limit": limit,
            "showRankingScore": True,
        }
        if filters:
            item["filter"] = filters
        request_queries.append(item)

    resp = _requests.post(
        f"{MEILI_URL}/multi-search",
        json={"queries": request_queries},
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if len(results) != len(queries):
        raise RuntimeError(
            f"Meilisearch returned {len(results)} results for {len(queries)} queries"
        )
    return [_filter_hits(result.get("hits", []), min_score) for result in results]


def _filter_hits(hits: list[dict], min_score: float) -> list[dict]:
    """Remove low-relevance hits and their internal ranking score."""
    out = []
    for hit in hits:
        score = hit.pop("_rankingScore", 1.0)
        if score >= min_score:
            out.append(hit)
    return out
