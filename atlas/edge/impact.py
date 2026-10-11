"""
Impact analysis: given changed function names, query the codebase index
for all callers and callees, walking called_by edges recursively.
Also follows HTTP bridge edges to surface cross-language callers.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ingest.indexer import search


def find_impact(project_id: str, function_names: list[str],
                max_depth: int = 3) -> list[dict]:
    """
    Find all functions affected by changes to the given functions.
    Walks the called_by edges up to max_depth levels, then appends any
    cross-language callers found via HTTP bridge documents.
    Returns a flat list of affected entity documents.
    """
    visited = set()
    affected = []
    frontier = list(function_names)

    for depth in range(max_depth + 1):
        if not frontier:
            break

        next_frontier = []
        for fn_name in frontier:
            if fn_name in visited:
                continue
            visited.add(fn_name)

            # Exact name lookup via filter — more reliable than relevance search
            hits = search(
                project_id,
                fn_name,
                filters=f"kind IN ['function', 'method'] AND name = '{fn_name}'",
                limit=50,
            )
            # Fall back to relevance search if exact filter returns nothing
            if not hits:
                hits = search(
                    project_id,
                    fn_name,
                    filters="kind IN ['function', 'method']",
                    limit=50,
                )

            for hit in hits:
                if hit["name"] != fn_name:
                    continue

                hit["_impact_depth"] = depth
                affected.append(hit)

                for caller in hit.get("called_by", []):
                    if caller not in visited:
                        next_frontier.append(caller)

        frontier = next_frontier

    # Follow HTTP bridge edges: find cross-language callers of every visited function
    try:
        from ingest.bridge_resolver import find_bridge_callers
        for fn_name in list(visited):
            bridge_hits = find_bridge_callers(project_id, fn_name)
            for bridge in bridge_hits:
                bridge["_impact_depth"] = max_depth
                bridge["_cross_language_bridge"] = True
                affected.append(bridge)
    except Exception:
        pass

    return affected
