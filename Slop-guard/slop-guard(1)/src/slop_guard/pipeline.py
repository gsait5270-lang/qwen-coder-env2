"""Pipeline orchestration: Router -> Detectors -> Scoring -> Report.

The pipeline is pure Python (no I/O beyond reading routed files); CLI and the
GitHub Action are thin wrappers around :func:`ScanPipeline.scan`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, load_config
from .detectors.ast_detector import AstDetector
from .detectors.provenance_detector import ProvenanceDetector
from .detectors.semantic_detector import SemanticDetector
from .llm.ollama_client import ApiExistenceChecker, build_checker
from .models import FailOn, Finding, Severity
from .router import RoutedFile, collect_files, git_changed_files, route_paths
from .scoring import consensus_score, filter_by_threshold, summary_lines

log = logging.getLogger("slop_guard.pipeline")

#: bundled offline API snapshots shipped with the package
DEFAULT_SNAPSHOT_DIR = Path(__file__).resolve().parent / "api_snapshots"


@dataclass
class ScanResult:
    findings: list[Finding]
    scanned_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.severity.value] = out.get(f.severity.value, 0) + 1
        return out

    def count(self, severity: Severity | None = None) -> int:
        return len([f for f in self.findings
                    if severity is None or f.severity == severity])

    def exceeds(self, fail_on: FailOn | str) -> bool:
        """True when any finding is at/above the ``--fail-on`` severity.

        ``FailOn.NEVER`` (or the string "never") always returns False.
        """
        fo = FailOn(fail_on) if isinstance(fail_on, str) else fail_on
        if fo is FailOn.NEVER:
            return False
        threshold = Severity(fo.value)
        return any(f.severity.rank >= threshold.rank for f in self.findings)


class ScanPipeline:
    def __init__(self, config: Config | None = None, root: Path = Path("."),
                 checkers: list[ApiExistenceChecker] | None = None):
        self.config = config or load_config(root=root)
        self.root = Path(root)
        self.ast = AstDetector(self.config.ast)
        self.provenance = ProvenanceDetector(self.config.provenance)
        if checkers is not None:
            self._checkers: list[ApiExistenceChecker] = checkers
        elif self.config.semantic.enabled:
            self._checkers = build_checker(
                self.config.semantic.base_url,
                self.config.semantic.model,
                self.config.semantic.timeout,
                self.config.semantic.known_apis_dir or str(DEFAULT_SNAPSHOT_DIR),
                probe_timeout=self.config.semantic.probe_timeout,
                cache_dir=self.config.semantic.cache_dir,
            )
        else:
            self._checkers = []
        if self.config.semantic.enabled and not self._checkers:
            log.warning("semantic detector enabled but no checker backend is "
                        "available — semantic findings will be skipped")
        self.semantic = SemanticDetector(self.config.semantic, self._checkers)

    # -- routing ---------------------------------------------------------------

    def _route(self, paths: list[Path]) -> list[RoutedFile]:
        excl = lambda p: self.config.is_excluded(p, root=self.root)  # noqa: E731
        return route_paths(paths, self.config.languages, excl, root=self.root)

    def plan(self, target: Path, pr_mode: bool = False,
             base_ref: str = "origin/main") -> list[RoutedFile]:
        """Resolve which files would be scanned (shared by scan & PR mode).

        Raises :class:`RouterError` in PR mode when the diff cannot be
        computed or is empty — callers must surface that as a hard failure
        (exit 2), never silently fall back to a full scan.
        """
        if pr_mode:
            changed = git_changed_files(self.root, base_ref)
            routed = self._route(changed)
            log.info("PR mode: %d changed file(s), %d scannable", len(changed), len(routed))
            return routed
        collected = collect_files(target)
        routed = self._route(collected)
        log.debug("collected %d file(s), %d routed", len(collected), len(routed))
        return routed

    # -- scanning ----------------------------------------------------------------

    def scan(self, target: Path, pr_mode: bool = False,
             base_ref: str = "origin/main") -> ScanResult:
        routed = self.plan(target, pr_mode, base_ref)
        findings: list[Finding] = []
        errors: list[str] = []
        scanned: list[str] = []

        for rf in routed:
            try:
                source = rf.source
            except OSError as exc:
                errors.append(f"{rf.path}: cannot read ({exc})")
                log.warning("cannot read %s (%s)", rf.path, exc)
                continue
            scanned.append(str(rf.path))
            if not rf.provenance_only:
                findings.extend(self.ast.detect(rf.path, rf.language, source))
                findings.extend(self.semantic.detect(rf.path, rf.language, source))
            findings.extend(self.provenance.detect(rf.path, rf.language, source))

        scored = consensus_score(findings)
        # global floor from config (was hard-coded 0.5 in earlier revisions)
        kept = filter_by_threshold(scored, self.config.min_confidence)
        dropped = len(scored) - len(kept)
        if dropped:
            log.info("%d finding(s) below min_confidence=%.2f suppressed",
                     dropped, self.config.min_confidence)
        # debug-level: the CLI prints the summary to stderr itself; library
        # callers can still enable it via logging configuration.
        log.debug("%s", summary_lines(len(scanned), kept, scanned))
        return ScanResult(findings=kept, scanned_files=scanned, errors=errors)
