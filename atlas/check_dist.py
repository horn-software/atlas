"""Fail if a public build artifact contains closed-source files. Run before every upload.

Usage: python check_dist.py dist/hornsa_atlas-X.Y.Z*
"""
import sys
import tarfile
import zipfile
from pathlib import Path

FORBIDDEN_SUFFIXES = (
    "keygen.py", "edge/bridge.py", "query/generate_data.py", "query/train.py",
    "build_pro.py", "query/data.jsonl", "query/model/query_model.pt",
)


def names_in(path: Path) -> list[str]:
    if path.suffix == ".whl":
        return zipfile.ZipFile(path).namelist()
    return tarfile.open(path).getnames()


def leaks(path: Path) -> list[str]:
    found = []
    for name in names_in(path):
        norm = name.replace("\\", "/")
        base = norm.rsplit("/", 1)[-1]
        if norm.endswith(FORBIDDEN_SUFFIXES) or (base.startswith("private") and base.endswith(".pem")):
            found.append(name)
    return found


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv]
    if not paths:
        print("usage: check_dist.py <artifact> [...]", file=sys.stderr)
        return 2
    bad = False
    for path in paths:
        found = leaks(path)
        print(f"{'LEAK' if found else 'ok  '} {path.name}" + (f": {found}" if found else ""))
        bad = bad or bool(found)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
