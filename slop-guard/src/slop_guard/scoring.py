"""Module 3: Scoring & Report Engine (severity, confidence, SARIF output).

Consensus scoring: when multiple detectors report on the same location, the
combined confidence grows. SARIF 2.1.0 output is emitted so results can be
uploaded to GitHub Code Scanning via github/codeql-action/upload-sarif.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Optional

from . import __version__
from .models import (
    Finding,
    RULE_DESCRIPTIONS,
    RULE_HELP_URI,
    Severity,
)

SARIF_LEVEL = {Severity.ERROR: "error", Severity.WARNING: "warning", Severity.INFO: "note"}


def consensus_score(findings: list[Finding]) -> list[Finding]:
    """Boost confidence for locations flagged by more than one detector."""
    by_loc: dict[tuple[str, int], list[Finding]] = defaultdict(list)
    for f in findings:
        by_loc[(f.file, f.line)].append(f)
    scored: list[Finding] = []
    for loc, group in by_loc.items():
        distinct_detectors = {f.detector for f in group if f.detector}
        for f in group:
            if len(distinct_detectors) > 1:
                # agreement across independent detectors raises confidence
                f.confidence = min(0.99, f.confidence + 0.1 * (len(distinct_detectors) - 1))
            scored.append(f)
    return sorted(scored, key=lambda f: (-f.confidence, f.file, f.line))


def filter_by_threshold(findings: Iterable[Finding], threshold: float) -> list[Finding]:
    return [f for f in findings if f.confidence >= threshold]


def build_sarif(findings: list[Finding], root: Optional[Path] = None) -> dict:
    rules: dict[str, dict] = {}
    results = []
    for f in findings:
        if f.rule_id not in rules:
            rules[f.rule_id] = {
                "id": f.rule_id,
                "shortDescription": {"text": RULE_DESCRIPTIONS.get(f.rule_id, f.rule_id)},
                "fullDescription": {"text": RULE_DESCRIPTIONS.get(f.rule_id, f.rule_id)},
                "helpUri": RULE_HELP_URI,
                "properties": {"tags": ["slop-guard", f.detector or "unknown"]},
            }
        try:
            fp = Path(f.file).resolve()
            uri = str(fp.relative_to(Path(root).resolve())) if root else str(fp)
        except ValueError:
            uri = str(f.file)
        result = {
            "ruleId": f.rule_id,
            "level": SARIF_LEVEL[f.severity],
            "message": {"text": f.message},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": uri.replace("\\", "/")},
                        "region": {
                            "startLine": f.line,
                            "startColumn": f.column,
                            **( {"endLine": f.end_line} if f.end_line else {}),
                        },
                    }
                }
            ],
            "properties": {"confidence": round(f.confidence, 3), "fingerprint": f.fingerprint},
        }
        if f.suggested_fix:
            result["fixes"] = [{"message": {"text": f.suggested_fix}}]
        results.append(result)

    sarif = {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "slop-guard",
                        "version": __version__,
                        "informationUri": "https://github.com/your-org/slop-guard",
                        "rules": sorted(rules.values(), key=lambda r: r["id"]),
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
        try:
            loc = Path(f.file).resolve().relative_to(Path(root).resolve()) if root else Path(f.file).name
        except ValueError:
            loc = Path(f.file).name
        fix = f"\n    fix: {f.suggested_fix.splitlines()[0]}" if f.suggested_fix else ""
        lines.append(
            f"{loc}:{f.line}:{f.column}: {f.severity.value} [{f.rule_id}] "
            f"{f.message} (confidence={f.confidence:.2f}){fix}"
        )
    lines.append(f"\n{len(findings)} finding(s).")
    return "\n".join(lines)
