"""Build the private Atlas Pro wheel (hornsa-atlas-pro).

Contains only the closed-source Pro modules, laid out so they install next to
the public package's files. Output goes to dist-pro/ (git-ignored). Never upload
this wheel to PyPI: it carries the 'Private :: Do Not Upload' classifier, which
makes PyPI reject it. Deliver it to licensed customers through your own channel.

Usage: python build_pro.py
"""
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRO_FILES = {
    "edge/bridge.py": "atlas/edge/bridge.py",
    "query/generate_data.py": "atlas/query/generate_data.py",
    "query/train.py": "atlas/query/train.py",
}

PYPROJECT = """\
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "hornsa-atlas-pro"
version = "{version}"
description = "Atlas Pro components (proprietary, licensed customers only)"
requires-python = ">=3.11"
license = {{ text = "Proprietary - Horn Software Architects Commercial Licence" }}
classifiers = ["Private :: Do Not Upload"]
dependencies = ["hornsa-atlas>={version}"]

[tool.setuptools.packages.find]
where = ["."]
include = ["atlas*"]
namespaces = true
"""


def main() -> int:
    version = tomllib.loads((HERE / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    missing = [src for src in PRO_FILES if not (HERE / src).is_file()]
    if missing:
        print(f"Missing Pro source files: {missing}", file=sys.stderr)
        return 1

    out_dir = HERE.parent / "dist-pro"
    out_dir.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        for src, dest in PRO_FILES.items():
            target = stage / dest
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(HERE / src, target)
        (stage / "pyproject.toml").write_text(PYPROJECT.format(version=version), encoding="utf-8")
        subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir), str(stage)],
            check=True,
        )
    print(f"Built Pro wheel for {version} in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
