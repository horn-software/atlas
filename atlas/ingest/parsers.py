"""
Parse source files using tree-sitter.
Supports Python, JavaScript, TypeScript, Go, Rust, Java, C#, C, C++,
Ruby, PHP, Swift, Kotlin, Scala, Lua, Elixir, Dart, Zig.
Extracts functions, classes, methods, imports, signatures, docstrings,
line numbers, and call relationships.
"""
import hashlib
from pathlib import Path

from tree_sitter import Language, Parser
import tree_sitter_python as tspython
import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
import tree_sitter_go as tsgo
import tree_sitter_rust as tsrust
import tree_sitter_java as tsjava
import tree_sitter_c_sharp as tscs
import tree_sitter_c as tsc
import tree_sitter_cpp as tscpp
import tree_sitter_ruby as tsruby
import tree_sitter_php as tsphp
import tree_sitter_swift as tsswift
import tree_sitter_kotlin as tskotlin
import tree_sitter_scala as tsscala
import tree_sitter_lua as tslua
import tree_sitter_elixir as tselixir
import tree_sitter_dart as tsdart
import tree_sitter_zig as tszig

LANGUAGES = {
    "python":     Language(tspython.language()),
    "javascript": Language(tsjs.language()),
    "typescript": Language(tsts.language_typescript()),
    "go":         Language(tsgo.language()),
    "rust":       Language(tsrust.language()),
    "java":       Language(tsjava.language()),
    "csharp":     Language(tscs.language()),
    "c":          Language(tsc.language()),
    "cpp":        Language(tscpp.language()),
    "ruby":       Language(tsruby.language()),
    "php":        Language(tsphp.language_php()),
    "swift":      Language(tsswift.language()),
    "kotlin":     Language(tskotlin.language()),
    "scala":      Language(tsscala.language()),
    "lua":        Language(tslua.language()),
    "elixir":     Language(tselixir.language()),
    "dart":       Language(tsdart.language()),
    "zig":        Language(tszig.language()),
}

EXT_TO_LANG = {
    ".py":   "python",
    ".js":   "javascript",
    ".ts":   "typescript",
    ".tsx":  "typescript",
    ".go":   "go",
    ".rs":   "rust",
    ".java": "java",
    ".cs":   "csharp",
    ".c":    "c",
    ".h":    "c",
    ".cpp":  "cpp",
    ".cc":   "cpp",
    ".cxx":  "cpp",
    ".hpp":  "cpp",
    ".rb":   "ruby",
    ".php":  "php",
    ".swift":"swift",
    ".kt":   "kotlin",
    ".kts":  "kotlin",
    ".scala":"scala",
    ".lua":  "lua",
    ".ex":   "elixir",
    ".exs":  "elixir",
    ".dart": "dart",
    ".zig":  "zig",
}


def _hash_id(file_path: str, name: str, kind: str) -> str:
    raw = f"{file_path}:{name}:{kind}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _node_text(node) -> str:
    return node.text.decode("utf-8") if node and node.text else ""


def _body_preview(source_lines: list[str], start: int, end: int, max_chars: int = 500) -> str:
    body = "\n".join(source_lines[start:end + 1])
    return body[:max_chars]


def _find_children(node, *types):
    return [c for c in node.children if c.type in types]


def _find_child(node, *types):
    for c in node.children:
        if c.type in types:
            return c
    return None


# ── Call extraction (language-agnostic via tree-sitter queries) ───────────────

def _collect_calls_ts(node) -> list[str]:
    """Walk a tree-sitter subtree and collect function/method call names."""
    calls = set()
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in ("call", "call_expression"):
            func = n.children[0] if n.children else None
            if func:
                if func.type in ("identifier", "field_identifier", "property_identifier"):
                    calls.add(_node_text(func))
                elif func.type == "attribute":
                    # Python: self.method() → attribute has identifier children
                    ids = _find_children(func, "identifier")
                    if len(ids) >= 2:
                        calls.add(_node_text(ids[-1]))
                    elif ids:
                        calls.add(_node_text(ids[0]))
                elif func.type == "member_expression":
                    # JS/TS: obj.method()
                    prop = _find_child(func, "property_identifier")
                    if prop:
                        calls.add(_node_text(prop))
                elif func.type == "selector_expression":
                    # Go: pkg.Function()
                    field = _find_child(func, "field_identifier")
                    if field:
                        calls.add(_node_text(field))
        stack.extend(n.children)
    return sorted(calls)


# ── Python ────────────────────────────────────────────────────────────────────

def _py_signature(node) -> str:
    name = _node_text(_find_child(node, "identifier"))
    params = _find_child(node, "parameters")
    sig = f"{name}{_node_text(params)}" if params else name
    ret = _find_child(node, "type")
    if ret:
        sig += f" -> {_node_text(ret)}"
    return sig


def _py_docstring(node) -> str:
    body = _find_child(node, "block")
    if not body:
        return ""
    first = body.children[0] if body.children else None
    if first and first.type == "expression_statement":
        s = _find_child(first, "string")
        if s:
            text = _node_text(s)
            for q in ('"""', "'''", '"', "'"):
                if text.startswith(q) and text.endswith(q):
                    return text[len(q):-len(q)].strip()
            return text
    return ""


def _py_module_docstring(root) -> str:
    if not root.children:
        return ""
    first = root.children[0]
    if first.type == "expression_statement":
        s = _find_child(first, "string")
        if s:
            text = _node_text(s)
            for q in ('"""', "'''", '"', "'"):
                if text.startswith(q) and text.endswith(q):
                    return text[len(q):-len(q)].strip()
    return ""


def _py_imports(root) -> list[str]:
    imports = []
    for node in root.children:
        if node.type == "import_statement":
            for c in _find_children(node, "dotted_name"):
                imports.append(_node_text(c))
        elif node.type == "import_from_statement":
            module = _find_child(node, "dotted_name")
            mod_name = _node_text(module) if module else ""
            for c in _find_children(node, "dotted_name"):
                if c is not module:
                    name = _node_text(c)
                    imports.append(f"{mod_name}.{name}" if mod_name else name)
            for c in _find_children(node, "aliased_import"):
                id_node = _find_child(c, "dotted_name", "identifier")
                if id_node:
                    name = _node_text(id_node)
                    imports.append(f"{mod_name}.{name}" if mod_name else name)
    return sorted(set(imports))


def _py_decorators(node) -> list[str]:
    decs = []
    for c in node.children:
        if c.type == "decorator":
            text = _node_text(c)
            if text.startswith("@"):
                text = text[1:]
            decs.append(text)
    return decs


def _parse_python(root, rel_path, source_lines, source) -> list[dict]:
    entities = []
    module_imports = _py_imports(root)

    entities.append({
        "id": _hash_id(rel_path, rel_path, "file"),
        "kind": "file",
        "name": rel_path.split("/")[-1],
        "file_path": rel_path,
        "language": "python",
        "line_start": 1,
        "line_end": len(source_lines),
        "signature": "",
        "docstring": _py_module_docstring(root),
        "body_preview": source[:500],
        "calls": [],
        "called_by": [],
        "imports": module_imports,
        "parent_class": "",
        "decorators": [],
    })

    for node in root.children:
        if node.type in ("function_definition",):
            entities.append({
                "id": _hash_id(rel_path, _node_text(_find_child(node, "identifier")), "function"),
                "kind": "function",
                "name": _node_text(_find_child(node, "identifier")),
                "file_path": rel_path,
                "language": "python",
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": _py_signature(node),
                "docstring": _py_docstring(node),
                "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                "calls": _collect_calls_ts(node),
                "called_by": [],
                "imports": [],
                "parent_class": "",
                "decorators": _py_decorators(node),
            })

        elif node.type == "decorated_definition":
            inner = _find_child(node, "function_definition", "class_definition")
            if inner and inner.type == "function_definition":
                entities.append({
                    "id": _hash_id(rel_path, _node_text(_find_child(inner, "identifier")), "function"),
                    "kind": "function",
                    "name": _node_text(_find_child(inner, "identifier")),
                    "file_path": rel_path,
                    "language": "python",
                    "line_start": node.start_point[0] + 1,
                    "line_end": node.end_point[0] + 1,
                    "signature": _py_signature(inner),
                    "docstring": _py_docstring(inner),
                    "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                    "calls": _collect_calls_ts(inner),
                    "called_by": [],
                    "imports": [],
                    "parent_class": "",
                    "decorators": _py_decorators(node),
                })
            elif inner and inner.type == "class_definition":
                _parse_py_class(inner, rel_path, source_lines, entities, _py_decorators(node))

        elif node.type == "class_definition":
            _parse_py_class(node, rel_path, source_lines, entities, [])

    return entities


