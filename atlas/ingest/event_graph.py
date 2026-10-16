"""
Event, callback, and scheduler graph builder.

Parses event listener registrations, Celery tasks, APScheduler jobs, queue
consumer registrations, and lifecycle hooks across Python, JavaScript/TypeScript,
Go, and Ruby. Emits structured `event_edge` documents for indexing.

Edge schema (kind = "event_edge"):
  event_type    — "listener" | "celery_task" | "scheduler_job" | "queue_consumer"
                  | "lifecycle_hook" | "signal" | "pubsub"
  event_name    — the event / queue / topic / signal name (if statically known)
  handler_name  — function/method that handles the event
  handler_id    — entity ID of the handler (if resolved)
  handler_file  — file path of the handler
  confidence    — "exact" for decorator-based; "probable" for pattern-matched
  provenance    — "ast"
"""
import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _event_id(*parts: str) -> str:
    return "event_" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:28]


# ── Pattern library ───────────────────────────────────────────────────────────

# Celery: @app.task, @celery.task, @shared_task
_CELERY_TASK_RE = re.compile(
    r"@(?:[\w.]+\.)?(?:task|shared_task)\b", re.I
)

# APScheduler / rq / dramatiq decorators
_SCHEDULER_RE = re.compile(
    r"@(?:[\w.]+\.)?(?:scheduled_job|job|cron_job|periodic_task|task)\s*\(", re.I
)

# Celery Beat schedule dict entry
_CELERY_BEAT_RE = re.compile(
    r"['\"]([^'\"]+)['\"]\s*:\s*\{[^}]*['\"]task['\"]\s*:\s*['\"]([^'\"]+)['\"]", re.I
)

# JavaScript addEventListener / EventEmitter.on / .once / .off
_JS_LISTENER_RE = re.compile(
    r"""(?:addEventListener|on|once)\s*\(\s*['"`]([^'"`]+)['"`]""", re.I
)

# Python signal / event frameworks
_PY_SIGNAL_RE = re.compile(
    r"""(?:signal|event|post_save|pre_save|post_delete|pre_delete)\s*\.\s*connect\s*\("""
    r"""|@(?:receiver|event_handler|listen)\s*\(""",
    re.I,
)
_SIGNAL_NAME_RE = re.compile(
    r"@receiver\s*\(\s*([^,)]+)", re.I
)

# Django signals
_DJANGO_SIGNAL_RE = re.compile(
    r"@receiver\s*\(\s*([^,\)]+)(?:,\s*sender\s*=\s*(\w+))?", re.I
)

# Queue consumer patterns
_QUEUE_RE: dict[str, list] = {
    "python": [
        # Celery
        re.compile(r"@(?:[\w.]+\.)?task\b", re.I),
        re.compile(r"@(?:[\w.]+\.)?shared_task\b", re.I),
        # RQ
        re.compile(r"@(?:[\w.]+\.)?job\s*\(", re.I),
        # Dramatiq
        re.compile(r"@dramatiq\.actor\b", re.I),
        # SQS / generic queue consumer
        re.compile(r"@(?:[\w.]+\.)?consumer\s*\(", re.I),
        # Kafka
        re.compile(r"@(?:[\w.]+\.)?kafka_consumer\s*\(", re.I),
    ],
    "javascript": [
        # Bull / BullMQ
        re.compile(r"""(?:new\s+Worker|queue\.process|worker\.on)\s*\(\s*['"`]([^'"`]*)['"`]""", re.I),
        # AWS SQS polling
        re.compile(r"""sqs\s*\.\s*receiveMessage\s*\(""", re.I),
        # RabbitMQ amqplib
        re.compile(r"""channel\s*\.\s*consume\s*\(\s*['"`]([^'"`]+)['"`]""", re.I),
        # Kafka.js
        re.compile(r"""consumer\s*\.\s*subscribe\s*\(\s*\{[^}]*topic\s*:\s*['"`]([^'"`]+)['"`]""", re.I),
    ],
    "typescript": [],
}
_QUEUE_RE["typescript"] = _QUEUE_RE["javascript"]

# Lifecycle hooks (Python framework)
_LIFECYCLE_RE: dict[str, list] = {
    "python": [
        re.compile(r"@(?:app|blueprint|router)\.(?:before_request|after_request|teardown_request|on_event)\s*\(", re.I),
        re.compile(r"def\s+(startup|shutdown|lifespan)\s*\(", re.I),
    ],
    "javascript": [
        re.compile(r"""(?:app|server)\s*\.\s*(?:on|once)\s*\(\s*['"`](?:listening|close|error|ready)['"`]""", re.I),
    ],
    "typescript": [],
}
_LIFECYCLE_RE["typescript"] = _LIFECYCLE_RE["javascript"]

