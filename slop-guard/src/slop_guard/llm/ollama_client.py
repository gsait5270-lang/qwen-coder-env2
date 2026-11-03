"""LLM clients for the semantic detector.

Three cooperating pieces:

* ``OllamaClient``  — talks to a local ollama server (default model
  qwen2.5:7b), uses structured JSON output, tolerant parsing and an
  in-memory + on-disk answer cache (10 identical calls = 1 request).
* ``OfflineSnapshotClient`` — resolves API existence against locally stored
  "API signature snapshots" (JSON files listing real members per package),
  with a **version cascade**: an exact-version snapshot
  (``pandas@2.2.3.json``) wins over the generic one (``pandas.json``).
* ``CachingChecker`` — decorator adding the LLM-answer cache around any
  checker.

All implement :class:`ApiExistenceChecker` so the semantic detector can be
backed by any combination with consensus scoring.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

import httpx

from .prompts import API_EXISTENCE_PROMPT

log = logging.getLogger("slop_guard.llm")


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

    ``_any`` lists members reachable anywhere in the package. Versioned
    snapshots are named ``<package>@<version>.json`` and take precedence when
    the caller supplies (or has configured) that exact version.
    """

    source = "snapshot"

    def __init__(self, snapshot_dir: Path | str):
        self.dir = Path(snapshot_dir)
        self._cache: dict[str, Optional[dict]] = {}

    # -- loading ------------------------------------------------------------

    def _load(self, package: str, version: str = "") -> Optional[dict]:
        key = f"{package}@{version}" if version else package
        if key in self._cache:
            return self._cache[key]
        data = None
        candidates: list[Path] = []
        if version:
            candidates.append(self.dir / f"{package}@{version}.json")
        candidates.append(self.dir / f"{package}.json")
        for f in candidates:
            if f.exists():
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    log.warning("corrupt snapshot %s: %s", f, exc)
                    data = None
                break
        self._cache[key] = data
        return data

    # -- checking -------------------------------------------------------------

    def check(self, library: str, member: str, version: str = "") -> ExistenceAnswer:
        parts = library.split(".")
        pkg = parts[0]
        subpath = ".".join(parts[1:])          # e.g. 'nn.functional' or 'DataFrame'
        data = self._load(pkg, version)
        if data is None:
            return ExistenceAnswer(None, 0.0, source=self.source)  # unknown package
        members = data.get("members", {})

        # Resolution order (most specific first):
        #   1. exact dotted class/module key ("DataFrame", "nn.functional")
        #   2. every suffix of the path     ("functional")
        #   3. alias keys like "F"
        #   4. package-wide "_any" pool
        pools: list[list[str]] = []
        seen_keys: set[str] = set()
        if subpath:
            keys = [subpath]
            segs = subpath.split(".")
            for i in range(len(segs)):
                keys.append(".".join(segs[i:]))
            for k in keys:
                if k in members and k not in seen_keys:
                    seen_keys.add(k)
                    pools.append(list(members[k]))
        pools.append(list(members.get("_any", [])))
        flat = [m for pool in pools for m in pool]
        if not flat:
            # nothing known about this package path -> unverifiable, not a hallucination
            return ExistenceAnswer(None, 0.0, source=self.source)
        if member in flat:
            return ExistenceAnswer(True, 1.0, source=self.source)

        # Absence from *only* the coarse `_any` pool is weak evidence: `_any`
        # is a verified subset, not the full surface. Report unverifiable
        # unless we had a class/module-level pool to compare against.
        has_class_pool = bool(subpath) and any(
            k in members for k in ([subpath] + [
                ".".join(subpath.split(".")[i:]) for i in range(len(subpath.split(".")))
            ])
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
# Answer cache (P1-10: 10 identical calls must be 1 request)
# ---------------------------------------------------------------------------

class CachingChecker:
    """Memoizes :meth:`check` results; optionally persists to disk."""

    source = "cache"

    def __init__(self, inner: ApiExistenceChecker, cache_file: Optional[Path] = None):
        self.inner = inner
        self.source = getattr(inner, "source", "cached")
        self.cache_file = Path(cache_file) if cache_file else None
        self._mem: dict[tuple[str, str, str], ExistenceAnswer] = {}
        if self.cache_file and self.cache_file.exists():
            try:
                raw = json.loads(self.cache_file.read_text(encoding="utf-8"))
                for k, v in raw.items():
                    lib, member, ver = k.split("\x1f")
                    self._mem[(lib, member, ver)] = ExistenceAnswer(**v)
            except Exception as exc:  # corrupted cache -> start fresh
                log.warning("ignoring corrupt LLM cache %s: %s", self.cache_file, exc)

    def check(self, library: str, member: str, version: str = "") -> ExistenceAnswer:
        key = (library, member, version)
        if key not in self._mem:
            ans = self.inner.check(library, member, version)
            # do not cache unknown answers — retry conditions may vanish later
            self._mem[key] = ans if ans.exists is not None else None
            if ans.exists is None:
                return ans
        cached = self._mem[key]
        assert cached is not None
        return cached

    def flush(self) -> None:
        if not self.cache_file:
            return
        payload = {
            "\x1f".join(k): {
                "exists": v.exists, "confidence": v.confidence,
                "suggestion": v.suggestion, "source": v.source,
            }
            for k, v in self._mem.items() if v is not None
        }
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        except OSError as exc:
            log.warning("cannot persist LLM cache: %s", exc)


# ---------------------------------------------------------------------------
# Ollama backend
# ---------------------------------------------------------------------------

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.S)
_CONF_ALIASES = {"high": 0.9, "medium": 0.6, "med": 0.6, "low": 0.3}