def _parse_py_class(node, rel_path, source_lines, entities, decorators):
    class_name = _node_text(_find_child(node, "identifier"))
    entities.append({
        "id": _hash_id(rel_path, class_name, "class"),
        "kind": "class",
        "name": class_name,
        "file_path": rel_path,
        "language": "python",
        "line_start": node.start_point[0] + 1,
        "line_end": node.end_point[0] + 1,
        "signature": class_name,
        "docstring": _py_docstring(node),
        "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
        "calls": [],
        "called_by": [],
        "imports": [],
        "parent_class": "",
        "decorators": decorators,
    })

    body = _find_child(node, "body", "block")
    if body:
        for child in body.children:
            fn = child
            decs = []
            if child.type == "decorated_definition":
                decs = _py_decorators(child)
                fn = _find_child(child, "function_definition")
                if not fn:
                    continue
            if fn.type == "function_definition":
                method_name = _node_text(_find_child(fn, "identifier"))
                entities.append({
                    "id": _hash_id(rel_path, f"{class_name}.{method_name}", "method"),
                    "kind": "method",
                    "name": method_name,
                    "file_path": rel_path,
                    "language": "python",
                    "line_start": fn.start_point[0] + 1,
                    "line_end": fn.end_point[0] + 1,
                    "signature": _py_signature(fn),
                    "docstring": _py_docstring(fn),
                    "body_preview": _body_preview(source_lines, fn.start_point[0], fn.end_point[0]),
                    "calls": _collect_calls_ts(fn),
                    "called_by": [],
                    "imports": [],
                    "parent_class": class_name,
                    "decorators": decs,
                })


# ── JavaScript / TypeScript ──────────────────────────────────────────────────

def _js_signature(node, lang) -> str:
    name_node = _find_child(node, "identifier", "property_identifier", "type_identifier")
    name = _node_text(name_node) if name_node else ""
    params = _find_child(node, "formal_parameters")
    sig = f"{name}{_node_text(params)}" if params else name
    ret = _find_child(node, "type_annotation")
    if ret:
        ret_text = _node_text(ret)
        if ret_text.startswith(":"):
            sig += ret_text
        else:
            sig += f": {ret_text}"
    return sig


def _js_docstring(node, source_lines) -> str:
    line = node.start_point[0]
    if line > 0:
        prev = source_lines[line - 1].strip()
        if prev.endswith("*/"):
            lines = []
            for i in range(line - 1, -1, -1):
                lines.insert(0, source_lines[i].strip())
                if source_lines[i].strip().startswith("/**") or source_lines[i].strip().startswith("/*"):
                    break
            comment = " ".join(lines)
            comment = comment.replace("/**", "").replace("*/", "").replace("* ", "").strip()
            return comment
    return ""


def _js_imports(root) -> list[str]:
    imports = []
    for node in root.children:
        if node.type == "import_statement":
            src = _find_child(node, "string")
            if src:
                text = _node_text(src).strip("'\"")
                imports.append(text)
        elif node.type == "lexical_declaration" or node.type == "variable_declaration":
            text = _node_text(node)
            if "require(" in text:
                start = text.find("require(") + 8
                end = text.find(")", start)
                if end > start:
                    mod = text[start:end].strip("'\"")
                    imports.append(mod)
    return sorted(set(imports))


def _parse_js(root, rel_path, source_lines, source, lang) -> list[dict]:
    entities = []
    module_imports = _js_imports(root)

    entities.append({
        "id": _hash_id(rel_path, rel_path, "file"),
        "kind": "file",
        "name": rel_path.split("/")[-1],
        "file_path": rel_path,
        "language": lang,
        "line_start": 1,
        "line_end": len(source_lines),
        "signature": "",
        "docstring": "",
        "body_preview": source[:500],
        "calls": [],
        "called_by": [],
        "imports": module_imports,
        "parent_class": "",
        "decorators": [],
    })

    for node in root.children:
        if node.type == "function_declaration":
            name = _node_text(_find_child(node, "identifier"))
            entities.append({
                "id": _hash_id(rel_path, name, "function"),
                "kind": "function",
                "name": name,
                "file_path": rel_path,
                "language": lang,
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": _js_signature(node, lang),
                "docstring": _js_docstring(node, source_lines),
                "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                "calls": _collect_calls_ts(node),
                "called_by": [],
                "imports": [],
                "parent_class": "",
                "decorators": [],
            })
            _extract_nested_arrow_fns(node, rel_path, source_lines, lang, entities, parent_name=name)

        elif node.type == "export_statement":
            inner = _find_child(node, "function_declaration", "class_declaration",
                                "lexical_declaration")
            if inner and inner.type == "function_declaration":
                name = _node_text(_find_child(inner, "identifier"))
                entities.append({
                    "id": _hash_id(rel_path, name, "function"),
                    "kind": "function",
                    "name": name,
                    "file_path": rel_path,
                    "language": lang,
                    "line_start": node.start_point[0] + 1,
                    "line_end": node.end_point[0] + 1,
                    "signature": _js_signature(inner, lang),
                    "docstring": _js_docstring(node, source_lines),
                    "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                    "calls": _collect_calls_ts(inner),
                    "called_by": [],
                    "imports": [],
                    "parent_class": "",
                    "decorators": [],
                })
                _extract_nested_arrow_fns(inner, rel_path, source_lines, lang, entities, parent_name=name)
            elif inner and inner.type == "class_declaration":
                _parse_js_class(inner, rel_path, source_lines, lang, entities)
            elif inner and inner.type == "lexical_declaration":
                _parse_js_arrow_fns(inner, rel_path, source_lines, lang, entities)
                # Also descend into any arrow fn body at this level
                for decl in _find_children(inner, "variable_declarator"):
                    arrow = _find_child(decl, "arrow_function")
                    name_node = _find_child(decl, "identifier")
                    if arrow and name_node:
                        _extract_nested_arrow_fns(arrow, rel_path, source_lines, lang, entities,
                                                  parent_name=_node_text(name_node))

        elif node.type == "class_declaration":
            _parse_js_class(node, rel_path, source_lines, lang, entities)

        elif node.type in ("lexical_declaration", "variable_declaration"):
            _parse_js_arrow_fns(node, rel_path, source_lines, lang, entities)
            for decl in _find_children(node, "variable_declarator"):
                arrow = _find_child(decl, "arrow_function")
                name_node = _find_child(decl, "identifier")
                if arrow and name_node:
                    _extract_nested_arrow_fns(arrow, rel_path, source_lines, lang, entities,
                                              parent_name=_node_text(name_node))

    return entities


