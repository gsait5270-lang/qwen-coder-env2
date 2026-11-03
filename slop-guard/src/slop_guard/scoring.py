"""Module 3: Scoring & Report Engine (severity, confidence, SARIF output).

Consensus scoring: when multiple detectors report on the same location, the
combined confidence grows. SARIF 2.1.0 output is emitted so results can be
uploaded to GitHub Code Scanning via github/codeql-action/upload-sarif.

Schema-compliance notes (validated against the official OASIS schema in
``tests/schemas/sarif-schema-2.1.0.json``):

* ``fixes[]`` requires ``artifactChanges`` and its message must live under a
  top-level ``fix.description`` — since slop-guard does not emit machine
  applicable fixes, we deliberately **do not** populate ``fixes``; the hint is
  carried in ``message.text`` suffix + ``properties.suggestedFix``.
* Fingerprints go into ``partialFingerprints`` (an object of
  tool-specific stable ids), *not* into ``properties``, so GitHub dedupes
  alerts across commits. The fingerprint excludes line numbers.
* Every reporting rule carries ``defaultConfiguration.level`` so Code
  Scanning shows the right severity even if a result omits ``level``.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Optional

from . import __version__
from .models import (
    Finding,
    REPO_URL,
    RULE_DESCRIPTIONS,
    Severity,
    rule_help_uri,
)

log = logging.getLogger("slop_guard.scoring")

SARIF_LEVEL = {Severity.ERROR: "error", Severity.WARNING: "warning", Severity.INFO: "note"}


def consensus_score(findings: list[Finding]) -> list[Finding]:
    """Boost confidence for locations flagged by more than one detector.

    ``Finding`` is frozen: boosted copies are returned, callers' objects are
    never mutated (a previous revision mutated inputs in place, corrupting
    re-scored lists).
    """
    by_loc: dict[tuple[str, int], list[Finding]] = defaultdict(list)
    for f in findings:
        by_loc[(f.file, f.line)].append(f)
    scored: list[Finding] = []
    for _loc, group in by_loc.items():
        distinct_detectors = {f.detector for f in group if f.detector}
        boost = 0.1 * (len(distinct_detectors) - 1) if len(distinct_detectors) > 1 else 0.0
        for f in group:
            scored.append(replace(f, confidence=min(0.99, f.confidence + boost))
                          if boost else f)
    return sorted(scored, key=lambda f: (-f.confidence, f.file, f.line))


def filter_by_threshold(findings: Iterable[Finding], threshold: float) -> list[Finding]:
    return [f for f in findings if f.confidence >= threshold]


# ---------------------------------------------------------------------------
# SARIF 2.1.0
# ---------------------------------------------------------------------------

def _artifact_uri(f: Finding, root: Optional[Path]) -> str:
    p = Path(f.file)
    if root is not None:
        try:
            uri = p.resolve().relative_to(Path(root).resolve()).as_posix()
        except (ValueError, OSError):
            uri = p.as_posix()
    else:
        uri = p.as_posix()
    return uri.replace("\\", "/")


def build_sarif(findings: list[Finding], root: Optional[Path] = None) -> dict:
    rules: dict[str, dict] = {}
    results: list[dict] = []
    for f in findings:
        if f.rule_id not in rules:
            desc = RULE_DESCRIPTIONS.get(f.rule_id, f.rule_id)
            rules[f.rule_id] = {
                "id": f.rule_id,
                "name": f.rule_id.removeprefix("SG-").replace("-", "_").upper(),
                "shortDescription": {"text": desc},
                "fullDescription": {"text": desc},
                "helpUri": rule_help_uri(f.rule_id),
                "help": {"text": desc, "markdown": f"### {f.rule_id}\n\n{desc}"},
                "defaultConfiguration": {"level": SARIF_LEVEL[f.severity]},
                "properties": {"tags": ["slop-guard", f.detector or "unknown"]},
            }
        region: dict = {"startLine": f.line, "startColumn": f.column}
        if f.end_line:
            region["endLine"] = f.end_line

        # suggested fix travels as text + property (no fake artifactChanges)
        message_text = f.message
        if f.suggested_fix:
            first_line = f.suggested_fix.splitlines()[0]
            message_text += f"\n[suggested fix] {first_line}"

        result: dict = {
            "ruleId": f.rule_id,
            "ruleIndex": None,  # patched after sorting rules below
            "level": SARIF_LEVEL[f.severity],
            "message": {"text": message_text},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": _artifact_uri(f, root)},
                        "region": region,
                    }
                }
            ],
            "partialFingerprints": {"slopGuard/stable/v2": f.fingerprint},
            "properties": {"confidence": round(f.confidence, 3)},
        }
        if f.suggested_fix:
            result["properties"]["suggestedFix"] = f.suggested_fix
        results.append(result)

    sorted_rules = sorted(rules.values(), key=lambda r: r["id"])
    order = {r["id"]: i for i, r in enumerate(sorted_rules)}
    for res in results:
        res["ruleIndex"] = order[res["ruleId"]]

    sarif = {
        "$schema": ("https://raw.githubusercontent.com/oasis-tcs/sarif-spec/"
                    "main/Schemata/sarif-schema-2.1.0.json"),
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "slop-guard",
                        "semanticVersion": __version__,
                        "informationUri": REPO_URL,
                        "rules": sorted_rules,
                    }
                },
                "results": results,
            }
        ],
    }
    return sarif


def write_sarif(findings: list[Finding], output: Path, root: Optional[Path] = None) -> None:
    output.write_text(json.dumps(build_sarif(findings, root), indent=2), encoding="utf-8")


def format_text(findings: list[Finding], root: Optional[Path] = None) -> str:
    lines = []
    for f in findings:
        loc = _artifact_uri(f, root)
        fix = f"\n    fix: {f.suggested_fix.splitlines()[0]}" if f.suggested_fix else ""
        lines.append(
            f"{loc}:{f.line}:{f.column}: {f.severity.value} [{f.rule_id}] "
            f"{f.message} (confidence={f.confidence:.2f}){fix}"
        )
    lines.append(f"\n{len(findings)} finding(s).")
    return "\n".join(lines)


def summary_lines(n_files: int, findings: list[Finding], scanned_names: list[str]) -> str:
    """Human one-line summary printed to stderr (never pollutes stdout JSON)."""
    counts: dict[str, int] = defaultdict(int)
    for f in findings:
        counts[f.severity.value] += 1
    parts = [f"{counts.get('error', 0)} error(s)",
             f"{counts.get('warning', 0)} warning(s)",
             f"{counts.get('info', 0)} info(s)"]
    return (f"slop-guard: scanned {n_files} file(s); "
            f"{len(findings)} finding(s) ({', '.join(parts)})")
