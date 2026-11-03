"""Pipeline orchestrator: router -> detectors -> scoring -> report."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import Config, load_config
from .detectors.ast_detector import AstDetector
from .detectors.provenance_detector import ProvenanceDetector
from .detectors.semantic_detector import SemanticDetector
from .llm.ollama_client import build_checker
from .models import Finding, Severity
from .router import RoutedFile, collect_files, git_changed_files, route_paths
from .scoring import consensus_score, filter_by_threshold


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    scanned_files: list[str] = field(default_factory=list)

    def count(self, severity: Optional[Severity] = None) -> int:
        return len([f for f in self.findings if severity is None or f.severity == severity])

    def exceeds(self, fail_on: str) -> bool:
        threshold = Severity(fail_on)
        return any(f.severity.rank >= threshold.rank for f in self.findings)


def scan(
    target: Path,
    config: Optional[Config] = None,
    config_path: Optional[str] = None,
    pr_mode: bool = False,
    base_ref: str = "origin/main",
) -> ScanResult:
    cfg = config or load_config(config_path, root=Path(target))
    result = ScanResult()

    if pr_mode:
        paths = git_changed_files(Path(target), base_ref) or collect_files(Path(target))
    else:
        paths = collect_files(Path(target))
    routed: list[RoutedFile] = route_paths(paths, cfg.languages, cfg.is_excluded)
    result.scanned_files = [str(r.path) for r in routed]

    detectors: list = []
    if cfg.ast.enabled:
        detectors.append(AstDetector(cfg.ast))
    semantic: Optional[SemanticDetector] = None
    if cfg.semantic.enabled:
        checkers = build_checker(
            cfg.semantic.base_url, cfg.semantic.model,
            cfg.semantic.timeout,
            cfg.semantic.known_apis_dir or str(Path(__file__).parent / "api_snapshots"),
        )
        semantic = SemanticDetector(cfg.semantic, checkers)
        if checkers:
            detectors.append(semantic)
    if cfg.provenance.enabled:
        detectors.append(ProvenanceDetector(cfg.provenance))

    findings: list[Finding] = []
    for rf in routed:
        source = rf.source
        for det in detectors:
            # provenance also inspects markdown docs; AST/semantic skip unsupported langs internally
            findings.extend(det.detect(rf.path, rf.language, source))

    result.findings = filter_by_threshold(consensus_score(findings), 0.5)
    return result