def _parse_js_arrow_fns(node, rel_path, source_lines, lang, entities, parent_name=""):
    for decl in _find_children(node, "variable_declarator"):
        name_node = _find_child(decl, "identifier")
        arrow = _find_child(decl, "arrow_function")
        if name_node and arrow:
            name = _node_text(name_node)
            params = _find_child(arrow, "formal_parameters")
            sig = f"{name}{_node_text(params)}" if params else name
            entities.append({
                "id": _hash_id(rel_path, f"{parent_name}.{name}" if parent_name else name, "function"),
                "kind": "function",
                "name": name,
                "file_path": rel_path,
                "language": lang,
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": sig,
                "docstring": _js_docstring(node, source_lines),
                "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                "calls": _collect_calls_ts(arrow),
                "called_by": [],
                "imports": [],
                "parent_class": parent_name,
                "decorators": [],
            })


def _extract_nested_arrow_fns(fn_node, rel_path, source_lines, lang, entities, parent_name="", depth=0):
    """Walk a function/arrow body and extract nested const arrow fn declarations (e.g. React handlers)."""
    if depth > 2:
        return
    body = _find_child(fn_node, "statement_block", "expression_statement")
    if body is None:
        return
    for child in body.children:
        if child.type in ("lexical_declaration", "variable_declaration"):
            _parse_js_arrow_fns(child, rel_path, source_lines, lang, entities, parent_name)
        elif child.type in ("if_statement", "try_statement", "return_statement",
                            "expression_statement", "block"):
            _extract_nested_arrow_fns(child, rel_path, source_lines, lang, entities, parent_name, depth + 1)


def _parse_js_class(node, rel_path, source_lines, lang, entities):
    class_name = _node_text(_find_child(node, "identifier", "type_identifier"))
    entities.append({
        "id": _hash_id(rel_path, class_name, "class"),
        "kind": "class",
        "name": class_name,
        "file_path": rel_path,
        "language": lang,
        "line_start": node.start_point[0] + 1,
        "line_end": node.end_point[0] + 1,
        "signature": class_name,
        "docstring": _js_docstring(node, source_lines),
        "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
        "calls": [],
        "called_by": [],
        "imports": [],
        "parent_class": "",
        "decorators": [],
    })

    body = _find_child(node, "class_body")
    if body:
        for child in body.children:
            if child.type == "method_definition":
                method_name = _node_text(_find_child(child, "property_identifier"))
                params = _find_child(child, "formal_parameters")
                sig = f"{method_name}{_node_text(params)}" if params else method_name
                entities.append({
                    "id": _hash_id(rel_path, f"{class_name}.{method_name}", "method"),
                    "kind": "method",
                    "name": method_name,
                    "file_path": rel_path,
                    "language": lang,
                    "line_start": child.start_point[0] + 1,
                    "line_end": child.end_point[0] + 1,
                    "signature": sig,
                    "docstring": _js_docstring(child, source_lines),
                    "body_preview": _body_preview(source_lines, child.start_point[0], child.end_point[0]),
                    "calls": _collect_calls_ts(child),
                    "called_by": [],
                    "imports": [],
                    "parent_class": class_name,
                    "decorators": [],
                })


# ── Go ────────────────────────────────────────────────────────────────────────

def _go_func_signature(node) -> str:
    name = _node_text(_find_child(node, "identifier"))
    params = _find_child(node, "parameter_list")
    sig = f"{name}{_node_text(params)}" if params else name
    result = _find_child(node, "type_identifier", "pointer_type", "parameter_list")
    children_types = [c.type for c in node.children]
    # Find return type (comes after parameter_list)
    param_seen = 0
    for c in node.children:
        if c.type == "parameter_list":
            param_seen += 1
            if param_seen == 2:
                sig += f" {_node_text(c)}"
                break
        elif param_seen == 1 and c.type in ("type_identifier", "pointer_type",
                                              "slice_type", "map_type", "interface_type"):
            sig += f" {_node_text(c)}"
            break
    return sig


def _go_method_signature(node) -> str:
    name = _node_text(_find_child(node, "field_identifier"))
    param_lists = _find_children(node, "parameter_list")
    receiver = _node_text(param_lists[0]) if param_lists else ""
    params = _node_text(param_lists[1]) if len(param_lists) > 1 else "()"
    sig = f"{receiver} {name}{params}"
    # Return type
    for i, c in enumerate(node.children):
        if c.type == "block":
            break
        if c.type in ("type_identifier", "pointer_type", "slice_type",
                       "map_type", "interface_type"):
            after_params = False
            for pc in node.children:
                if pc.type == "parameter_list":
                    after_params = True
                elif after_params and pc is c:
                    sig += f" {_node_text(c)}"
                    break
    return sig.strip()


def _go_docstring(node, source_lines) -> str:
    line = node.start_point[0]
    lines = []
    for i in range(line - 1, -1, -1):
        stripped = source_lines[i].strip()
        if stripped.startswith("//"):
            lines.insert(0, stripped[2:].strip())
        else:
            break
    return " ".join(lines) if lines else ""


def _go_imports(root) -> list[str]:
    imports = []
    for node in root.children:
        if node.type == "import_declaration":
            for child in node.children:
                if child.type == "import_spec":
                    path = _find_child(child, "interpreted_string_literal")
                    if path:
                        imports.append(_node_text(path).strip('"'))
                elif child.type == "import_spec_list":
                    for spec in child.children:
                        if spec.type == "import_spec":
                            path = _find_child(spec, "interpreted_string_literal")
                            if path:
                                imports.append(_node_text(path).strip('"'))
                elif child.type == "interpreted_string_literal":
                    imports.append(_node_text(child).strip('"'))
    return sorted(set(imports))


def _go_receiver_type(node) -> str:
    param_lists = _find_children(node, "parameter_list")
    if not param_lists:
        return ""
    receiver = param_lists[0]
    for c in receiver.children:
        if c.type == "parameter_declaration":
            type_node = _find_child(c, "type_identifier", "pointer_type")
            if type_node:
                text = _node_text(type_node)
                return text.lstrip("*")
    return ""


def _parse_go(root, rel_path, source_lines, source) -> list[dict]:
    entities = []
    module_imports = _go_imports(root)

    entities.append({
        "id": _hash_id(rel_path, rel_path, "file"),
        "kind": "file",
        "name": rel_path.split("/")[-1],
        "file_path": rel_path,
        "language": "go",
        "line_start": 1,
        "line_end": len(source_lines),
        "signature": "",
        "docstring": "",
        "body_preview": source[:500],
        "calls": [],
        "called_by": [],
        "imports": module_imports,
        "parent_class": "",
        "decorators": [],
    })

    for node in root.children:
        if node.type == "function_declaration":
            name = _node_text(_find_child(node, "identifier"))
            entities.append({
                "id": _hash_id(rel_path, name, "function"),
                "kind": "function",
                "name": name,
                "file_path": rel_path,
                "language": "go",
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": _go_func_signature(node),
                "docstring": _go_docstring(node, source_lines),
                "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                "calls": _collect_calls_ts(node),
                "called_by": [],
                "imports": [],
                "parent_class": "",
                "decorators": [],
            })

        elif node.type == "method_declaration":
            name = _node_text(_find_child(node, "field_identifier"))
            receiver = _go_receiver_type(node)
            entities.append({
                "id": _hash_id(rel_path, f"{receiver}.{name}" if receiver else name, "method"),
                "kind": "method",
                "name": name,
                "file_path": rel_path,
                "language": "go",
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": _go_method_signature(node),
                "docstring": _go_docstring(node, source_lines),
                "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                "calls": _collect_calls_ts(node),
                "called_by": [],
                "imports": [],
                "parent_class": receiver,
                "decorators": [],
            })

        elif node.type == "type_declaration":
            for spec in _find_children(node, "type_spec"):
                type_name = _node_text(_find_child(spec, "type_identifier"))
                struct = _find_child(spec, "struct_type")
                if struct:
                    entities.append({
                        "id": _hash_id(rel_path, type_name, "class"),
                        "kind": "class",
                        "name": type_name,
                        "file_path": rel_path,
                        "language": "go",
                        "line_start": node.start_point[0] + 1,
                        "line_end": node.end_point[0] + 1,
                        "signature": type_name,
                        "docstring": _go_docstring(node, source_lines),
                        "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                        "calls": [],
                        "called_by": [],
                        "imports": [],
                        "parent_class": "",
                        "decorators": [],
                    })

    return entities


