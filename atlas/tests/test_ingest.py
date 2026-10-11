"""
Test the ingest pipeline: crawl, parse, build call graph, index to Meilisearch.
Uses small sample files created in a temp directory.
"""
import json
import os
import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.crawler import crawl
from ingest.parsers import parse_file
from ingest.call_graph import build_call_graph

SAMPLE_PYTHON = '''
"""Sample module for testing Atlas ingestion."""
import os
import json


def load_config(path: str) -> dict:
    """Load configuration from a JSON file."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate_config(config: dict) -> bool:
    """Validate that required keys exist."""
    required = ["name", "version"]
    return all(k in config for k in required)


def setup(config_path: str) -> dict:
    """Load and validate config."""
    config = load_config(config_path)
    if not validate_config(config):
        raise ValueError("Invalid config")
    return config


class DataProcessor:
    """Process data records."""

    def __init__(self, config: dict):
        self.config = config

    def process(self, records: list) -> list:
        """Process a list of records."""
        return [self.transform(r) for r in records]

    def transform(self, record: dict) -> dict:
        """Transform a single record."""
        record["processed"] = True
        return record
'''

SAMPLE_JS = '''
import express from "express";

class UserService {
    constructor(db) {
        this.db = db;
    }
    async findUser(id) {
        return this.db.query(id);
    }
}

function createApp(config) {
    const app = express();
    return app;
}

const helper = (x) => x + 1;
'''

SAMPLE_GO = '''
package main

import (
    "fmt"
    "net/http"
)

// Server handles HTTP requests.
type Server struct {
    Host string
    Port int
}

// Start begins listening on the configured address.
func (s *Server) Start() error {
    addr := fmt.Sprintf("%s:%d", s.Host, s.Port)
    return http.ListenAndServe(addr, nil)
}

// NewServer creates a new Server instance.
func NewServer(host string, port int) *Server {
    return &Server{Host: host, Port: port}
}
'''

REQUIRED_FIELDS = {
    "id", "kind", "name", "file_path", "language",
    "line_start", "line_end", "signature", "docstring",
    "body_preview", "calls", "called_by", "imports",
    "parent_class", "decorators",
}


def test_crawler():
    """Test that the crawler finds multi-language files and skips hidden/venv/node_modules dirs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        src = Path(tmpdir) / "src"
        src.mkdir()
        (src / "main.py").write_text("print('hello')", encoding="utf-8")
        (src / "utils.js").write_text("const x = 1;", encoding="utf-8")
        (src / "server.go").write_text("package main", encoding="utf-8")
        (src / "types.ts").write_text("type X = string;", encoding="utf-8")
        (src / "notes.txt").write_text("not code", encoding="utf-8")

        hidden = src / ".hidden"
        hidden.mkdir()
        (hidden / "secret.py").write_text("pass", encoding="utf-8")

        venv = Path(tmpdir) / "venv"
        venv.mkdir()
        (venv / "activate.py").write_text("pass", encoding="utf-8")

        nm = Path(tmpdir) / "node_modules"
        nm.mkdir()
        (nm / "index.js").write_text("module.exports = {}", encoding="utf-8")

        vendor = Path(tmpdir) / "vendor"
        vendor.mkdir()
        (vendor / "lib.go").write_text("package vendor", encoding="utf-8")

        files = crawl(tmpdir)
        names = [Path(f).name for f in files]
        assert "main.py" in names, f"main.py not found: {names}"
        assert "utils.js" in names, f"utils.js not found: {names}"
        assert "server.go" in names, f"server.go not found: {names}"
        assert "types.ts" in names, f"types.ts not found: {names}"
        assert "notes.txt" not in names, "should skip non-code files"
        assert "secret.py" not in names, "should skip hidden dirs"
        assert "activate.py" not in names, "should skip venv"
        assert "index.js" not in names, "should skip node_modules"
        assert "lib.go" not in names, "should skip vendor"
        print("  PASS test_crawler")


def test_parser():
    """Test that the parser extracts functions, classes, methods, and imports from Python."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = Path(tmpdir) / "sample.py"
        fpath.write_text(SAMPLE_PYTHON, encoding="utf-8")

        entities = parse_file(str(fpath), tmpdir)
        kinds = {e["kind"] for e in entities}
        names = {e["name"] for e in entities}

        assert "file" in kinds, "should have file entity"
        assert "function" in kinds, "should have function entities"
        assert "class" in kinds, "should have class entity"
        assert "method" in kinds, "should have method entities"

        assert "load_config" in names, "should find load_config"
        assert "validate_config" in names, "should find validate_config"
        assert "setup" in names, "should find setup"
        assert "DataProcessor" in names, "should find DataProcessor"
        assert "process" in names, "should find process method"
        assert "transform" in names, "should find transform method"

        setup_fn = [e for e in entities if e["name"] == "setup"][0]
        assert "load_config" in setup_fn["calls"], \
            f"setup should call load_config: {setup_fn['calls']}"
        assert "validate_config" in setup_fn["calls"], \
            f"setup should call validate_config: {setup_fn['calls']}"

        file_ent = [e for e in entities if e["kind"] == "file"][0]
        assert "os" in file_ent["imports"], f"should import os: {file_ent['imports']}"
        assert "json" in file_ent["imports"], f"should import json: {file_ent['imports']}"

        print("  PASS test_parser")


