"""Module 2c: Provenance detector — phantom source verification.

Extracts DOI / arXiv identifiers from comments and documentation and verifies
them against public registries:

* Crossref:  https://api.crossref.org/works/{doi}
* arXiv:     https://export.arxiv.org/api/query?id_list=a,b,c   (batched)

Three-state resolution (P1-7): a registry **404** means "phantom"; a network
failure (timeout/DNS/429/5xx) means **"unverifiable"** and is *never* reported
as a finding — CI machines without internet must not flag real DOIs.

Network verification is **opt-in** (``verify_online: true`` or
``--verify-sources``); by default the detector only extracts identifiers.

Performance & politeness:
* per-scan memoization + optional TTL disk cache between runs;
* retries with exponential backoff for transient errors;
* arXiv ids are batched into one request via ``id_list=a,b,c``;
* ``mailto`` parameter enables Crossref's polite pool;
* resolutions run concurrently (bounded thread pool).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote_plus

import httpx

from ..config import ProvenanceConfig
from ..models import RULE_PHANTOM_SOURCE, Finding, Severity

log = logging.getLogger("slop_guard.provenance")

DOI_RE = re.compile(
    r"\b(?:doi[:\s]*(?:https?://dx\.doi\.org/)?|https?://dx\.doi\.org/)"
    r"(10\.\d{4,9}/[^\s\"'<>)\]}]+?)(?=[.,;]?(?:\s|$|[\"'<>)\]}]))",
    re.I,
)
ARXIV_RE = re.compile(
    r"\b(?:arxiv[:\s]*(?:https?://arxiv\.org/(?:abs|pdf)/)?"
    r"|https?://arxiv\.org/(?:abs|pdf)/)"
    r"((?:\d{4}\.\d{4,5}(?:v\d+)?|[a-z\-]+(?:\.[A-Z]{2,7})?/\d{7}(?:v\d+)?))",
    re.I,
)
CROSSREF_URL = "https://api.crossref.org/works/{doi}"
ARXIV_URL = "https://export.arxiv.org/api/query"


class ResolveStatus(StrEnum):
    RESOLVED = "resolved"          # registry knows it -> genuine source
    PHANTOM = "phantom"            # registry answered definitively "not found"
    UNVERIFIABLE = "unverifiable"  # network/config problem -> report nothing


@dataclass
class _Ref:
    kind: str        # 'DOI' | 'arXiv'
    ident: str
    path: Path
    line: int


# ---------------------------------------------------------------------------
# TTL disk cache
# ---------------------------------------------------------------------------

class ResolutionCache:
    """Tiny JSON {key: [timestamp, status]} store shared across runs."""

    def __init__(self, cache_dir: Path | None, ttl: float):
        self.file = Path(cache_dir) / "provenance-cache.json" if cache_dir else None
        self.ttl = ttl
        self._data: dict[str, list] = {}
        if self.file and self.file.exists():
            try:
                self._data = json.loads(self.file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("ignoring corrupt provenance cache: %s", exc)

    def get(self, key: str) -> ResolveStatus | None:
        entry = self._data.get(key)
        if not entry:
            return None
        ts, status = entry[0], entry[1]
        if time.time() - float(ts) > self.ttl:
            del self._data[key]
            return None
        try:
            return ResolveStatus(status)
        except ValueError:
            return None

    def put(self, key: str, status: ResolveStatus) -> None:
        self._data[key] = [time.time(), status.value]

    def flush(self) -> None:
        if not self.file:
            return
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            self.file.write_text(json.dumps(self._data), encoding="utf-8")
        except OSError as exc:
            log.warning("cannot persist provenance cache: %s", exc)


class ProvenanceDetector:
    name = "provenance"

    def __init__(self, config: ProvenanceConfig | None = None):
        self.config = config or ProvenanceConfig()
        self._memo: dict[str, ResolveStatus] = {}      # per-scan memory cache
        self._disk = ResolutionCache(
            Path(self.config.cache_dir) if self.config.cache_dir else None,
            self.config.cache_ttl,
        )
        #: injected in tests; None -> real HTTP implementations
        self.resolve_fn: Callable[[str, str], ResolveStatus] | None = None
        self._doi_override: Callable[[str], bool] | None = None
        self._arxiv_override: Callable[[str], bool] | None = None

    # -- online verification gate ------------------------------------------------

    @property
    def _network_enabled(self) -> bool:
        # `offline` kept as legacy alias: offline=False + verify_online unset
        # is translated by load_config(); here we honour both flags.
        return self.config.verify_online and not self.config.offline

    # -- public API -----------------------------------------------------------

    def detect(self, path: Path, language: str, source: str) -> list[Finding]:
        refs: list[_Ref] = []
        for line_no, line in enumerate(source.splitlines(), start=1):
            if self.config.check_doi:
                for m in DOI_RE.finditer(line):
                    refs.append(_Ref("DOI", m.group(1).rstrip(".,;"), path, line_no))
            if self.config.check_arxiv:
                for m in ARXIV_RE.finditer(line):
                    refs.append(_Ref("arXiv", m.group(1), path, line_no))

        if not refs:
            return []
        if not self._network_enabled:
            log.debug("provenance: network verification disabled; "
                      "%d identifier(s) extracted but not checked", len(refs))
            return []

        statuses = self._resolve_all(refs)
        findings: list[Finding] = []
        unverifiable = 0
        for ref, status in zip(refs, statuses, strict=True):
            if status is ResolveStatus.PHANTOM:
                findings.append(self._phantom_finding(ref))
            elif status is ResolveStatus.UNVERIFIABLE:
                unverifiable += 1
        if unverifiable:
            log.info("provenance: %d identifier(s) could not be verified "
                     "(registry unreachable) — skipped, not flagged", unverifiable)
        self._disk.flush()
        return findings

    # -- resolution ------------------------------------------------------------

    def _resolve_all(self, refs: list[_Ref]) -> list[ResolveStatus]:
        """Resolve with memo + disk cache + batching + bounded parallelism.

        Identical identifiers within one scan are deduplicated before any
        network work (the same DOI cited twice = one registry lookup).
        """
        results: dict[int, ResolveStatus] = {}
        todo: list[tuple[int, _Ref]] = []
        for i, ref in enumerate(refs):
            key = f"{ref.kind}:{ref.ident}"
            cached = self._memo.get(key) or self._disk.get(key)
            if cached is not None:
                results[i] = cached
            else:
                todo.append((i, ref))

        if todo:
            # dedupe by identity key, preserving first-seen order
            first_for_key: dict[str, tuple[int, _Ref]] = {}
            indices_for_key: dict[str, list[int]] = {}
            for i, ref in todo:
                key = f"{ref.kind}:{ref.ident}"
                first_for_key.setdefault(key, (i, ref))
                indices_for_key.setdefault(key, []).append(i)
            uniq = list(first_for_key.values())

            # Batch all arXiv ids into ONE request first — unless resolvers
            # are injected (tests), where per-id resolution keeps determinism.
            arxiv_items = [(i, r) for i, r in uniq if r.kind == "arXiv"]
            other_items = [(i, r) for i, r in uniq if r.kind != "arXiv"]
            if self._arxiv_override is not None or self.resolve_fn is not None:
                other_items = other_items + arxiv_items
                arxiv_items = []
            key_status: dict[str, ResolveStatus] = {}
            if arxiv_items:
                batch = self._resolve_arxiv_batch([r.ident for _, r in arxiv_items])
                for _i, r in arxiv_items:
                    st = batch.get(r.ident, ResolveStatus.UNVERIFIABLE)
                    key_status[f"{r.kind}:{r.ident}"] = st
                    self._remember(r, st)
            if other_items:
                workers = min(8, max(1, len(other_items)))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    sts = list(pool.map(
                        lambda ir: self._resolve_one(ir[1]), other_items))
                for (_i, r), st in zip(other_items, sts, strict=True):
                    key_status[f"{r.kind}:{r.ident}"] = st
                    self._remember(r, st)
            for key, idxs in indices_for_key.items():
                st = key_status.get(key, ResolveStatus.UNVERIFIABLE)
                for i in idxs:
                    results[i] = st
        return [results[i] for i in range(len(refs))]

    def _remember(self, ref: _Ref, status: ResolveStatus) -> None:
        if self._doi_override is not None or self._arxiv_override is not None \
                or self.resolve_fn is not None:
            return  # injected resolvers: keep every call observable & fresh
        key = f"{ref.kind}:{ref.ident}"
        # Only definitive answers are cached; never cache UNVERIFIABLE.
        if status is not ResolveStatus.UNVERIFIABLE:
            self._memo[key] = status
            self._disk.put(key, status)

    def _resolve_one(self, ref: _Ref) -> ResolveStatus:
        override = self._doi_override if ref.kind == "DOI" else self._arxiv_override
        if override is not None:
            try:
                return (ResolveStatus.RESOLVED if override(ref.ident)
                        else ResolveStatus.PHANTOM)
            except Exception as exc:
                log.warning("injected resolver raised for %s: %s", ref.ident, exc)
                return ResolveStatus.UNVERIFIABLE
        if self.resolve_fn is not None:
            try:
                return self.resolve_fn(ref.kind, ref.ident)
            except Exception as exc:
                log.warning("injected resolver raised for %s: %s", ref.ident, exc)
                return ResolveStatus.UNVERIFIABLE
        if ref.kind == "DOI":
            return self._http_resolve_doi(ref.ident)
        return self._http_resolve_arxiv(ref.ident)

    # -- legacy per-kind resolvers (kept for monkeypatching in tests) --------

    @property
    def resolve_doi(self):  # type: ignore[no-redef]
        """Callable wrapper so `det.resolve_doi = lambda doi: ...` keeps
        working in older test suites; returns bool like the old API."""
        if self._doi_override is not None:
            return self._doi_override

        def _r(doi: str) -> bool:
            return self._resolve_one(_Ref("DOI", doi, Path("."), 1)) is ResolveStatus.RESOLVED
        return _r

    @resolve_doi.setter
    def resolve_doi(self, fn) -> None:
        self._doi_override = fn

    @property
    def resolve_arxiv(self):  # type: ignore[no-redef]
        if self._arxiv_override is not None:
            return self._arxiv_override

        def _r(aid: str) -> bool:
            return self._resolve_one(_Ref("arXiv", aid, Path("."), 1)) is ResolveStatus.RESOLVED
        return _r

    @resolve_arxiv.setter
    def resolve_arxiv(self, fn) -> None:
        self._arxiv_override = fn

    # -- HTTP implementations ----------------------------------------------------

    def _client(self) -> httpx.Client:
        from .. import __version__

        headers = {
            "User-Agent": (
                f"slop-guard/{__version__} "
                "(https://github.com/slop-guard/slop-guard)"
            )
        }
        return httpx.Client(timeout=self.config.timeout,
                            follow_redirects=True, headers=headers)

    def _with_retries(self, fn: Callable[[], ResolveStatus]) -> ResolveStatus:
        """Retry transient failures (network / 5xx / 429) with backoff.

        A definitive 404 short-circuits immediately — retrying it is rude.
        If every attempt fails transiently -> UNVERIFIABLE (NOT phantom).
        """
        last: ResolveStatus = ResolveStatus.UNVERIFIABLE
        for attempt in range(self.config.retries + 1):
            try:
                last = fn()
            except (httpx.HTTPError, OSError) as exc:
                log.debug("registry error (attempt %d): %s", attempt + 1, exc)
                last = ResolveStatus.UNVERIFIABLE
            if last in (ResolveStatus.RESOLVED, ResolveStatus.PHANTOM):
                return last
            if attempt < self.config.retries:
                time.sleep(self.config.backoff * (2 ** attempt))
        return last

    def _http_resolve_doi(self, doi: str) -> ResolveStatus:
        """RESOLVED on 200, PHANTOM on 404, UNVERIFIABLE otherwise."""
        url = CROSSREF_URL.format(doi=quote_plus(doi))
        params = {"mailto": self.config.mailto} if self.config.mailto else None

        def once() -> ResolveStatus:
            with self._client() as c:
                r = c.get(url, params=params)
            if r.status_code == 200:
                return ResolveStatus.RESOLVED
            if r.status_code == 404:
                return ResolveStatus.PHANTOM
            log.debug("crossref unexpected status %s for %s", r.status_code, doi)
            return ResolveStatus.UNVERIFIABLE

        return self._with_retries(once)

    def _http_resolve_arxiv(self, arxiv_id: str) -> ResolveStatus:
        return self._resolve_arxiv_batch([arxiv_id]).get(
            arxiv_id, ResolveStatus.UNVERIFIABLE)

    def _resolve_arxiv_batch(self, ids: list[str]) -> dict[str, ResolveStatus]:
        """One query for N ids: id_list=a,b,c. Entries missing from the feed
        response are phantoms; transport failure marks the whole batch
        UNVERIFIABLE."""
        out: dict[str, ResolveStatus] = {}
        for attempt in range(self.config.retries + 1):
            try:
                with self._client() as c:
                    r = c.get(ARXIV_URL, params={"id_list": ",".join(ids)})
                if r.status_code == 429 or r.status_code >= 500:
                    log.debug("arxiv transient status %s", r.status_code)
                    raise httpx.HTTPStatusError("transient", request=r.request,
                                               response=r)
                r.raise_for_status()
                body = r.text
                for ident in ids:
                    base = re.sub(r"v\d+$", "", ident)
                    out[ident] = (ResolveStatus.RESOLVED
                                  if f"arxiv.org/abs/{base}" in body
                                  or f"api.arxiv.org/abs/{base}" in body
                                  or f"<id>http://arxiv.org/abs/{base}" in body
                                  else ResolveStatus.PHANTOM)
                return out
            except (httpx.HTTPError, OSError) as exc:
                log.debug("arxiv batch error (attempt %d): %s", attempt + 1, exc)
                if attempt < self.config.retries:
                    time.sleep(self.config.backoff * (2 ** attempt))
        # exhausted retries -> everything unverifiable
        return {ident: ResolveStatus.UNVERIFIABLE for ident in ids}

    # -- reporting -----------------------------------------------------------------

    def _phantom_finding(self, ref: _Ref) -> Finding:
        return Finding(
            rule_id=RULE_PHANTOM_SOURCE,
            message=(f"{ref.kind} identifier `{ref.ident}` was not found in the "
                     f"{ref.kind.lower()} registry (phantom source)."),
            file=str(ref.path),
            line=ref.line,
            column=1,
            severity=Severity.WARNING,
            confidence=0.85,
            suggested_fix=(
                f"Verify the {ref.kind.lower()} `{ref.ident}` manually; remove it "
                "if it does not exist (AI-generated text often contains fabricated "
                "references)."
            ),
            detector=self.name,
        )