# ── Rust ─────────────────────────────────────────────────────────────────────

def _rust_imports(root) -> list[str]:
    imports = []
    for node in root.children:
        if node.type == "use_declaration":
            path = _find_child(node, "scoped_identifier", "identifier", "scoped_use_list")
            if path:
                imports.append(_node_text(path))
    return sorted(set(imports))


def _rust_docstring(node, source_lines) -> str:
    line = node.start_point[0]
    lines = []
    for i in range(line - 1, -1, -1):
        s = source_lines[i].strip()
        if s.startswith("///") or s.startswith("//!"):
            lines.insert(0, s.lstrip("/!").strip())
        else:
            break
    return " ".join(lines)


def _parse_rust(root, rel_path, source_lines, source) -> list[dict]:
    entities = []
    entities.append(_file_entity(rel_path, "rust", source, source_lines, _rust_imports(root)))

    def walk(node, parent_class=""):
        if node.type == "function_item":
            name = _node_text(_find_child(node, "identifier"))
            params = _find_child(node, "parameters")
            ret = _find_child(node, "type_identifier", "primitive_type",
                              "reference_type", "generic_type")
            sig = f"fn {name}{_node_text(params) if params else '()'}"
            if ret:
                sig += f" -> {_node_text(ret)}"
            kind = "method" if parent_class else "function"
            entities.append({
                "id": _hash_id(rel_path, f"{parent_class}.{name}" if parent_class else name, kind),
                "kind": kind,
                "name": name,
                "file_path": rel_path,
                "language": "rust",
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": sig,
                "docstring": _rust_docstring(node, source_lines),
                "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                "calls": _collect_calls_ts(node),
                "called_by": [],
                "imports": [],
                "parent_class": parent_class,
                "decorators": [],
            })
        elif node.type == "struct_item":
            name = _node_text(_find_child(node, "type_identifier"))
            entities.append({
                "id": _hash_id(rel_path, name, "class"),
                "kind": "class",
                "name": name,
                "file_path": rel_path,
                "language": "rust",
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": f"struct {name}",
                "docstring": _rust_docstring(node, source_lines),
                "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                "calls": [],
                "called_by": [],
                "imports": [],
                "parent_class": "",
                "decorators": [],
            })
        elif node.type == "impl_item":
            impl_type = _node_text(_find_child(node, "type_identifier"))
            decl = _find_child(node, "declaration_list")
            if decl:
                for child in decl.children:
                    walk(child, parent_class=impl_type)
            return
        for child in node.children:
            if child.type not in ("function_item", "struct_item", "impl_item"):
                walk(child, parent_class)

    for child in root.children:
        walk(child)
    return entities


# ── Java ──────────────────────────────────────────────────────────────────────

def _java_imports(root) -> list[str]:
    imports = []
    for node in root.children:
        if node.type == "import_declaration":
            path = _find_child(node, "scoped_identifier")
            if path:
                imports.append(_node_text(path))
    return sorted(set(imports))


def _java_docstring(node, source_lines) -> str:
    line = node.start_point[0]
    lines = []
    for i in range(line - 1, -1, -1):
        s = source_lines[i].strip()
        if s.startswith("*") or s.startswith("/**") or s.startswith("*/"):
            text = s.lstrip("*/ ").strip()
            if text:
                lines.insert(0, text)
        elif s == "/**":
            break
        elif not s:
            break
        else:
            break
    return " ".join(lines)


def _java_method_sig(node) -> str:
    name = _node_text(_find_child(node, "identifier"))
    params = _find_child(node, "formal_parameters")
    ret_type = None
    for c in node.children:
        if c.type in ("type_identifier", "integral_type", "floating_point_type",
                       "boolean_type", "void_type", "array_type", "generic_type"):
            ret_type = c
            break
    sig = f"{_node_text(ret_type) + ' ' if ret_type else ''}{name}{_node_text(params) if params else '()'}"
    return sig


def _parse_java_class(node, rel_path, source_lines, entities):
    class_name = _node_text(_find_child(node, "identifier"))
    entities.append({
        "id": _hash_id(rel_path, class_name, "class"),
        "kind": "class",
        "name": class_name,
        "file_path": rel_path,
        "language": "java",
        "line_start": node.start_point[0] + 1,
        "line_end": node.end_point[0] + 1,
        "signature": class_name,
        "docstring": _java_docstring(node, source_lines),
        "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
        "calls": [],
        "called_by": [],
        "imports": [],
        "parent_class": "",
        "decorators": [],
    })
    body = _find_child(node, "class_body")
    if body:
        for child in body.children:
            if child.type in ("method_declaration", "constructor_declaration"):
                method_name = _node_text(_find_child(child, "identifier"))
                entities.append({
                    "id": _hash_id(rel_path, f"{class_name}.{method_name}", "method"),
                    "kind": "method",
                    "name": method_name,
                    "file_path": rel_path,
                    "language": "java",
                    "line_start": child.start_point[0] + 1,
                    "line_end": child.end_point[0] + 1,
                    "signature": _java_method_sig(child),
                    "docstring": _java_docstring(child, source_lines),
                    "body_preview": _body_preview(source_lines, child.start_point[0], child.end_point[0]),
                    "calls": _collect_calls_ts(child),
                    "called_by": [],
                    "imports": [],
                    "parent_class": class_name,
                    "decorators": [],
                })


def _parse_java(root, rel_path, source_lines, source) -> list[dict]:
    entities = []
    entities.append(_file_entity(rel_path, "java", source, source_lines, _java_imports(root)))
    for node in root.children:
        if node.type == "class_declaration":
            _parse_java_class(node, rel_path, source_lines, entities)
    return entities


# ── C# ────────────────────────────────────────────────────────────────────────

def _cs_imports(root) -> list[str]:
    imports = []
    for node in root.children:
        if node.type == "using_directive":
            name = _find_child(node, "identifier", "qualified_name")
            if name:
                imports.append(_node_text(name))
    return sorted(set(imports))


def _cs_method_sig(node) -> str:
    name = _node_text(_find_child(node, "identifier"))
    params = _find_child(node, "parameter_list")
    ret = None
    for c in node.children:
        if c.type in ("identifier", "predefined_type", "nullable_type",
                       "generic_name", "qualified_name", "void_keyword"):
            ret = c
            break
    sig = f"{_node_text(ret) + ' ' if ret else ''}{name}{_node_text(params) if params else '()'}"
    return sig


