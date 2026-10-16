"""
Build caller → callee edges from parsed entities and populate called_by fields.

Edge schema:
  called_by      — list of caller qualified names (display / backward-compat)
  called_by_ids  — list of caller entity IDs (exact traversal)
  calls          — list of callee bare names as extracted by the parser
  confidence     — "exact" for statically resolved edges, "heuristic" for uncertain
  provenance     — "ast" (tree-sitter), "import_graph", "alias"
"""
import re


def _qualified_name(ent: dict) -> str:
    """Return a stable, fully qualified symbol identity for a function/class.

    Scoped by module path and parent class so the graph is resilient to
    duplicate names across files and packages.
    """
    kind = ent.get("kind")
    name = ent.get("name") or ""
    if not name:
        return ""

    file_path = ent.get("file_path") or ""
    module_name = (
        file_path.rsplit(".", 1)[0].replace("/", ".").replace("\\", ".")
        if file_path else ""
    )
    parent = ent.get("parent_class") or ""

    if kind == "class":
        return f"{module_name}.{name}" if module_name else name
    if kind == "method":
        if module_name and parent:
            return f"{module_name}.{parent}.{name}"
        return f"{module_name}.{name}" if module_name else name
    if kind == "function":
        return f"{module_name}.{name}" if module_name else name
    return name


def _canonical_name(name: str) -> str:
    return (name or "").strip().lower().replace("-", "_")


