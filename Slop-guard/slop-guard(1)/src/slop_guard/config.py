"""Configuration loader for ``.slop-guard.yaml``.

Everything is plain dataclasses with sensible defaults so that detectors
work out of the box without a config file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import pathspec
import yaml

log = logging.getLogger("slop_guard.config")


@dataclass
class AstConfig:
    enabled: bool = True
    stub_threshold: int = 3            # max body lines considered a "stub"
    dead_imports: bool = True
    noise_comments: bool = True
    duplicate_blocks: bool = True
    duplicate_min_lines: int = 5
    duplicate_similarity: float = 0.9
    # Optional semantic upgrade for SG-AST-003 (needs the [semantic] extra):
    cross_encoder: bool = False        # cross-encoder instead of token heuristic
    cross_encoder_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    cross_encoder_threshold: float = 0.9


@dataclass
class SemanticConfig:
    enabled: bool = False              # requires ollama; opt-in
    model: str = "qwen2.5:7b"
    base_url: str = "http://localhost:11434"
    confidence_threshold: float = 0.7
    timeout: float = 30.0
    known_apis_dir: str | None = None  # offline API-signature snapshots
    versions: dict[str, str] = field(default_factory=dict)  # pkg -> pinned version
    cache_dir: str | None = None       # JSON cache of LLM answers across runs
    probe_timeout: float = 1.0            # ollama reachability probe budget


@dataclass
class ProvenanceConfig:
    enabled: bool = True
    check_doi: bool = True
    check_arxiv: bool = True
    timeout: float = 2.0               # per-request timeout (spec: 2 seconds)
    # Legacy flag kept for backwards compatibility; implies verify_online=False
    # unless verify_online is explicitly set to true.
    offline: bool = True
    verify_online: bool = False        # network verification is OPT-IN (P1-7)
    retries: int = 2                   # transient failures retried w/ backoff
    backoff: float = 0.3               # exponential backoff base seconds
    cache_dir: str | None = None    # TTL disk cache between runs
    cache_ttl: float = 86400.0         # 24 h default TTL
    mailto: str | None = None       # Crossref polite-pool identification


@dataclass
class Config:
    ast: AstConfig = field(default_factory=AstConfig)
    semantic: SemanticConfig = field(default_factory=SemanticConfig)
    provenance: ProvenanceConfig = field(default_factory=ProvenanceConfig)
    languages: list[str] = field(default_factory=lambda: ["python"])
    exclude: list[str] = field(
        default_factory=lambda: ["tests/**", "**/*.min.js", "**/node_modules/**"]
    )
    min_confidence: float = 0.5        # global reporting floor (fixes hard-coded 0.5)
    respect_gitignore: bool = True     # gitignore-semantics on top of `exclude`

    def __post_init__(self) -> None:
        self._spec: pathspec.PathSpec | None = None
        self._root_specs: dict[Path | None, pathspec.PathSpec] = {}

    @property
    def exclusion_spec(self) -> pathspec.PathSpec:
        """Matcher for the explicit ``exclude`` patterns only."""
        if self._spec is None:
            # gitignore-style semantics: `tests/**`, `*.min.js`, `/build` all work
            self._spec = _make_spec(self.exclude)
        return self._spec

    def exclusion_spec_for(self, root: Path | None) -> pathspec.PathSpec:
        """``exclude`` patterns + the root's ``.gitignore`` (when enabled).

        ``respect_gitignore`` was previously a dead config flag; vendored and
        generated files ignored by git are now skipped by the scanner too.
        """
        key = Path(root).resolve() if root is not None else None
        if key in self._root_specs:
            return self._root_specs[key]
        lines: list[str] = list(self.exclude)
        if self.respect_gitignore and key is not None:
            gi = key / ".gitignore"
            if gi.is_file():
                try:
                    lines += gi.read_text(encoding="utf-8").splitlines()
                except OSError as exc:
                    log.warning("cannot read %s: %s", gi, exc)
        spec = _make_spec(lines)
        self._root_specs[key] = spec
        return spec

    def is_excluded(self, path: str | Path, root: Path | None = None) -> bool:
        """Match *relative* posix paths against gitignore-style patterns.

        Previously this fnmatch'd absolute paths, which made every default
        pattern (`tests/**`) silently never match. Now the scan root is used
        to compute a relative path first.
        """
        p = Path(path)
        rel: str | None = None
        if root is not None:
            try:
                rel = p.resolve().relative_to(Path(root).resolve()).as_posix()
            except (ValueError, OSError):
                rel = None
        if rel is None:
            # fall back: use the tail of the path from a recognizable segment
            parts = p.as_posix().split("/")
            rel = "/".join(parts[-2:]) if len(parts) >= 2 else p.name
        return self.exclusion_spec_for(root).match_file(rel)


_DEFAULT_SEARCH_NAMES = (".slop-guard.yaml", ".slop-guard.yml")


def _make_spec(lines: list[str]) -> pathspec.PathSpec:
    """gitignore-semantics matcher; compatible with pathspec 0.x and 1.x.

    pathspec 1.0 renamed the ``gitwildmatch`` factory to ``gitignore`` and
    emits DeprecationWarnings for the old name; 0.x only knows the old one.
    """
    try:
        return pathspec.PathSpec.from_lines("gitignore", lines)
    except (KeyError, TypeError):        # pragma: no cover - pathspec < 1.0
        return pathspec.PathSpec.from_lines("gitwildmatch", lines)


def find_config(start: Path) -> Path | None:
    """Search *start* and its parents for a config file (repo-root friendly)."""
    start = Path(start).resolve()
    dirs = [start] if start.is_dir() else [start.parent]
    dirs += list(start.parents)
    for d in dirs:
        for name in _DEFAULT_SEARCH_NAMES:
            cand = d / name
            if cand.is_file():
                return cand
    return None


def _sub(cls, data: Any):
    """Build a dataclass from a dict, ignoring unknown keys (warns on them)."""
    if not isinstance(data, dict):
        return cls()
    valid = {f.name for f in fields(cls)}
    unknown = set(data) - valid
    if unknown:
        log.warning("config: ignoring unknown keys for %s: %s",
                    cls.__name__, ", ".join(sorted(unknown)))
    kwargs = {k: v for k, v in data.items() if k in valid}
    try:
        return cls(**kwargs)
    except (TypeError, ValueError) as exc:
        log.warning("config: invalid values for %s (%s); using defaults",
                    cls.__name__, exc)
        return cls()


def load_config(path: str | Path | None = None, root: Path | None = None) -> Config:
    """Load configuration explicitly, by project-root search, or via parent walk.

    Resolution order:
      1. explicit ``path`` (must exist — FileNotFoundError otherwise);
      2. ``root/.slop-guard.yaml``;
      3. nearest config found walking up from ``root`` (or cwd).
    """
    cfg_path: Path | None = None
    if path is not None:
        cfg_path = Path(path)
        if not cfg_path.exists():
            raise FileNotFoundError(f"config file not found: {cfg_path}")
    elif root is not None:
        for name in _DEFAULT_SEARCH_NAMES:
            cand = Path(root) / name
            if cand.exists():
                cfg_path = cand
                break
        if cfg_path is None:
            cfg_path = find_config(Path(root))
    if cfg_path is None:
        return Config()

    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        log.warning("config: cannot parse %s (%s); using defaults", cfg_path, exc)
        return Config()
    if not isinstance(data, dict):
        log.warning("config: %s is not a mapping; using defaults", cfg_path)
        return Config()

    cfg = Config()
    detectors = data.get("detectors", {}) or {}
    if isinstance(detectors, dict):
        if "ast" in detectors:
            cfg.ast = _sub(AstConfig, detectors["ast"])
        if "semantic" in detectors:
            cfg.semantic = _sub(SemanticConfig, detectors["semantic"])
        if "provenance" in detectors:
            cfg.provenance = _sub(ProvenanceConfig, detectors["provenance"])
    if isinstance(data.get("languages"), list):
        cfg.languages = [str(item).lower() for item in data["languages"]]
    if isinstance(data.get("exclude"), list):
        cfg.exclude = [str(e) for e in data["exclude"]]
    if isinstance(data.get("min_confidence"), (int, float)):
        cfg.min_confidence = float(data["min_confidence"])
    if isinstance(data.get("respect_gitignore"), bool):
        cfg.respect_gitignore = bool(data["respect_gitignore"])

    # Backwards-compat shim: an explicit `offline: false` was the old way of
    # enabling network checks; translate it to verify_online=true.
    prov_raw = detectors.get("provenance", {}) if isinstance(detectors, dict) else {}
    if isinstance(prov_raw, dict) and prov_raw.get("verify_online") is None:
        if prov_raw.get("offline") is False:
            cfg.provenance.verify_online = True
    cfg._spec = None          # rebuild matcher after exclude changes
    cfg._root_specs.clear()   # per-root (gitignore-aware) specs too
    return cfg