def _walk_cs_decls(node, rel_path, source_lines, entities, class_name=""):
    for child in node.children:
        if child.type == "class_declaration":
            cname = _node_text(_find_child(child, "identifier"))
            entities.append({
                "id": _hash_id(rel_path, cname, "class"),
                "kind": "class",
                "name": cname,
                "file_path": rel_path,
                "language": "csharp",
                "line_start": child.start_point[0] + 1,
                "line_end": child.end_point[0] + 1,
                "signature": cname,
                "docstring": "",
                "body_preview": _body_preview(source_lines, child.start_point[0], child.end_point[0]),
                "calls": [],
                "called_by": [],
                "imports": [],
                "parent_class": "",
                "decorators": [],
            })
            body = _find_child(child, "declaration_list")
            if body:
                _walk_cs_decls(body, rel_path, source_lines, entities, class_name=cname)
        elif child.type == "method_declaration" and class_name:
            mname = _node_text(_find_child(child, "identifier"))
            entities.append({
                "id": _hash_id(rel_path, f"{class_name}.{mname}", "method"),
                "kind": "method",
                "name": mname,
                "file_path": rel_path,
                "language": "csharp",
                "line_start": child.start_point[0] + 1,
                "line_end": child.end_point[0] + 1,
                "signature": _cs_method_sig(child),
                "docstring": "",
                "body_preview": _body_preview(source_lines, child.start_point[0], child.end_point[0]),
                "calls": _collect_calls_ts(child),
                "called_by": [],
                "imports": [],
                "parent_class": class_name,
                "decorators": [],
            })
        elif child.type == "namespace_declaration":
            decl = _find_child(child, "declaration_list")
            if decl:
                _walk_cs_decls(decl, rel_path, source_lines, entities)


def _parse_csharp(root, rel_path, source_lines, source) -> list[dict]:
    entities = []
    entities.append(_file_entity(rel_path, "csharp", source, source_lines, _cs_imports(root)))
    _walk_cs_decls(root, rel_path, source_lines, entities)
    return entities


# ── C / C++ ───────────────────────────────────────────────────────────────────

def _c_imports(root) -> list[str]:
    imports = []
    for node in root.children:
        if node.type == "preproc_include":
            path = _find_child(node, "system_lib_string", "string_literal")
            if path:
                imports.append(_node_text(path).strip('<>"'))
    return sorted(set(imports))


def _c_func_name(node) -> str:
    decl = _find_child(node, "function_declarator")
    if decl:
        name = _find_child(decl, "identifier", "qualified_identifier")
        if name:
            return _node_text(name)
    return ""


def _c_func_sig(node) -> str:
    ret = None
    for c in node.children:
        if c.type in ("primitive_type", "type_identifier", "sized_type_specifier"):
            ret = c
            break
    decl = _find_child(node, "function_declarator")
    if decl:
        name = _node_text(_find_child(decl, "identifier", "qualified_identifier"))
        params = _find_child(decl, "parameter_list")
        sig = f"{_node_text(ret) + ' ' if ret else ''}{name}{_node_text(params) if params else '()'}"
        return sig
    return ""


def _parse_c_cpp(root, rel_path, source_lines, source, lang) -> list[dict]:
    entities = []
    entities.append(_file_entity(rel_path, lang, source, source_lines, _c_imports(root)))

    for node in root.children:
        if node.type == "function_definition":
            name = _c_func_name(node)
            if not name:
                continue
            # C++: qualified name like Server::start → method
            is_method = "::" in name
            parent = name.split("::")[0] if is_method else ""
            short_name = name.split("::")[-1] if is_method else name
            entities.append({
                "id": _hash_id(rel_path, name, "method" if is_method else "function"),
                "kind": "method" if is_method else "function",
                "name": short_name,
                "file_path": rel_path,
                "language": lang,
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": _c_func_sig(node),
                "docstring": _go_docstring(node, source_lines),  # reuse // comment extractor
                "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                "calls": _collect_calls_ts(node),
                "called_by": [],
                "imports": [],
                "parent_class": parent,
                "decorators": [],
            })
        elif node.type == "class_specifier":  # C++ only
            name = _node_text(_find_child(node, "type_identifier"))
            entities.append({
                "id": _hash_id(rel_path, name, "class"),
                "kind": "class",
                "name": name,
                "file_path": rel_path,
                "language": lang,
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": f"class {name}",
                "docstring": _go_docstring(node, source_lines),
                "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                "calls": [],
                "called_by": [],
                "imports": [],
                "parent_class": "",
                "decorators": [],
            })
        elif node.type == "type_definition":  # C typedef struct
            type_name = _node_text(_find_child(node, "type_identifier"))
            struct = _find_child(node, "struct_specifier")
            if struct and type_name:
                entities.append({
                    "id": _hash_id(rel_path, type_name, "class"),
                    "kind": "class",
                    "name": type_name,
                    "file_path": rel_path,
                    "language": lang,
                    "line_start": node.start_point[0] + 1,
                    "line_end": node.end_point[0] + 1,
                    "signature": f"struct {type_name}",
                    "docstring": "",
                    "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                    "calls": [],
                    "called_by": [],
                    "imports": [],
                    "parent_class": "",
                    "decorators": [],
                })
    return entities


# ── Ruby ──────────────────────────────────────────────────────────────────────

def _ruby_imports(root) -> list[str]:
    imports = []
    for node in root.children:
        if node.type == "call":
            fn = _find_child(node, "identifier")
            if fn and _node_text(fn) in ("require", "require_relative"):
                args = _find_child(node, "argument_list")
                if args:
                    s = _find_child(args, "string")
                    if s:
                        imports.append(_node_text(s).strip("\"'"))
    return sorted(set(imports))


def _ruby_docstring(node, source_lines) -> str:
    line = node.start_point[0]
    lines = []
    for i in range(line - 1, -1, -1):
        s = source_lines[i].strip()
        if s.startswith("#"):
            lines.insert(0, s.lstrip("# "))
        else:
            break
    return " ".join(lines)


def _parse_ruby_class(node, rel_path, source_lines, entities):
    class_name = _node_text(_find_child(node, "constant"))
    entities.append({
        "id": _hash_id(rel_path, class_name, "class"),
        "kind": "class",
        "name": class_name,
        "file_path": rel_path,
        "language": "ruby",
        "line_start": node.start_point[0] + 1,
        "line_end": node.end_point[0] + 1,
        "signature": class_name,
        "docstring": _ruby_docstring(node, source_lines),
        "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
        "calls": [],
        "called_by": [],
        "imports": [],
        "parent_class": "",
        "decorators": [],
    })
    body = _find_child(node, "body_statement")
    if body:
        for child in body.children:
            if child.type == "method":
                mname = _node_text(_find_child(child, "identifier"))
                params = _find_child(child, "method_parameters")
                sig = f"{mname}{_node_text(params) if params else ''}"
                entities.append({
                    "id": _hash_id(rel_path, f"{class_name}.{mname}", "method"),
                    "kind": "method",
                    "name": mname,
                    "file_path": rel_path,
                    "language": "ruby",
                    "line_start": child.start_point[0] + 1,
                    "line_end": child.end_point[0] + 1,
                    "signature": sig,
                    "docstring": _ruby_docstring(child, source_lines),
                    "body_preview": _body_preview(source_lines, child.start_point[0], child.end_point[0]),
                    "calls": _collect_calls_ts(child),
                    "called_by": [],
                    "imports": [],
                    "parent_class": class_name,
                    "decorators": [],
                })


def _parse_ruby(root, rel_path, source_lines, source) -> list[dict]:
    entities = []
    entities.append(_file_entity(rel_path, "ruby", source, source_lines, _ruby_imports(root)))
    for node in root.children:
        if node.type == "class":
            _parse_ruby_class(node, rel_path, source_lines, entities)
        elif node.type == "method":
            mname = _node_text(_find_child(node, "identifier"))
            params = _find_child(node, "method_parameters")
            entities.append({
                "id": _hash_id(rel_path, mname, "function"),
                "kind": "function",
                "name": mname,
                "file_path": rel_path,
                "language": "ruby",
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": f"{mname}{_node_text(params) if params else ''}",
                "docstring": _ruby_docstring(node, source_lines),
                "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                "calls": _collect_calls_ts(node),
                "called_by": [],
                "imports": [],
                "parent_class": "",
                "decorators": [],
            })
    return entities