def parse_llm_response(raw: str) -> tuple[Optional[bool], float, Optional[str]]:
    """Tolerant parser for the JSON-mode LLM reply.

    Handles: fenced code blocks, ``"confidence": "high"`` string confidences,
    booleans instead of YES/NO, missing fields. Returns (None, 0.0, None) on
    anything unparseable — never guesses.
    """
    m = _JSON_OBJECT_RE.search(raw or "")
    if not m:
        return None, 0.0, None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None, 0.0, None
    if not isinstance(data, dict):
        return None, 0.0, None

    ex = data.get("exists")
    exists: Optional[bool]
    if isinstance(ex, bool):
        exists = ex
    elif isinstance(ex, str):
        token = ex.strip().upper()
        if token.startswith("YES") or token in ("TRUE", "EXISTS"):
            exists = True
        elif token.startswith("NO") or token in ("FALSE", "NOT_EXISTS", "DOES_NOT_EXIST"):
            exists = False
        else:
            exists = None
    else:
        exists = None

    cf = data.get("confidence", 0.5)
    if isinstance(cf, str):
        cf = _CONF_ALIASES.get(cf.strip().lower(), 0.5)
    try:
        conf = max(0.0, min(1.0, float(cf)))
    except (TypeError, ValueError):
        conf = 0.5

    sug = data.get("suggestion")
    suggestion = str(sug).strip() if sug not in (None, "") else None
    return exists, conf, suggestion


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
        t0 = time.monotonic()
        try:
            resp = httpx.post(
                f"{self.base_url}/api/generate",
                json={"model": self.model, "prompt": prompt,
                      "stream": False, "format": "json"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            raw = resp.json().get("response", "")
        except Exception as exc:
            # LLM unavailable -> cannot verify; never fail-open as an error
            log.warning("ollama request failed (%s); answer=unknown", exc)
            return ExistenceAnswer(None, 0.0, source=self.source)
        exists, conf, suggestion = parse_llm_response(raw)
        log.debug("ollama %s.%s -> %s (%.1fs)", library, member, exists,
                  time.monotonic() - t0)
        return ExistenceAnswer(exists, conf, suggestion, source=self.source)


def probe_ollama(base_url: str, timeout: float = 1.0) -> bool:
    """Cheap reachability probe; short budget so scans don't stall (P1-10)."""
    try:
        r = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=timeout)
        ok = r.status_code == 200
        if not ok:
            log.info("ollama probe returned HTTP %s", r.status_code)
        return ok
    except Exception as exc:
        log.info("ollama unreachable at %s (%s) — using snapshots only",
                 base_url, exc.__class__.__name__)
        return False


def build_checker(
    base_url: str,
    model: str,
    timeout: float,
    known_apis_dir: Optional[str],
    *,
    probe_timeout: float = 1.0,
    cache_dir: Optional[str] = None,
) -> list[ApiExistenceChecker]:
    """Assemble the checker stack: snapshots always, ollama when reachable.

    Each checker is wrapped in :class:`CachingChecker`; when ``cache_dir`` is
    given, LLM answers are persisted across runs (snapshots stay in-memory).
    """
    checkers: list[ApiExistenceChecker] = []
    if known_apis_dir:
        snap = OfflineSnapshotClient(known_apis_dir)
        cache_file = Path(cache_dir) / "snapshot-cache.json" if cache_dir else None
        checkers.append(CachingChecker(snap, cache_file))
    if probe_ollama(base_url, probe_timeout):
        oll = OllamaClient(base_url, model, timeout)
        cache_file = Path(cache_dir) / "ollama-cache.json" if cache_dir else None
        checkers.append(CachingChecker(oll, cache_file))
    return checkers