def _dedupe_entities(entities: list[dict]) -> list[dict]:
    """Drop duplicate entities caused by nested repo copies or repeated parsing."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for ent in entities:
        key = (ent.get("kind"), ent.get("name") or "", ent.get("parent_class") or "", ent.get("file_path") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(ent)
    return out


# ── Import / alias resolution ─────────────────────────────────────────────────

_FROM_IMPORT_RE = re.compile(
    r"from\s+([\w.]+)\s+import\s+([\w,\s*]+(?:\s+as\s+\w+)?(?:\s*,\s*[\w]+(?:\s+as\s+\w+)?)*)",
    re.MULTILINE,
)
_AS_RE = re.compile(r"(\w+)(?:\s+as\s+(\w+))?")
_SIMPLE_IMPORT_RE = re.compile(r"^import\s+([\w.]+)(?:\s+as\s+(\w+))?", re.MULTILINE)


def _parse_body_aliases(body: str) -> dict[str, str]:
    """Extract `from X import Y as Z` and `import X as Y` aliases from a code body."""
    aliases: dict[str, str] = {}
    for m in _FROM_IMPORT_RE.finditer(body):
        module = m.group(1)
        imports_clause = m.group(2)
        for part in imports_clause.split(","):
            am = _AS_RE.match(part.strip())
            if not am:
                continue
            orig_name, alias = am.group(1), am.group(2)
            target = f"{module}.{orig_name}"
            local = alias if alias else orig_name
            aliases[local] = target
    for m in _SIMPLE_IMPORT_RE.finditer(body):
        module, alias = m.group(1), m.group(2)
        local = alias if alias else module.split(".")[-1]
        aliases[local] = module
    return aliases


def _resolve_import_aliases(ent: dict) -> dict[str, str]:
    """Map local names to their import targets for symbol resolution.

    Uses both the structured `imports` field and `from X import Y as Z`
    statements in body_preview.
    """
    aliases: dict[str, str] = {}

    # Structured imports (from tree-sitter: fully qualified names)
    for item in ent.get("imports", []) or []:
        if not isinstance(item, str):
            continue
        clean = item.strip()
        if not clean:
            continue
        if "." in clean:
            module_part, local_name = clean.rsplit(".", 1)
            aliases[local_name] = clean
            aliases[module_part.rsplit(".", 1)[-1]] = module_part
        else:
            aliases[clean] = clean

    # Parse alias statements from body/signature text
    body = (ent.get("body_preview") or "") + "\n" + (ent.get("signature") or "")
    aliases.update(_parse_body_aliases(body))

    return aliases


# ── Class method resolution ────────────────────────────────────────────────────

_SELF_CALL_RE = re.compile(r"\bself\.([A-Za-z_]\w*)\s*\(")
_CLS_CALL_RE = re.compile(r"\bcls\.([A-Za-z_]\w*)\s*\(")


def _resolve_self_calls(ent: dict, class_methods: dict[str, set[str]]) -> list[str]:
    """Expand `self.method()` / `cls.method()` calls to qualified method names.

    Returns additional callee names that should be added to `calls`.
    """
    parent = ent.get("parent_class") or ""
    if not parent:
        return []

    known_methods = class_methods.get(parent, set())
    if not known_methods:
        return []

    body = (ent.get("body_preview") or "") + "\n" + (ent.get("signature") or "")
    extra: list[str] = []
    for pat in (_SELF_CALL_RE, _CLS_CALL_RE):
        for m in pat.finditer(body):
            method_name = m.group(1)
            if method_name in known_methods and method_name not in extra:
                extra.append(method_name)
    return extra


# ── Lookup helpers ────────────────────────────────────────────────────────────

def _lookup_targets(name_to_entities: dict[str, list[dict]], callee_name: str) -> list[dict]:
    """Resolve a call target by exact name, canonical name, and qualified name."""
    targets = name_to_entities.get(callee_name, [])
    if targets:
        return targets

    canonical = _canonical_name(callee_name)
    for key, candidates in name_to_entities.items():
        if _canonical_name(key) == canonical:
            targets.extend(candidates)
    if targets:
        return targets

    for candidates in name_to_entities.values():
        for candidate in candidates:
            if candidate.get("qualified_name") == callee_name:
                targets.append(candidate)
    return targets


def _is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _defined_locally(caller: dict, name: str) -> bool:
    """True when the caller's body defines a nested function called `name`.

    Nested defs are not indexed as entities, so a call to one would otherwise
    bind to an unrelated top-level function that happens to share the name.
    """
    body = caller.get("body_preview") or ""
    _, _, rest = body.partition("\n")
    return bool(
        re.search(rf"^[ \t]+(?:async[ \t]+)?def[ \t]+{re.escape(name)}[ \t]*\(", rest, re.M)
    )


def _unique_by_id(entities: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for ent in entities:
        key = ent.get("id") or id(ent)
        if key in seen:
            continue
        seen.add(key)
        out.append(ent)
    return out


def _resolve_callee(
    caller: dict,
    callee_name: str,
    import_aliases: dict[str, str],
    self_calls: set[str],
    name_to_entities: dict[str, list[dict]],
) -> tuple[list[dict], bool]:
    """Resolve one call to (targets, is_exact).

    Exact edges are backed by evidence: an import, a self/cls call within the
    same class, or a name that is unique in the repo or in the caller's file.
    A bare name shared by several definitions with no such evidence yields
    heuristic targets, which impact traversal ignores unless asked.
    Dunder names (e.g. super().__init__()) are never linked by bare name.
    """
    if "." not in callee_name and _defined_locally(caller, callee_name):
        return [], False

    resolved = import_aliases.get(callee_name)
    if resolved:
        targets = _unique_by_id(_lookup_targets(name_to_entities, resolved))
        if targets:
            return targets, True

    if callee_name in self_calls:
        same_class = [
            t for t in name_to_entities.get(callee_name, [])
            if t.get("kind") == "method"
            and t.get("parent_class") == caller.get("parent_class")
            and t.get("file_path") == caller.get("file_path")
        ]
        if same_class:
            return _unique_by_id(same_class), True

    if _is_dunder(callee_name.split(".")[-1]):
        return [], False

    candidates = _unique_by_id(_lookup_targets(name_to_entities, callee_name))
    if not candidates and "." in callee_name:
        candidates = _unique_by_id(
            _lookup_targets(name_to_entities, callee_name.split(".")[-1])
        )
    if not candidates:
        return [], False
    if len(candidates) == 1:
        return candidates, True

    same_file = [c for c in candidates if c.get("file_path") == caller.get("file_path")]
    if len(same_file) == 1:
        return same_file, True
    return candidates, False


# ── Main builder ──────────────────────────────────────────────────────────────

def build_call_graph(entities: list[dict]) -> list[dict]:
    """
    Populate called_by, called_by_ids, qualified_name, and entity_id on each entity.

    called_by      — caller qualified names (display, backward-compatible)
    called_by_ids  — caller entity IDs (exact graph traversal in impact.py)
    entity_id      — explicit alias for the `id` field
    """
    entities = _dedupe_entities(entities)

    # Phase 1: enrich identifiers
    for ent in entities:
        fp = ent.get("file_path") or ""
        ent["module_name"] = fp.rsplit(".", 1)[0].replace("/", ".").replace("\\", ".") if fp else ""
        ent["qualified_name"] = _qualified_name(ent)
        ent["canonical_name"] = _canonical_name(ent.get("name") or "")
        ent["entity_id"] = ent.get("id") or ""
        ent.setdefault("called_by", [])
        ent.setdefault("called_by_ids", [])
        ent.setdefault("called_by_heuristic", [])
        ent.setdefault("called_by_heuristic_ids", [])

    # Build name → entity lookup
    name_to_entities: dict[str, list[dict]] = {}
    for ent in entities:
        if ent["kind"] in ("function", "method", "class"):
            for key in (ent.get("name") or "", ent.get("qualified_name") or "", ent.get("canonical_name") or ""):
                if key:
                    name_to_entities.setdefault(key, []).append(ent)

    # Build class → method-names lookup for self.method() resolution
    class_methods: dict[str, set[str]] = {}
    for ent in entities:
        if ent["kind"] == "method" and ent.get("parent_class"):
            class_methods.setdefault(ent["parent_class"], set()).add(ent["name"] or "")

    # Phase 2: edge building
    for ent in entities:
        if ent["kind"] not in ("function", "method"):
            continue
        caller_id = ent.get("id") or ""
        caller_name = ent.get("name")
        caller_qname = ent.get("qualified_name")
        import_aliases = _resolve_import_aliases(ent)

        self_calls = set(_resolve_self_calls(ent, class_methods))
        raw_calls = list(ent.get("calls") or [])
        for extra in self_calls:
            if extra not in raw_calls:
                raw_calls.append(extra)

        for callee_name in raw_calls:
            targets, exact = _resolve_callee(
                ent, callee_name, import_aliases, self_calls, name_to_entities
            )

            for target in targets:
                if target.get("name") is None:
                    continue

                if exact:
                    names_field, ids_field = "called_by", "called_by_ids"
                else:
                    names_field, ids_field = "called_by_heuristic", "called_by_heuristic_ids"

                if caller_name and caller_name not in target[names_field]:
                    target[names_field].append(caller_name)
                if caller_qname and caller_qname not in target[names_field]:
                    target[names_field].append(caller_qname)
                if caller_id and caller_id not in target[ids_field]:
                    target[ids_field].append(caller_id)

    # Phase 3: deduplicate and sort; a heuristic edge never duplicates an exact one
    for ent in entities:
        ent["called_by"] = sorted(set(ent.get("called_by", [])))
        ent["called_by_ids"] = sorted(set(ent.get("called_by_ids", [])))
        exact_ids = set(ent["called_by_ids"])
        exact_names = set(ent["called_by"])
        ent["called_by_heuristic_ids"] = sorted(
            set(ent.get("called_by_heuristic_ids", [])) - exact_ids
        )
        ent["called_by_heuristic"] = sorted(
            set(ent.get("called_by_heuristic", [])) - exact_names
        )

    return entities
