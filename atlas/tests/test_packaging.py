"""The public package must never ship closed-source modules, and must degrade cleanly without them."""
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ATLAS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATLAS_DIR))

CLOSED_PATHS = ("keygen.py", "edge/bridge.py", "query/generate_data.py", "query/train.py")


def _gitignore_closed_paths() -> list[str]:
    gitignore = ATLAS_DIR.parent / ".gitignore"
    if not gitignore.is_file():
        pytest.skip("no repository .gitignore (running from an sdist)")
    lines = gitignore.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if "CLOSED SOURCE FILES" in line)
    paths = []
    for line in lines[start + 1:]:
        if not line.strip():
            break
        if line.startswith("atlas/") and line.endswith(".py"):
            paths.append(line[len("atlas/"):])
    return paths


def test_every_gitignored_closed_module_is_excluded_from_the_build():
    setup_text = (ATLAS_DIR / "setup.py").read_text(encoding="utf-8")
    for rel in _gitignore_closed_paths():
        package = "atlas" + "".join("." + p for p in rel.split("/")[:-1])
        module = rel.rsplit("/", 1)[-1][:-3]
        assert re.search(rf'\(\s*"{re.escape(package)}"\s*,\s*"{module}"\s*\)', setup_text), (
            f"{rel} is closed in .gitignore but not excluded in setup.py CLOSED_MODULES"
        )


def test_build_py_omits_closed_modules_but_keeps_open_ones(tmp_path):
    build_lib = tmp_path / "lib"
    subprocess.run(
        [sys.executable, "setup.py", "-q", "build_py", "--build-lib", str(build_lib)],
        cwd=ATLAS_DIR, check=True, capture_output=True,
    )
    built = {p.relative_to(build_lib / "atlas").as_posix() for p in (build_lib / "atlas").rglob("*.py")}
    for rel in CLOSED_PATHS + ("build_pro.py", "check_dist.py"):
        assert rel not in built, f"closed/dev module leaked into build: {rel}"
    for rel in ("cli.py", "server.py", "edge/impact.py", "ingest/call_graph.py", "query/net.py"):
        assert rel in built, f"open module missing from build: {rel}"


def test_check_dist_flags_closed_files_and_passes_clean_wheels(tmp_path):
    import check_dist

    leaky = tmp_path / "leaky-1.0-py3-none-any.whl"
    with zipfile.ZipFile(leaky, "w") as z:
        z.writestr("atlas/cli.py", "")
        z.writestr("atlas/query/train.py", "")
    clean = tmp_path / "clean-1.0-py3-none-any.whl"
    with zipfile.ZipFile(clean, "w") as z:
        z.writestr("atlas/cli.py", "")
        z.writestr("atlas/query/net.py", "")
    assert check_dist.leaks(leaky) == ["atlas/query/train.py"]
    assert check_dist.leaks(clean) == []
    assert check_dist.main([str(leaky)]) == 1
    assert check_dist.main([str(clean)]) == 0


def test_training_degrades_gracefully_without_pro_modules(monkeypatch, tmp_path, capsys):
    import cli

    monkeypatch.setitem(sys.modules, "query.generate_data", None)
    monkeypatch.setitem(sys.modules, "query.train", None)
    assert cli._run_train(tmp_path, "proj") is None
    out = capsys.readouterr().out
    assert "Atlas Pro components" in out and "hornsoftwarearchitects.co.za" in out


def test_edge_check_returns_error_without_bridge_module(monkeypatch):
    import license as atlas_license
    import server

    monkeypatch.setattr(server, "_ensure_meilisearch", lambda: None)
    monkeypatch.setattr(atlas_license, "requires_pro", lambda: None)
    monkeypatch.setitem(sys.modules, "edge.bridge", None)
    result = server._handle_edge_check({"code": "x = 1", "functions": ["f"], "project_id": "p"})
    assert "Atlas Pro components" in result["error"]
