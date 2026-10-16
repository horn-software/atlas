# Atlas Benchmark Fixtures

Ground-truth manifests for Atlas benchmark evaluation.

Each fixture directory contains:
- `source/` — the fixture source files (frozen, never modified)
- `expected.json` — manually reviewed expected relationships

## expected.json schema

```json
{
  "fixture": "fixture_name",
  "description": "What this fixture tests",
  "reviewed_by": "petrushorn24@gmail.com",
  "reviewed_at": "2026-10-15",
  "expected_impact": [
    {
      "input": "function_name_or_entity_id",
      "depth": 3,
      "expected_ids": ["id1", "id2"],
      "notes": "why these are correct"
    }
  ],
  "expected_edges": [
    {
      "caller": "qualified_name",
      "callee": "qualified_name",
      "confidence": "exact",
      "evidence": "file:line"
    }
  ],
  "expected_routes": [
    {
      "method": "GET",
      "path": "/api/users",
      "handler": "get_users",
      "handler_file": "source/app.py"
    }
  ],
  "expected_events": [
    {
      "event_type": "celery_task",
      "handler": "send_email",
      "handler_file": "source/tasks.py"
    }
  ],
  "expected_config": [
    {
      "config_key": "DATABASE_URL",
      "consumer": "connect_db",
      "consumer_file": "source/db.py"
    }
  ]
}
```

## Integrity rule

**Never generate expected.json from Atlas output.** Each expected relationship
must be manually verified against the source files. If Atlas produced a result
and you want to add it, open the source file, confirm the edge exists, and then
add it here with your email in `reviewed_by`.

## Running the benchmark

```
python atlas/benchmarking.py --fixture benchmarking/fixtures/call_graph_basic
```
