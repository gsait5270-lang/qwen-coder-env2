"""Configuration loader for ``.slop-guard.yaml``.

Everything is plain dataclasses with sensible defaults so that detectors
work out of the box without a config file.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class AstConfig:
    enabled: bool = True
    stub_threshold: int = 3            # max body lines considered a "stub"
    dead_imports: bool = True
    noise_comments: bool = True
    duplicate_blocks: bool = True
    duplicate_min_lines: int = 5
    duplicate_similarity: float = 0.9


@dataclass
class SemanticConfig:
    enabled: bool = False              # requires ollama; opt-in
    model: str = "qwen2.5:7b"
    base_url: str = "http://localhost:11434"
    confidence_threshold: float = 0.7
    timeout: float = 30.0
    known_apis_dir: Optional[str] = None  # offline API-signature snapshots


@dataclass
class ProvenanceConfig:
    enabled: bool = True
    check_doi: bool = True
    check_arxiv: bool = True
    timeout: float = 2.0               # per-request timeout (spec: 2 seconds)
    offline: bool = False              # skip network calls (tests / air-gapped CI)


@dataclass
class Config:
    ast: AstConfig = field(default_factory=AstConfig)
    semantic: SemanticConfig = field(default_factory=SemanticConfig)
    provenance: ProvenanceConfig = field(default_factory=ProvenanceConfig)
    languages: list[str] = field(default_factory=lambda: ["python"])
    exclude: list[str] = field(
        default_factory=lambda: ["tests/**", "**/*.min.js", "**/node_modules/**"]
    )

    def is_excluded(self, path: str | Path) -> bool:
        p = str(path).replace("\\", "/")
        return any(fnmatch.fnmatch(p, pat) for pat in self.exclude)


_DEFAULT_SEARCH_NAMES = (".slop-guard.yaml", ".slop-guard.yml")


def _sub(cls, data: Any):
    """Build a dataclass from a dict, ignoring unknown keys."""
    if not isinstance(data, dict):
        return cls()
    valid = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in data.items() if k in valid})


def load_config(path: Optional[str | Path] = None, root: Optional[Path] = None) -> Config:
    """Load configuration explicitly or by searching the project root."""
    cfg_path: Optional[Path] = None
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

    cfg = Config()
    if cfg_path is None:
        return cfg

    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    detectors = data.get("detectors", {}) or {}
    if "ast" in detectors:
        cfg.ast = _sub(AstConfig, detectors["ast"])
    if "semantic" in detectors:
        cfg.semantic = _sub(SemanticConfig, detectors["semantic"])
    if "provenance" in detectors:
        cfg.provenance = _sub(ProvenanceConfig, detectors["provenance"])
    if isinstance(data.get("languages"), list):
        cfg.languages = [str(l).lower() for l in data["languages"]]
    if isinstance(data.get("exclude"), list):
        cfg.exclude = [str(e) for e in data["exclude"]]
    return cfg
