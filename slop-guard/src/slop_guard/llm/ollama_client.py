"""LLM clients for the semantic detector.

Two backends:
* ``OllamaClient``  — talks to a local ollama server (default model qwen2.5:7b),
  uses structured JSON output.
* ``OfflineSnapshotClient`` — resolves API existence against locally stored
  "API signature snapshots" (JSON files listing real members per package).
  This keeps CI deterministic and works without GPU/network; it is also what
  the unit tests use.

Both implement :class:`ApiExistenceChecker` so the semantic detector can be
backed by either, or by both with consensus scoring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

import httpx

from .prompts import API_EXISTENCE_PROMPT


@dataclass
class ExistenceAnswer:
    exists: Optional[bool]      # None = unknown / could not verify
    confidence: float
    suggestion: Optional[str] = None
    source: str = ""


class ApiExistenceChecker(Protocol):
    def check(self, library: str, member: str, version: str = "") -> ExistenceAnswer: ...


# ---------------------------------------------------------------------------
# Offline snapshot backend
# ---------------------------------------------------------------------------

class OfflineSnapshotClient:
    """Checks membership against bundled/known API snapshots.

    Snapshot format (one JSON file per top-level package)::

        {"package": "pandas", "version": "2.2.0",
         "members": {"DataFrame": ["to_parquet", "to_csv", ...],
                     "_any": [...]}}

    ``_any`` lists members reachable anywhere in the package.
    """

    source = "snapshot"

    def __init__(self, snapshot_dir: Path | str):
        self.dir = Path(snapshot_dir)
        self._cache: dict[str, dict] = {}

    def _load(self, package: str) -> Optional[dict]:
        if package in self._cache:
            return self._cache[package]
        f = self.dir / f"{package}.json"
        if not f.exists():
            return None
        data = json.loads(f.read_text(encoding="utf-8"))
        self._cache[package] = data
        return data

    def check(self, library: str, member: str, version: str = "") -> ExistenceAnswer:
        parts = library.split(".")
        pkg = parts[0]
        subpath = ".".join(parts[1:])          # e.g. 'nn.functional' or 'DataFrame'
        data = self._load(pkg)
        if data is None:
            return ExistenceAnswer(None, 0.0, source=self.source)  # unknown package
        members = data.get("members", {})
        # Resolution order (most specific first):
        #   1. exact dotted class key      ("DataFrame")
        #   2. last path segment           ("nn.functional" -> "functional")
        #   3. alias keys like "F"
        #   4. package-wide "_any" pool
        pools: list[list[str]] = []
        if subpath:
            for key in (subpath, subpath.split(".")[-1]):
                if key in members:
                    pools.append(list(members[key]))
            if len(parts) > 1 and parts[-1] in members:
                pools.append(list(members[parts[-1]]))
        pools.append(list(members.get("_any", [])))
        flat = [m for pool in pools for m in pool]
        if not flat:
            # nothing known about this package path -> unverifiable, not a hallucination
            return ExistenceAnswer(None, 0.0, source=self.source)
        if member in flat:
            return ExistenceAnswer(True, 1.0, source=self.source)
        # If no specific class pool existed, only the _any pool was consulted;
        # absence from _any alone is weak evidence — report as unverifiable
        # unless we had a class-level pool to compare against.
        has_class_pool = bool(subpath) and any(
            k in members for k in (subpath, subpath.split(".")[-1], parts[-1])
        )
        if not has_class_pool:
            return ExistenceAnswer(None, 0.0, source=self.source)
        suggestion = self._closest(member, flat)
        return ExistenceAnswer(False, 0.95, suggestion=suggestion, source=self.source)

    @staticmethod
    def _closest(name: str, pool: list[str], max_dist: int = 4) -> Optional[str]:
        import difflib

        best = difflib.get_close_matches(name, pool, n=1, cutoff=0.6)
        return best[0] if best else None


# ---------------------------------------------------------------------------
# Ollama backend
# ---------------------------------------------------------------------------

class OllamaClient:
    source = "ollama"

    def __init__(self, base_url: str = "http://localhost:11434", model: str = "qwen2.5:7b",
                 timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def check(self, library: str, member: str, version: str = "") -> ExistenceAnswer:
        prompt = API_EXISTENCE_PROMPT.format(
            lib_name=library, version=version or "latest", method_name=member
        )
        try:
            resp = httpx.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            raw = resp.json().get("response", "")
            data = json.loads(raw)
        except Exception:
            # LLM unavailable -> cannot verify; never fail-open as an error
            return ExistenceAnswer(None, 0.0, source=self.source)
        answer = str(data.get("exists", "")).strip().upper()
        if not answer:
            exists = None
        else:
            exists = answer.startswith("YES") or answer == "TRUE"
        conf = float(data.get("confidence", 0.5))
        suggestion = data.get("suggestion") or None
        return ExistenceAnswer(exists, conf, suggestion, source=self.source)


def build_checker(base_url: str, model: str, timeout: float,
                  known_apis_dir: Optional[str]) -> list[ApiExistenceChecker]:
    checkers: list[ApiExistenceChecker] = []
    if known_apis_dir:
        checkers.append(OfflineSnapshotClient(known_apis_dir))
    try:  # only add ollama if the server actually responds
        httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=2.0).raise_for_status()
        checkers.append(OllamaClient(base_url, model, timeout))
    except Exception:
        pass
    return checkers
