"""
Atlas CLI — one-command setup and management.

Commands:
  atlas init              Set up Atlas in the current project directory
  atlas status            Show index stats for the current project
  atlas retrain           Regenerate training data and retrain the query model
  atlas activate <KEY>    Activate a license key
  atlas license           Show current license status
"""
import argparse
import json
import os
import platform
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

MEILI_URL = "http://localhost:7700"
MEILI_KEY = "poc_master_key_local_1234567890"

MEILI_URLS = {
    ("linux", "x86_64"): "https://github.com/meilisearch/meilisearch/releases/latest/download/meilisearch-linux-amd64",
    ("linux", "aarch64"): "https://github.com/meilisearch/meilisearch/releases/latest/download/meilisearch-linux-aarch64",
    ("darwin", "x86_64"): "https://github.com/meilisearch/meilisearch/releases/latest/download/meilisearch-macos-amd64",
    ("darwin", "arm64"): "https://github.com/meilisearch/meilisearch/releases/latest/download/meilisearch-macos-apple-silicon",
    ("windows", "amd64"): "https://github.com/meilisearch/meilisearch/releases/latest/download/meilisearch-windows-amd64.exe",
}


def _meili_platform_key():
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        return ("windows", "amd64")
    if system == "darwin":
        return ("darwin", "arm64") if machine in ("arm64", "aarch64") else ("darwin", "x86_64")
    return ("linux", "aarch64") if machine in ("arm64", "aarch64") else ("linux", "x86_64")


