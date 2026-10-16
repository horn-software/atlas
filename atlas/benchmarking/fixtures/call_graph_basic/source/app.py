"""Fixture: basic call graph with clear caller/callee relationships."""


def load_config(path: str) -> dict:
    """Load config from file."""
    import json
    with open(path) as f:
        return json.load(f)


def validate_config(config: dict) -> bool:
    """Validate required keys."""
    return "db_url" in config


def connect_db(config: dict):
    """Connect using config."""
    import os
    url = os.getenv("DATABASE_URL") or config.get("db_url")
    return url


def setup(config_path: str):
    """Entry point: load, validate, connect."""
    config = load_config(config_path)
    if not validate_config(config):
        raise ValueError("bad config")
    return connect_db(config)


class Worker:
    def __init__(self, config: dict):
        self.config = config

    def run(self):
        """Run the worker."""
        return connect_db(self.config)

    def restart(self):
        """Restart by re-running."""
        return self.run()
