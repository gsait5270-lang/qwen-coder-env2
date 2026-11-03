"""Clean, human-like code: must produce zero findings."""

import json
from pathlib import Path


def load_config(path: Path) -> dict:
    """Read JSON config; retry once on decode error because CI caches
    sometimes deliver truncated files."""
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return json.loads(path.read_text().rstrip(",\n "))


def merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if value is not None:
            out[key] = value
    return out