# ── PHP ───────────────────────────────────────────────────────────────────────

def _php_imports(root) -> list[str]:
    imports = []
    for node in root.children:
        if node.type == "namespace_use_declaration":
            for clause in _find_children(node, "namespace_use_clause"):
                qn = _find_child(clause, "qualified_name")
                if qn:
                    imports.append(_node_text(qn))
    return sorted(set(imports))


def _parse_php_class(node, rel_path, source_lines, entities):
    class_name = _node_text(_find_child(node, "name"))
    entities.append({
        "id": _hash_id(rel_path, class_name, "class"),
        "kind": "class",
        "name": class_name,
        "file_path": rel_path,
        "language": "php",
        "line_start": node.start_point[0] + 1,
        "line_end": node.end_point[0] + 1,
        "signature": class_name,
        "docstring": "",
        "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
        "calls": [],
        "called_by": [],
        "imports": [],
        "parent_class": "",
        "decorators": [],
    })
    body = _find_child(node, "declaration_list")
    if body:
        for child in body.children:
            if child.type == "method_declaration":
                mname = _node_text(_find_child(child, "name"))
                params = _find_child(child, "formal_parameters")
                sig = f"{mname}{_node_text(params) if params else '()'}"
                entities.append({
                    "id": _hash_id(rel_path, f"{class_name}.{mname}", "method"),
                    "kind": "method",
                    "name": mname,
                    "file_path": rel_path,
                    "language": "php",
                    "line_start": child.start_point[0] + 1,
                    "line_end": child.end_point[0] + 1,
                    "signature": sig,
                    "docstring": "",
                    "body_preview": _body_preview(source_lines, child.start_point[0], child.end_point[0]),
                    "calls": _collect_calls_ts(child),
                    "called_by": [],
                    "imports": [],
                    "parent_class": class_name,
                    "decorators": [],
                })


def _parse_php(root, rel_path, source_lines, source) -> list[dict]:
    entities = []
    entities.append(_file_entity(rel_path, "php", source, source_lines, _php_imports(root)))
    for node in root.children:
        if node.type == "class_declaration":
            _parse_php_class(node, rel_path, source_lines, entities)
        elif node.type == "function_definition":
            fname = _node_text(_find_child(node, "name"))
            params = _find_child(node, "formal_parameters")
            entities.append({
                "id": _hash_id(rel_path, fname, "function"),
                "kind": "function",
                "name": fname,
                "file_path": rel_path,
                "language": "php",
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": f"{fname}{_node_text(params) if params else '()'}",
                "docstring": "",
                "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                "calls": _collect_calls_ts(node),
                "called_by": [],
                "imports": [],
                "parent_class": "",
                "decorators": [],
            })
    return entities


# ── Swift ─────────────────────────────────────────────────────────────────────

def _swift_imports(root) -> list[str]:
    imports = []
    for node in root.children:
        if node.type == "import_declaration":
            name = _find_child(node, "identifier")
            if name:
                imports.append(_node_text(name))
    return sorted(set(imports))


def _parse_swift(root, rel_path, source_lines, source) -> list[dict]:
    entities = []
    entities.append(_file_entity(rel_path, "swift", source, source_lines, _swift_imports(root)))

    for node in root.children:
        if node.type == "class_declaration":
            kw = _find_child(node, "class", "struct")
            kind_word = _node_text(kw) if kw else "class"
            cname = _node_text(_find_child(node, "type_identifier"))
            entities.append({
                "id": _hash_id(rel_path, cname, "class"),
                "kind": "class",
                "name": cname,
                "file_path": rel_path,
                "language": "swift",
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": f"{kind_word} {cname}",
                "docstring": "",
                "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                "calls": [],
                "called_by": [],
                "imports": [],
                "parent_class": "",
                "decorators": [],
            })
            body = _find_child(node, "class_body")
            if body:
                for child in body.children:
                    if child.type == "function_declaration":
                        mname = _node_text(_find_child(child, "simple_identifier"))
                        params = []
                        for p in child.children:
                            if p.type == "parameter":
                                params.append(_node_text(p))
                        sig = f"{mname}({', '.join(params)})"
                        entities.append({
                            "id": _hash_id(rel_path, f"{cname}.{mname}", "method"),
                            "kind": "method",
                            "name": mname,
                            "file_path": rel_path,
                            "language": "swift",
                            "line_start": child.start_point[0] + 1,
                            "line_end": child.end_point[0] + 1,
                            "signature": sig,
                            "docstring": "",
                            "body_preview": _body_preview(source_lines, child.start_point[0], child.end_point[0]),
                            "calls": _collect_calls_ts(child),
                            "called_by": [],
                            "imports": [],
                            "parent_class": cname,
                            "decorators": [],
                        })
        elif node.type == "function_declaration":
            fname = _node_text(_find_child(node, "simple_identifier"))
            params = [_node_text(p) for p in node.children if p.type == "parameter"]
            ret = _find_child(node, "user_type", "optional_type", "array_type")
            sig = f"func {fname}({', '.join(params)})"
            if ret:
                sig += f" -> {_node_text(ret)}"
            entities.append({
                "id": _hash_id(rel_path, fname, "function"),
                "kind": "function",
                "name": fname,
                "file_path": rel_path,
                "language": "swift",
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": sig,
                "docstring": "",
                "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                "calls": _collect_calls_ts(node),
                "called_by": [],
                "imports": [],
                "parent_class": "",
                "decorators": [],
            })
    return entities


# ── Kotlin ────────────────────────────────────────────────────────────────────

def _kotlin_imports(root) -> list[str]:
    imports = []
    for node in root.children:
        if node.type == "import":
            ident = _find_child(node, "qualified_identifier", "identifier")
            if ident:
                imports.append(_node_text(ident))
    return sorted(set(imports))


def _parse_kotlin_class(node, rel_path, source_lines, entities):
    cname = _node_text(_find_child(node, "identifier"))
    entities.append({
        "id": _hash_id(rel_path, cname, "class"),
        "kind": "class",
        "name": cname,
        "file_path": rel_path,
        "language": "kotlin",
        "line_start": node.start_point[0] + 1,
        "line_end": node.end_point[0] + 1,
        "signature": cname,
        "docstring": "",
        "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
        "calls": [],
        "called_by": [],
        "imports": [],
        "parent_class": "",
        "decorators": [],
    })
    body = _find_child(node, "class_body")
    if body:
        for child in body.children:
            if child.type == "function_declaration":
                mname = _node_text(_find_child(child, "identifier"))
                params = _find_child(child, "function_value_parameters")
                ret = _find_child(child, "user_type", "nullable_type")
                sig = f"fun {mname}{_node_text(params) if params else '()'}"
                if ret:
                    sig += f": {_node_text(ret)}"
                entities.append({
                    "id": _hash_id(rel_path, f"{cname}.{mname}", "method"),
                    "kind": "method",
                    "name": mname,
                    "file_path": rel_path,
                    "language": "kotlin",
                    "line_start": child.start_point[0] + 1,
                    "line_end": child.end_point[0] + 1,
                    "signature": sig,
                    "docstring": "",
                    "body_preview": _body_preview(source_lines, child.start_point[0], child.end_point[0]),
                    "calls": _collect_calls_ts(child),
                    "called_by": [],
                    "imports": [],
                    "parent_class": cname,
                    "decorators": [],
                })