def test_parser_js():
    """Test JavaScript parsing: functions, classes, methods, arrow fns, imports."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = Path(tmpdir) / "app.js"
        fpath.write_text(SAMPLE_JS, encoding="utf-8")

        entities = parse_file(str(fpath), tmpdir)
        names = {e["name"] for e in entities}
        kinds = {e["kind"] for e in entities}

        assert "file" in kinds, "should have file entity"
        assert "function" in kinds, "should have function entities"
        assert "class" in kinds, "should have class entity"
        assert "method" in kinds, "should have method entities"

        assert "UserService" in names, "should find UserService class"
        assert "findUser" in names, "should find findUser method"
        assert "createApp" in names, "should find createApp function"
        assert "helper" in names, "should find helper arrow function"

        for e in entities:
            assert e["language"] == "javascript", \
                f"all entities should be javascript: {e['name']} is {e['language']}"

        file_ent = [e for e in entities if e["kind"] == "file"][0]
        assert "express" in file_ent["imports"], \
            f"should import express: {file_ent['imports']}"

        find_user = [e for e in entities if e["name"] == "findUser"][0]
        assert find_user["parent_class"] == "UserService", \
            f"findUser should belong to UserService: {find_user['parent_class']}"

        print("  PASS test_parser_js")


def test_parser_go():
    """Test Go parsing: funcs, methods, structs (as class), imports, docstrings."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = Path(tmpdir) / "main.go"
        fpath.write_text(SAMPLE_GO, encoding="utf-8")

        entities = parse_file(str(fpath), tmpdir)
        names = {e["name"] for e in entities}
        kinds = {e["kind"] for e in entities}

        assert "file" in kinds, "should have file entity"
        assert "function" in kinds, "should have function entities"
        assert "class" in kinds, "Go struct should map to class"
        assert "method" in kinds, "Go method should map to method"

        assert "Server" in names, "should find Server struct"
        assert "Start" in names, "should find Start method"
        assert "NewServer" in names, "should find NewServer function"

        for e in entities:
            assert e["language"] == "go", \
                f"all entities should be go: {e['name']} is {e['language']}"

        server = [e for e in entities if e["name"] == "Server"][0]
        assert server["kind"] == "class", f"Server should be class: {server['kind']}"

        start = [e for e in entities if e["name"] == "Start"][0]
        assert start["kind"] == "method", f"Start should be method: {start['kind']}"
        assert start["parent_class"] == "Server", \
            f"Start should belong to Server: {start['parent_class']}"
        assert "Sprintf" in start["calls"], \
            f"Start should call Sprintf: {start['calls']}"

        file_ent = [e for e in entities if e["kind"] == "file"][0]
        assert "fmt" in file_ent["imports"], f"should import fmt: {file_ent['imports']}"
        assert "net/http" in file_ent["imports"], \
            f"should import net/http: {file_ent['imports']}"

        start_ent = [e for e in entities if e["name"] == "Start"][0]
        assert start_ent["docstring"], "Start should have a docstring from // comment"

        print("  PASS test_parser_go")


def test_call_graph():
    """Test that called_by edges are populated correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fpath = Path(tmpdir) / "sample.py"
        fpath.write_text(SAMPLE_PYTHON, encoding="utf-8")

        entities = parse_file(str(fpath), tmpdir)
        entities = build_call_graph(entities)

        load_config = [e for e in entities if e["name"] == "load_config"][0]
        assert "setup" in load_config["called_by"], \
            f"load_config should be called by setup: {load_config['called_by']}"

        validate_config = [e for e in entities if e["name"] == "validate_config"][0]
        assert "setup" in validate_config["called_by"], \
            f"validate_config should be called by setup: {validate_config['called_by']}"

        transform = [e for e in entities if e["name"] == "transform" and e["kind"] == "method"][0]
        assert "process" in transform["called_by"], \
            f"transform should be called by process: {transform['called_by']}"

        print("  PASS test_call_graph")


def test_entity_schema():
    """Test that entity documents match the expected schema across all languages."""
    with tempfile.TemporaryDirectory() as tmpdir:
        samples = {
            "sample.py": SAMPLE_PYTHON,
            "app.js": SAMPLE_JS,
            "main.go": SAMPLE_GO,
        }
        for fname, code in samples.items():
            fpath = Path(tmpdir) / fname
            fpath.write_text(code, encoding="utf-8")

        for fname in samples:
            fpath = Path(tmpdir) / fname
            entities = parse_file(str(fpath), tmpdir)
            for ent in entities:
                missing = REQUIRED_FIELDS - set(ent.keys())
                assert not missing, \
                    f"Entity {ent['name']} in {fname} missing fields: {missing}"

        print("  PASS test_entity_schema")


if __name__ == "__main__":
    print("Running ingest tests...")
    test_crawler()
    test_parser()
    test_parser_js()
    test_parser_go()
    test_call_graph()
    test_entity_schema()
    print("All ingest tests passed.")
