"""SARIF 2.1.0 schema validation against the committed official schema.

The schema lives in ``tests/schemas/sarif-schema-2.1.0.json`` (vendored from
https://github.com/oasis-tcs/sarif-spec) so the suite never needs network.

This is the regression guard for the class of bugs where a hand-built SARIF
document drifts from the spec (e.g. ``fixes[].message`` instead of
``description`` + ``artifactChanges``) and GitHub Code Scanning silently
rejects the upload.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from slop_guard.models import Finding, Severity
from slop_guard.scoring import build_sarif

SCHEMA_PATH = Path(__file__).parent / "schemas" / "sarif-schema-2.1.0.json"

jsonschema = pytest.importorskip("jsonschema", reason="dev extra not installed")


@pytest.fixture(scope="module")
def schema() -> dict:
    assert SCHEMA_PATH.is_file(), f"missing vendored schema: {SCHEMA_PATH}"
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _representative_findings() -> list[Finding]:
    base = dict(file="pkg/mod.py", line=3, column=2, end_line=4)
    return [
        Finding(rule_id="SG-AST-001", message="stub `f`", severity=Severity.WARNING,
                confidence=0.9, detector="ast", suggested_fix="Implement `f`.", **base),
        Finding(rule_id="SG-AST-002", message="Import `os` is never used",
                severity=Severity.WARNING, confidence=0.95, detector="ast",
                suggested_fix="Remove the unused import `os`.", **base),
        Finding(rule_id="SG-AST-003", message='Comment restates code: "x"',
                severity=Severity.INFO, confidence=0.75, detector="ast", **base),
        Finding(rule_id="SG-AST-004", message="Duplicated block", severity=Severity.INFO,
                confidence=0.8, detector="ast", **base),
        Finding(rule_id="SG-SEM-001", message="`df.to_parquet_s3` does not exist",
                severity=Severity.ERROR, confidence=0.95, detector="semantic",
                suggested_fix="Did you mean `df.to_parquet`?", **base),
        Finding(rule_id="SG-PRV-001", message="DOI `10.9/x` phantom",
                severity=Severity.WARNING, confidence=0.85, detector="provenance",
                **base),
    ]


def _validate(schema: dict, sarif: dict) -> None:
    errors = sorted(jsonschema.Draft7Validator(schema).iter_errors(sarif),
                    key=lambda e: list(e.path))
    assert not errors, [
        f"{'/'.join(map(str, e.path))}: {e.message[:200]}" for e in errors
    ]


def test_full_report_validates(schema):
    _validate(schema, build_sarif(_representative_findings(), root=None))


def test_empty_report_validates(schema):
    _validate(schema, build_sarif([], root=None))


def test_relative_uris_with_root(schema, tmp_path: Path):
    f = replace(_representative_findings()[0], file=str(tmp_path / "a" / "b.py"))
    sarif = build_sarif([f], root=tmp_path)
    uri = sarif["runs"][0]["results"][0]["locations"][0][
        "physicalLocation"]["artifactLocation"]["uri"]
    assert uri == "a/b.py"
    _validate(schema, sarif)


def test_github_dedup_fields_present(schema):
    sarif = build_sarif(_representative_findings())
    for res in sarif["runs"][0]["results"]:
        assert "partialFingerprints" in res
        assert "fixes" not in res          # we emit no machine-applicable fixes
    for rule in sarif["runs"][0]["tool"]["driver"]["rules"]:
        assert "defaultConfiguration" in rule
        assert rule["helpUri"].startswith("http")


def test_fingerprint_stable_across_line_shifts():
    """Alerts must not be 'recreated' when unrelated edits shift lines."""
    a, b = _representative_findings()[0], _representative_findings()[0]
    shifted = replace(b, line=99, end_line=100)
    assert a.fingerprint == shifted.fingerprint
