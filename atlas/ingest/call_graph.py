"""
Build caller → callee edges from parsed entities and populate called_by fields.
"""


def build_call_graph(entities: list[dict]) -> list[dict]:
    """
    Given a flat list of entity documents (from parsers.parse_file),
    populate the called_by field on each entity by inverting the calls edges.

    A function F that has calls=["G", "H"] means F calls G and H.
    So G.called_by should include F, and H.called_by should include F.
    """
    name_to_entities: dict[str, list[dict]] = {}
    for ent in entities:
        if ent["kind"] in ("function", "method"):
            key = ent["name"]
            name_to_entities.setdefault(key, []).append(ent)

    for ent in entities:
        if ent["kind"] not in ("function", "method"):
            continue
        caller_name = ent["name"]
        for callee_name in ent.get("calls", []):
            targets = name_to_entities.get(callee_name, [])
            for target in targets:
                if caller_name not in target["called_by"]:
                    target["called_by"].append(caller_name)

    for ent in entities:
        ent["called_by"] = sorted(set(ent.get("called_by", [])))

    return entities