def _meili_running() -> bool:
    url = os.environ.get("MEILI_URL", MEILI_URL)
    key = os.environ.get("MEILI_KEY", MEILI_KEY)
    try:
        import urllib.request as ur
        req = ur.Request(f"{url}/health", headers={"Authorization": f"Bearer {key}"})
        with ur.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def _start_meilisearch(project_dir: Path):
    key = _meili_platform_key()
    url = MEILI_URLS.get(key)
    if not url:
        print(f"  [ERROR] Unsupported platform: {platform.system()} {platform.machine()}")
        print("    Download Meilisearch manually from https://www.meilisearch.com/docs/learn/getting_started/installation")
        sys.exit(1)

    data_dir = project_dir / ".atlas" / "meili_data"
    bin_dir = project_dir / ".atlas" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    suffix = ".exe" if platform.system().lower() == "windows" else ""
    bin_path = bin_dir / f"meilisearch{suffix}"

    if not bin_path.exists():
        print(f"  Downloading Meilisearch for {platform.system()} {platform.machine()} ...")
        print(f"  from {url}")
        try:
            urllib.request.urlretrieve(url, str(bin_path))
        except Exception as e:
            print(f"  [ERROR] Download failed: {e}")
            print("    Download Meilisearch manually from https://www.meilisearch.com/docs/learn/getting_started/installation")
            sys.exit(1)

        if platform.system().lower() != "windows":
            bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        print(f"  Saved to {bin_path}")

    data_dir.mkdir(parents=True, exist_ok=True)
    print("  Starting Meilisearch in the background ...")
    subprocess.Popen(
        [str(bin_path), "--db-path", str(data_dir), "--master-key", MEILI_KEY, "--no-analytics"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for _ in range(30):
        time.sleep(1)
        if _meili_running():
            print("  Meilisearch is up.")
            return

    print("  [ERROR] Meilisearch did not start within 30 seconds.")
    print(f"    Run it manually: {bin_path} --db-path {data_dir} --master-key {MEILI_KEY}")
    sys.exit(1)


def _ensure_env(project_dir: Path):
    env_path = project_dir / ".env"
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        if "MEILI_URL" in content and "MEILI_KEY" in content:
            return
        with open(env_path, "a", encoding="utf-8") as f:
            if "MEILI_URL" not in content:
                f.write(f"\nMEILI_URL={MEILI_URL}\n")
            if "MEILI_KEY" not in content:
                f.write(f"MEILI_KEY={MEILI_KEY}\n")
        print(f"  Updated {env_path}")
    else:
        env_path.write_text(
            f"MEILI_URL={MEILI_URL}\nMEILI_KEY={MEILI_KEY}\n",
            encoding="utf-8",
        )
        print(f"  Created {env_path}")


def _load_env(project_dir: Path):
    """Load .env from project_dir into os.environ; fall back to module defaults."""
    env_path = project_dir / ".env"
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(str(env_path), override=True)
            return
        except ImportError:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
    os.environ.setdefault("MEILI_URL", MEILI_URL)
    os.environ.setdefault("MEILI_KEY", MEILI_KEY)


def _run_ingest(project_dir: Path, project_id: str) -> dict:
    _load_env(project_dir)

    from ingest.crawler import crawl
    from ingest.parsers import parse_file
    from ingest.call_graph import build_call_graph
    from ingest.indexer import create_index, index_documents

    root = str(project_dir)
    files = crawl(root)
    if not files:
        return {"indexed": 0, "files": 0, "entities": 0}

    entities = []
    for fpath in files:
        entities.extend(parse_file(fpath, root))
    entities = build_call_graph(entities)

    create_index(project_id)
    count = index_documents(project_id, entities)

    bridges_count = 0
    try:
        from ingest.bridge_resolver import resolve_all_bridges
        bridges = resolve_all_bridges(entities, project_id)
        if bridges:
            index_documents(project_id, bridges)
            bridges_count = len(bridges)
    except Exception:
        pass

    return {
        "indexed": count + bridges_count,
        "files": len(files),
        "entities": len(entities),
        "bridges": bridges_count,
    }


def _run_train(project_dir: Path, project_id: str, count: int = 2500, epochs: int = 30):
    from ingest.crawler import crawl
    from ingest.parsers import parse_file
    from ingest.call_graph import build_call_graph
    from query.generate_data import generate_from_entities
    from query.train import train as run_train

    root = str(project_dir)
    files = crawl(root)
    entities = []
    for fpath in files:
        entities.extend(parse_file(fpath, root))
    entities = build_call_graph(entities)

    examples = generate_from_entities(entities, target_count=count)

    model_dir = project_dir / ".atlas" / "model"
    model_dir.mkdir(parents=True, exist_ok=True)

    data_path = model_dir / "data.jsonl"
    with open(data_path, "w", encoding="utf-8") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")
    print(f"  Generated {len(examples)} training examples -> {data_path}")

    import types
    train_args = types.SimpleNamespace(
        data=str(data_path),
        epochs=epochs,
        lr=0.001,
        batch=32,
        model_dir=str(model_dir),
    )
    run_train(train_args)


def cmd_init(project_dir: Path):
    project_id = project_dir.name.lower().replace(" ", "-")
    _load_env(project_dir)

    print(f"\nAtlas init — {project_dir}")
    print(f"  project_id: {project_id}")
    print()

    print("[1/4] Checking Meilisearch ...")
    if _meili_running():
        print("  Meilisearch already running.")
    else:
        _start_meilisearch(project_dir)

    print()
    print("[2/4] Writing .env ...")
    _ensure_env(project_dir)

    print()
    print("[3/4] Ingesting codebase ...")
    result = _run_ingest(project_dir, project_id)
    print(f"  {result['files']} files  |  {result['entities']} entities  |  {result['bridges']} bridges  ->  {result['indexed']} documents indexed")

    print()
    print("[4/4] Training query model ...")
    from license import load_license
    lic = load_license()
    if lic["valid"] and lic["tier"] in ("pro", "team", "enterprise"):
        _run_train(project_dir, project_id)
    else:
        print("  Skipped — query model training requires Atlas Pro.")
        print(f"  Upgrade at: https://horn-dev.co.za/atlas")

    _write_mcp_config(project_dir, project_id)
    print()
    print("=" * 60)
    print("Atlas is ready.")
    print(f"  project_id: {project_id}")
    print(f"  tools: atlas_ingest  atlas_query  atlas_impact  atlas_edge_check  atlas_status")
    print("=" * 60)


def _write_mcp_config(project_dir: Path, project_id: str):
    """Write (or merge) the Atlas MCP entry into .claude/settings.json."""
    atlas_path = str(_HERE / "server.py").replace("\\", "/")
    cwd_str = str(project_dir).replace("\\", "/")

    entry = {
        "command": "python",
        "args": [atlas_path],
        "cwd": cwd_str,
    }

    settings_dir = project_dir / ".claude"
    settings_path = settings_dir / "settings.json"
    settings_dir.mkdir(exist_ok=True)

    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            settings = {}
    else:
        settings = {}

    servers = settings.setdefault("mcpServers", {})
    already_correct = servers.get("atlas") == entry

    servers["atlas"] = entry
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    if already_correct:
        print()
        print(f"  .claude/settings.json already up to date.")
    else:
        print()
        print(f"  Wrote MCP config -> {settings_path}")


def cmd_status(project_dir: Path):
    project_id = project_dir.name.lower().replace(" ", "-")
    _load_env(project_dir)

    if not _meili_running():
        print(f"Meilisearch is not running at {MEILI_URL}")
        return

    from ingest.indexer import get_index_stats
    stats = get_index_stats(project_id)
    if not stats.get("exists"):
        print(f"Project '{project_id}' has not been indexed. Run: atlas init")
        return

    print(f"\nAtlas status — {project_dir.name}")
    print(f"  project_id:  {project_id}")
    print(f"  index_name:  {stats.get('index_name', '')}")
    print(f"  documents:   {stats.get('numberOfDocuments', 0):,}")
    print(f"  indexing:    {'yes' if stats.get('isIndexing') else 'no'}")

    model_dir = project_dir / ".atlas" / "model"
    if (model_dir / "query_model.pt").exists():
        print(f"  query model: trained  ({model_dir})")
    else:
        print("  query model: not trained — run: atlas retrain")


def cmd_retrain(project_dir: Path):
    from license import requires_pro
    requires_pro()
    project_id = project_dir.name.lower().replace(" ", "-")
    print(f"\nAtlas retrain — {project_dir.name}")
    print("Regenerating training data and retraining model ...")
    _run_train(project_dir, project_id)
    print("Done.")


def cmd_activate(key: str):
    from license import activate
    result = activate(key)
    if not result["valid"]:
        print(f"Activation failed: {result['error']}")
        sys.exit(1)
    print(f"\nAtlas activated.")
    print(f"  tier:    {result['tier'].capitalize()}")
    print(f"  email:   {result['email']}")
    print(f"  expiry:  {result['expiry']}  ({result['days_remaining']} days remaining)")


def cmd_license():
    from license import load_license
    info = load_license()
    print(f"\nAtlas license")
    if not info["valid"]:
        print(f"  status:  Free  (no active license)")
        if info["error"]:
            print(f"  note:    {info['error']}")
        print(f"\n  Upgrade to Pro at: https://horn-dev.co.za/atlas")
        return
    print(f"  status:  Active")
    print(f"  tier:    {info['tier'].capitalize()}")
    print(f"  email:   {info['email']}")
    print(f"  expiry:  {info['expiry']}  ({info['days_remaining']} days remaining)")


def main():
    parser = argparse.ArgumentParser(
        prog="atlas",
        description="Atlas CLI — one-command setup and management",
    )
    parser.add_argument(
        "command",
        choices=["init", "status", "retrain", "activate", "license"],
        help="Command to run",
    )
    parser.add_argument(
        "key",
        nargs="?",
        default=None,
        help="License key (for: atlas activate <KEY>)",
    )
    parser.add_argument(
        "--dir",
        default=None,
        help="Project directory (default: current working directory)",
    )
    args = parser.parse_args()

    project_dir = Path(args.dir).resolve() if args.dir else Path.cwd()

    if args.command == "init":
        cmd_init(project_dir)
    elif args.command == "status":
        cmd_status(project_dir)
    elif args.command == "retrain":
        cmd_retrain(project_dir)
    elif args.command == "activate":
        if not args.key:
            print("Usage: atlas activate <KEY>")
            sys.exit(1)
        cmd_activate(args.key)
    elif args.command == "license":
        cmd_license()


if __name__ == "__main__":
    main()