def _parse_kotlin(root, rel_path, source_lines, source) -> list[dict]:
    entities = []
    entities.append(_file_entity(rel_path, "kotlin", source, source_lines, _kotlin_imports(root)))
    for node in root.children:
        if node.type == "class_declaration":
            _parse_kotlin_class(node, rel_path, source_lines, entities)
        elif node.type == "function_declaration":
            fname = _node_text(_find_child(node, "identifier"))
            params = _find_child(node, "function_value_parameters")
            ret = _find_child(node, "user_type", "nullable_type")
            sig = f"fun {fname}{_node_text(params) if params else '()'}"
            if ret:
                sig += f": {_node_text(ret)}"
            entities.append({
                "id": _hash_id(rel_path, fname, "function"),
                "kind": "function",
                "name": fname,
                "file_path": rel_path,
                "language": "kotlin",
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": sig,
                "docstring": "",
                "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                "calls": _collect_calls_ts(node),
                "called_by": [],
                "imports": [],
                "parent_class": "",
                "decorators": [],
            })
    return entities


# ── Scala ─────────────────────────────────────────────────────────────────────

def _scala_imports(root) -> list[str]:
    imports = []
    for node in root.children:
        if node.type == "import_declaration":
            parts = [_node_text(c) for c in node.children if c.type == "identifier"]
            if parts:
                imports.append(".".join(parts))
    return sorted(set(imports))


def _parse_scala_body(body, rel_path, source_lines, class_name, entities):
    if not body:
        return
    for child in body.children:
        if child.type == "function_definition":
            fname = _node_text(_find_child(child, "identifier"))
            params = _find_child(child, "parameters")
            ret = _find_child(child, "type_identifier")
            sig = f"def {fname}{_node_text(params) if params else '()'}"
            if ret:
                sig += f": {_node_text(ret)}"
            kind = "method" if class_name else "function"
            entities.append({
                "id": _hash_id(rel_path, f"{class_name}.{fname}" if class_name else fname, kind),
                "kind": kind,
                "name": fname,
                "file_path": rel_path,
                "language": "scala",
                "line_start": child.start_point[0] + 1,
                "line_end": child.end_point[0] + 1,
                "signature": sig,
                "docstring": "",
                "body_preview": _body_preview(source_lines, child.start_point[0], child.end_point[0]),
                "calls": _collect_calls_ts(child),
                "called_by": [],
                "imports": [],
                "parent_class": class_name,
                "decorators": [],
            })


def _parse_scala(root, rel_path, source_lines, source) -> list[dict]:
    entities = []
    entities.append(_file_entity(rel_path, "scala", source, source_lines, _scala_imports(root)))
    for node in root.children:
        if node.type in ("class_definition", "object_definition"):
            cname = _node_text(_find_child(node, "identifier"))
            entities.append({
                "id": _hash_id(rel_path, cname, "class"),
                "kind": "class",
                "name": cname,
                "file_path": rel_path,
                "language": "scala",
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": cname,
                "docstring": "",
                "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                "calls": [],
                "called_by": [],
                "imports": [],
                "parent_class": "",
                "decorators": [],
            })
            body = _find_child(node, "template_body")
            _parse_scala_body(body, rel_path, source_lines, cname, entities)
        elif node.type == "function_definition":
            _parse_scala_body(type("N", (), {"children": [node]})(), rel_path, source_lines, "", entities)
    return entities


# ── Lua ───────────────────────────────────────────────────────────────────────

def _parse_lua(root, rel_path, source_lines, source) -> list[dict]:
    entities = []
    entities.append(_file_entity(rel_path, "lua", source, source_lines, []))
    for node in root.children:
        if node.type == "function_declaration":
            name_node = _find_child(node, "identifier", "dot_index_expression",
                                     "method_index_expression")
            name = _node_text(name_node) if name_node else ""
            # dot_index_expression: M.func → use full name
            params = _find_child(node, "parameters")
            sig = f"function {name}{_node_text(params) if params else '()'}"
            entities.append({
                "id": _hash_id(rel_path, name, "function"),
                "kind": "function",
                "name": name.split(".")[-1].split(":")[-1],
                "file_path": rel_path,
                "language": "lua",
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": sig,
                "docstring": _go_docstring(node, source_lines),
                "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                "calls": _collect_calls_ts(node),
                "called_by": [],
                "imports": [],
                "parent_class": "",
                "decorators": [],
            })
    return entities


# ── Elixir ────────────────────────────────────────────────────────────────────

def _elixir_def_name(call_node) -> str:
    args = _find_child(call_node, "arguments")
    if args:
        first = args.children[0] if args.children else None
        if first:
            if first.type == "call":
                return _node_text(_find_child(first, "identifier")) or ""
            return _node_text(first)
    return ""


def _parse_elixir(root, rel_path, source_lines, source) -> list[dict]:
    entities = []
    entities.append(_file_entity(rel_path, "elixir", source, source_lines, []))

    for node in root.children:
        if node.type != "call":
            continue
        fn_id = _find_child(node, "identifier")
        fn_name = _node_text(fn_id) if fn_id else ""

        if fn_name == "defmodule":
            args = _find_child(node, "arguments")
            mod_name = _node_text(args.children[0]) if args and args.children else ""
            entities.append({
                "id": _hash_id(rel_path, mod_name, "class"),
                "kind": "class",
                "name": mod_name,
                "file_path": rel_path,
                "language": "elixir",
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": f"defmodule {mod_name}",
                "docstring": "",
                "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                "calls": [],
                "called_by": [],
                "imports": [],
                "parent_class": "",
                "decorators": [],
            })
            do_block = _find_child(node, "do_block")
            if do_block:
                for child in do_block.children:
                    if child.type == "call":
                        cid = _find_child(child, "identifier")
                        cname = _node_text(cid) if cid else ""
                        if cname in ("def", "defp"):
                            fname = _elixir_def_name(child)
                            if fname:
                                entities.append({
                                    "id": _hash_id(rel_path, f"{mod_name}.{fname}", "method"),
                                    "kind": "method",
                                    "name": fname,
                                    "file_path": rel_path,
                                    "language": "elixir",
                                    "line_start": child.start_point[0] + 1,
                                    "line_end": child.end_point[0] + 1,
                                    "signature": f"{cname} {fname}",
                                    "docstring": "",
                                    "body_preview": _body_preview(source_lines, child.start_point[0], child.end_point[0]),
                                    "calls": _collect_calls_ts(child),
                                    "called_by": [],
                                    "imports": [],
                                    "parent_class": mod_name,
                                    "decorators": [],
                                })
    return entities


# ── Dart ──────────────────────────────────────────────────────────────────────

def _dart_imports(root) -> list[str]:
    imports = []
    for node in root.children:
        if node.type == "import_or_export":
            lib = _find_child(node, "library_import")
            if lib:
                uri = _find_child(lib, "uri")
                if not uri:
                    for c in lib.children:
                        if "string" in c.type:
                            imports.append(_node_text(c).strip("'\""))
                            break
                else:
                    imports.append(_node_text(uri).strip("'\""))
    return sorted(set(imports))


