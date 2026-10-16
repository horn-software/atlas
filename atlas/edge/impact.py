"""
Impact analysis and dependency exploration over indexed call relationships.

Traversal contract:
  find_impact       — caller-only, bounded depth, no fuzzy promotion
  find_dependencies — bidirectional, separate API

Edge traversal uses called_by_ids (exact entity-ID edges) when present,
falling back to called_by (name strings) for indexes built before W1.1.
Only edges with confidence != "heuristic" are followed by default;
pass include_heuristic=True to widen traversal.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ingest.indexer import search


_SYMBOL_KINDS = "kind IN ['function', 'method']"


def _filter_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _exact_lookup(project_id: str, field: str, value: str,
                  cache: dict[tuple[str, str], list[dict]] | None = None) -> list[dict]:
    """Return only exact filter matches; lexical relevance never establishes an edge."""
    key = (field, value)
    if cache is not None and key in cache:
        return cache[key]
    hits = search(
        project_id,
        "",
        filters=f"{_SYMBOL_KINDS} AND {field} = {_filter_value(value)}",
        limit=1000,
        min_score=0,
    )
    exact = [
        hit for hit in hits
        if hit.get(field) == value and hit.get("kind") in {"function", "method"}
    ]
    if cache is not None:
        cache[key] = exact
    return exact


def _resolve_symbol(project_id: str, symbol: str,
                    cache: dict[tuple[str, str], list[dict]]) -> dict:
    """Resolve an entity ID, qualified name, or unique bare symbol name."""
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("function symbols must be non-empty strings")
    symbol = symbol.strip()

    for field in ("id", "qualified_name"):
        matches = _exact_lookup(project_id, field, symbol, cache)
        if matches:
            return matches[0]

    matches = _exact_lookup(project_id, "name", symbol, cache)
    if not matches:
        raise ValueError(f"no indexed function or method exactly matches {symbol!r}")
    if len(matches) > 1:
        candidates = sorted(
            hit.get("qualified_name") or hit.get("id") or hit.get("name", "")
            for hit in matches
        )
        raise ValueError(
            f"symbol {symbol!r} is ambiguous; pass a qualified name or entity ID: "
            + ", ".join(candidates)
        )
    return matches[0]


def _resolve_edge_target_by_id(project_id: str, entity_id: str,
                                cache: dict[tuple[str, str], list[dict]]) -> list[dict]:
    """Resolve an exact entity-ID edge — no name matching, no ambiguity."""
    return _exact_lookup(project_id, "id", entity_id, cache)


def _resolve_edge_target_by_name(project_id: str, relation: str,
                                  cache: dict[tuple[str, str], list[dict]]) -> list[dict]:
    """Resolve a stored name-based edge endpoint, skipping ambiguous bare names.

    Used as a fallback for indexes built before called_by_ids was added.
    """
    if not isinstance(relation, str) or not relation:
        return []

    matches = _exact_lookup(project_id, "qualified_name", relation, cache)
    if matches:
        return matches

    matches = _exact_lookup(project_id, "name", relation, cache)
    return matches if len(matches) == 1 else []


def _walk(project_id: str, symbols: list[str], max_depth: int,
          bidirectional: bool, include_heuristic: bool = False) -> list[dict]:
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 0:
        raise ValueError("max_depth must be a non-negative integer")
    if not symbols:
        return []

    lookup_cache: dict[tuple[str, str], list[dict]] = {}
    seeds = [_resolve_symbol(project_id, symbol, lookup_cache) for symbol in symbols]
    visited: set[str] = set()
    affected: list[dict] = []
    frontier = [(seed, 0) for seed in seeds]
    scheduled = {seed.get("id") for seed in seeds}

    while frontier:
        entity, depth = frontier.pop(0)
        entity_id = entity.get("id")
        if not isinstance(entity_id, str) or not entity_id:
            raise ValueError(
                f"indexed function {entity.get('qualified_name') or entity.get('name')!r} "
                "has no stable entity ID"
            )
        if entity_id in visited:
            continue
        visited.add(entity_id)

        result = dict(entity)
        result["_impact_depth"] = depth
        affected.append(result)
        if depth >= max_depth:
            continue

        # Collect next-hop entities using exact IDs when available
        next_hops: list[dict] = []

        # Caller edges (always followed for impact; also for bidirectional)
        called_by_ids = entity.get("called_by_ids")
        if called_by_ids is not None:
            # Exact ID-based traversal — no name matching. An empty list means
            # "no callers", not "field missing", so it must not fall back to names.
            edge_ids = list(called_by_ids)
            if include_heuristic:
                edge_ids += entity.get("called_by_heuristic_ids") or []
            for cid in edge_ids:
                next_hops.extend(_resolve_edge_target_by_id(project_id, cid, lookup_cache))
        else:
            # Legacy name-based fallback for pre-W1.1 indexes
            for relation in entity.get("called_by") or []:
                next_hops.extend(
                    _resolve_edge_target_by_name(project_id, relation, lookup_cache)
                )

        if bidirectional:
            # Callee edges (dependency exploration only)
            for callee_name in entity.get("calls") or []:
                next_hops.extend(
                    _resolve_edge_target_by_name(project_id, callee_name, lookup_cache)
                )

        for related in next_hops:
            # Skip heuristic edges unless opted in
            if not include_heuristic and related.get("confidence") == "heuristic":
                continue
            related_id = related.get("id")
            if related_id and related_id not in scheduled:
                scheduled.add(related_id)
                frontier.append((related, depth + 1))

    return affected


def find_impact(project_id: str, function_names: list[str],
                max_depth: int = 3, include_heuristic: bool = False) -> list[dict]:
    """
    Return the given functions and their transitive callers, bounded by max_depth.

    Traversal follows caller edges only (called_by_ids preferred over called_by).
    Input symbols must resolve exactly to an entity ID, qualified name, or a unique
    bare name — ambiguous bare names are rejected.

    Set include_heuristic=True to also follow edges marked confidence="heuristic".
    """
    return _walk(project_id, function_names, max_depth, bidirectional=False,
                 include_heuristic=include_heuristic)


def find_dependencies(project_id: str, function_names: list[str],
                      max_depth: int = 3, include_heuristic: bool = False) -> list[dict]:
    """
    Explore dependencies in both caller and callee directions.

    Kept separate from find_impact so callers can do broad dependency exploration
    without treating callees as affected by a change.
    """
    return _walk(project_id, function_names, max_depth, bidirectional=True,
                 include_heuristic=include_heuristic)