# APScheduler add_job / cron
_APSCHEDULER_RE = re.compile(
    r"scheduler\s*\.\s*add_job\s*\([^)]*func\s*=\s*(\w+)", re.I
)
_CRON_DECORATOR_RE = re.compile(
    r"@(?:crontab|periodic_task|scheduled_task|cron)\s*\(", re.I
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _entity_text(ent: dict) -> str:
    return "\n".join([
        ent.get("body_preview") or "",
        ent.get("signature") or "",
        "\n".join(ent.get("decorators") or []),
    ])


def _classify_event(text: str, lang: str) -> tuple[str, str, str]:
    """Return (event_type, event_name, confidence) from entity text."""
    if lang == "python":
        if _CELERY_TASK_RE.search(text):
            return "celery_task", "", "exact"
        if _CRON_DECORATOR_RE.search(text):
            return "scheduler_job", "", "exact"
        m = _DJANGO_SIGNAL_RE.search(text)
        if m:
            return "signal", m.group(1).strip(), "exact"
        if _PY_SIGNAL_RE.search(text):
            return "signal", "", "probable"
        if _APSCHEDULER_RE.search(text):
            return "scheduler_job", "", "exact"
        for pat in _LIFECYCLE_RE.get(lang, []):
            if pat.search(text):
                return "lifecycle_hook", "", "exact"
        for pat in _QUEUE_RE.get(lang, []):
            if pat.search(text):
                return "queue_consumer", "", "probable"

    elif lang in ("javascript", "typescript"):
        m = _JS_LISTENER_RE.search(text)
        if m:
            return "listener", m.group(1), "exact"
        for pat in _QUEUE_RE.get(lang, []):
            qm = pat.search(text)
            if qm and qm.lastindex:
                return "queue_consumer", qm.group(1), "exact"
            if qm:
                return "queue_consumer", "", "probable"
        for pat in _LIFECYCLE_RE.get(lang, []):
            if pat.search(text):
                return "lifecycle_hook", "", "exact"

    return "", "", ""


# ── Public builder ────────────────────────────────────────────────────────────

def build_event_edges(entities: list[dict], project_id: str) -> list[dict]:
    """
    Parse event/callback/scheduler registrations from entity body previews.

    Returns a list of `event_edge` documents ready for indexing.
    """
    name_to_id: dict[str, str] = {}
    name_to_qname: dict[str, str] = {}
    for ent in entities:
        name = ent.get("name") or ""
        if name:
            name_to_id[name] = ent.get("id") or ""
            name_to_qname[name] = ent.get("qualified_name") or ""

    edges: list[dict] = []
    seen_ids: set[str] = set()

    for ent in entities:
        if ent.get("kind") not in ("function", "method"):
            continue

        lang = ent.get("language", "")
        text = _entity_text(ent)
        if not text.strip():
            continue

        event_type, event_name, confidence = _classify_event(text, lang)
        if not event_type:
            continue

        handler_name = ent.get("name") or ""
        handler_file = ent.get("file_path") or ""
        handler_id = ent.get("id") or ""
        handler_qname = ent.get("qualified_name") or ""

        edge_id = _event_id(project_id, handler_file, handler_name, event_type, event_name)
        if edge_id in seen_ids:
            continue
        seen_ids.add(edge_id)

        edges.append({
            "id": edge_id,
            "kind": "event_edge",
            "name": f"{event_type}:{event_name or handler_name}",
            "event_type": event_type,
            "event_name": event_name,
            "handler_name": handler_name,
            "handler_qname": handler_qname,
            "handler_file": handler_file,
            "handler_id": handler_id,
            "language": lang,
            "file_path": handler_file,
            "line_start": ent.get("line_start", 0),
            "line_end": ent.get("line_end", 0),
            "signature": f"{event_type} → {handler_name}",
            "docstring": "",
            "body_preview": "",
            "calls": [],
            "called_by": [],
            "called_by_ids": [],
            "imports": [],
            "parent_class": ent.get("parent_class") or "",
            "decorators": list(ent.get("decorators") or []),
            "project_id": project_id,
            "confidence": confidence,
            "provenance": "ast",
            "evidence": [{"file": handler_file, "line": ent.get("line_start", 0)}],
        })

    return edges