def _parse_dart(root, rel_path, source_lines, source) -> list[dict]:
    entities = []
    entities.append(_file_entity(rel_path, "dart", source, source_lines, _dart_imports(root)))

    for node in root.children:
        if node.type == "class_definition":
            cname = _node_text(_find_child(node, "identifier"))
            entities.append({
                "id": _hash_id(rel_path, cname, "class"),
                "kind": "class",
                "name": cname,
                "file_path": rel_path,
                "language": "dart",
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": cname,
                "docstring": "",
                "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                "calls": [],
                "called_by": [],
                "imports": [],
                "parent_class": "",
                "decorators": [],
            })
            body = _find_child(node, "class_body")
            if body:
                for child in body.children:
                    if child.type == "method_signature":
                        # method_signature > function_signature > identifier
                        fsig = _find_child(child, "function_signature")
                        name_node = _find_child(fsig or child, "identifier")
                        mname = _node_text(name_node) if name_node else ""
                        if not mname:
                            continue
                        params = _find_child(fsig or child, "formal_parameter_list")
                        sig = f"{mname}{_node_text(params) if params else '()'}"
                        entities.append({
                            "id": _hash_id(rel_path, f"{cname}.{mname}", "method"),
                            "kind": "method",
                            "name": mname,
                            "file_path": rel_path,
                            "language": "dart",
                            "line_start": child.start_point[0] + 1,
                            "line_end": child.end_point[0] + 1,
                            "signature": sig,
                            "docstring": "",
                            "body_preview": _body_preview(source_lines, child.start_point[0], child.end_point[0]),
                            "calls": _collect_calls_ts(child),
                            "called_by": [],
                            "imports": [],
                            "parent_class": cname,
                            "decorators": [],
                        })
                    elif child.type == "declaration":
                        # constructor_signature inside declaration
                        csig = _find_child(child, "constructor_signature")
                        if csig:
                            mname = _node_text(_find_child(csig, "identifier"))
                            if mname:
                                params = _find_child(csig, "formal_parameter_list")
                                entities.append({
                                    "id": _hash_id(rel_path, f"{cname}.{mname}", "method"),
                                    "kind": "method",
                                    "name": mname,
                                    "file_path": rel_path,
                                    "language": "dart",
                                    "line_start": child.start_point[0] + 1,
                                    "line_end": child.end_point[0] + 1,
                                    "signature": f"{mname}{_node_text(params) if params else '()'}",
                                    "docstring": "",
                                    "body_preview": _body_preview(source_lines, child.start_point[0], child.end_point[0]),
                                    "calls": [],
                                    "called_by": [],
                                    "imports": [],
                                    "parent_class": cname,
                                    "decorators": [],
                                })
        elif node.type == "function_signature":
            fname = _node_text(_find_child(node, "identifier"))
            params = _find_child(node, "formal_parameter_list")
            entities.append({
                "id": _hash_id(rel_path, fname, "function"),
                "kind": "function",
                "name": fname,
                "file_path": rel_path,
                "language": "dart",
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": f"{fname}{_node_text(params) if params else '()'}",
                "docstring": "",
                "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                "calls": _collect_calls_ts(node),
                "called_by": [],
                "imports": [],
                "parent_class": "",
                "decorators": [],
            })
    return entities


# ── Zig ───────────────────────────────────────────────────────────────────────

def _parse_zig(root, rel_path, source_lines, source) -> list[dict]:
    entities = []
    imports = []
    for node in root.children:
        if node.type == "variable_declaration":
            # const std = @import("std") → import
            val = None
            for c in node.children:
                if c.type == "builtin_function":
                    fn_name = _node_text(_find_child(c, "builtin_identifier"))
                    if fn_name == "@import":
                        args = _find_child(c, "arguments")
                        if args:
                            s = _find_child(args, "string_literal", "STRINGLITERALSINGLE")
                            if s:
                                imports.append(_node_text(s).strip('"'))
            # const X = struct { ... }
            struct = None
            for c in node.children:
                if c.type == "struct_declaration":
                    struct = c
                    break
            if struct:
                name = _node_text(_find_child(node, "identifier"))
                entities.append({
                    "id": _hash_id(rel_path, name, "class"),
                    "kind": "class",
                    "name": name,
                    "file_path": rel_path,
                    "language": "zig",
                    "line_start": node.start_point[0] + 1,
                    "line_end": node.end_point[0] + 1,
                    "signature": f"const {name} = struct",
                    "docstring": "",
                    "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                    "calls": [],
                    "called_by": [],
                    "imports": [],
                    "parent_class": "",
                    "decorators": [],
                })

    entities[0:0] = [_file_entity(rel_path, "zig", source, source_lines, sorted(set(imports)))]

    for node in root.children:
        if node.type == "function_declaration":
            fname = _node_text(_find_child(node, "identifier"))
            params = _find_child(node, "parameters")
            ret = None
            for c in node.children:
                if c.type in ("identifier", "builtin_type", "optional_type",
                               "pointer_type", "error_union"):
                    ret = c
                    break
            sig = f"fn {fname}{_node_text(params) if params else '()'}"
            if ret:
                sig += f" {_node_text(ret)}"
            entities.append({
                "id": _hash_id(rel_path, fname, "function"),
                "kind": "function",
                "name": fname,
                "file_path": rel_path,
                "language": "zig",
                "line_start": node.start_point[0] + 1,
                "line_end": node.end_point[0] + 1,
                "signature": sig,
                "docstring": _go_docstring(node, source_lines),
                "body_preview": _body_preview(source_lines, node.start_point[0], node.end_point[0]),
                "calls": _collect_calls_ts(node),
                "called_by": [],
                "imports": [],
                "parent_class": "",
                "decorators": [],
            })
    return entities


# ── Shared helper ─────────────────────────────────────────────────────────────

def _file_entity(rel_path, lang, source, source_lines, imports) -> dict:
    return {
        "id": _hash_id(rel_path, rel_path, "file"),
        "kind": "file",
        "name": rel_path.split("/")[-1],
        "file_path": rel_path,
        "language": lang,
        "line_start": 1,
        "line_end": len(source_lines),
        "signature": "",
        "docstring": "",
        "body_preview": source[:500],
        "calls": [],
        "called_by": [],
        "imports": imports,
        "parent_class": "",
        "decorators": [],
    }


# ── Public API ────────────────────────────────────────────────────────────────

def parse_file(file_path: str, root_path: str) -> list[dict]:
    """
    Parse a source file and return a list of entity documents.
    Detects language from file extension. Returns empty list for
    unsupported languages or parse errors.
    """
    path = Path(file_path)
    rel_path = str(path.relative_to(root_path)).replace("\\", "/")
    ext = path.suffix.lower()

    lang = EXT_TO_LANG.get(ext)
    if not lang:
        return []

    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    ts_lang = LANGUAGES[lang]
    parser = Parser(ts_lang)
    tree = parser.parse(source.encode("utf-8"))
    root = tree.root_node
    source_lines = source.splitlines()

    if lang == "python":
        return _parse_python(root, rel_path, source_lines, source)
    elif lang in ("javascript", "typescript"):
        return _parse_js(root, rel_path, source_lines, source, lang)
    elif lang == "go":
        return _parse_go(root, rel_path, source_lines, source)
    elif lang == "rust":
        return _parse_rust(root, rel_path, source_lines, source)
    elif lang == "java":
        return _parse_java(root, rel_path, source_lines, source)
    elif lang == "csharp":
        return _parse_csharp(root, rel_path, source_lines, source)
    elif lang in ("c", "cpp"):
        return _parse_c_cpp(root, rel_path, source_lines, source, lang)
    elif lang == "ruby":
        return _parse_ruby(root, rel_path, source_lines, source)
    elif lang == "php":
        return _parse_php(root, rel_path, source_lines, source)
    elif lang == "swift":
        return _parse_swift(root, rel_path, source_lines, source)
    elif lang == "kotlin":
        return _parse_kotlin(root, rel_path, source_lines, source)
    elif lang == "scala":
        return _parse_scala(root, rel_path, source_lines, source)
    elif lang == "lua":
        return _parse_lua(root, rel_path, source_lines, source)
    elif lang == "elixir":
        return _parse_elixir(root, rel_path, source_lines, source)
    elif lang == "dart":
        return _parse_dart(root, rel_path, source_lines, source)
    elif lang == "zig":
        return _parse_zig(root, rel_path, source_lines, source)

    return []
