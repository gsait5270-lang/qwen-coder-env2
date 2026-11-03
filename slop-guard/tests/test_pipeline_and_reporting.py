"""Integration tests: full pipeline, SARIF output, CLI exit codes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

from slop_guard.cli import app  # noqa: E402
from slop_guard.config import Config  # noqa: E402
from slop_guard.models import (  # noqa: E402
    RULE_DEAD_IMPORT,
    RULE_HALLUCINATED_API,
    RULE_PHANTOM_SOURCE,
    RULE_STUB_FUNCTION,
    Finding,
    Severity,
)
from slop_guard.pipeline import scan  # noqa: E402
from slop_guard.scoring import build_sarif, consensus_score, format_text  # noqa: E402


def _semantic_config_enabled() -> Config:
    cfg = Config()
    cfg.semantic.enabled = True
    cfg.provenance.offline = True  # no network in unit tests
    return cfg


# ---------------------------------------------------------------------------
# pipeline end-to-end on the fixtures directory
# ---------------------------------------------------------------------------

class TestPipeline:
    def test_bad_dir_reports_all_rule_classes(self, tmp_path: Path):
        # copy only the bad fixture into an isolated dir
        shutil_copy = tmp_path / "hallucinated_pipeline.py"
        shutil_copy.write_text(
            (ROOT / "tests/fixtures/bad/hallucinated_pipeline.py").read_text()
        )
        result = scan(tmp_path, config=_semantic_config_enabled())
        rules = {f.rule_id for f in result.findings}
        assert RULE_STUB_FUNCTION in rules
        assert RULE_DEAD_IMPORT in rules
        assert RULE_HALLUCINATED_API in rules
        # provenance is offline here, so phantom ids are NOT reported; that's
        # expected — covered by dedicated mocked tests.

    def test_good_dir_is_clean(self, tmp_path: Path):
        shutil_copy = tmp_path / "clean_module.py"
        shutil_copy.write_text(
            (ROOT / "tests/fixtures/good/clean_module.py").read_text()
        )
        result = scan(tmp_path, config=_semantic_config_enabled())
        assert result.findings == []

    def test_exceeds_fail_on(self, tmp_path: Path):
        shutil_copy = tmp_path / "bad.py"
        shutil_copy.write_text("# TODO: implement later\ndef f():\n    pass\n")
        result = scan(tmp_path, config=_semantic_config_enabled())
        assert result.exceeds("warning")
        assert not result.exceeds("error")

    def test_exclude_patterns_applied(self, tmp_path: Path):
        d = tmp_path / "tests"
        d.mkdir()
        (d / "t.py").write_text("# TODO: implement later\ndef f():\n    pass\n")
        cfg = _semantic_config_enabled()
        result = scan(tmp_path, config=cfg)
        assert result.findings == []  # tests/** excluded by default


# ---------------------------------------------------------------------------
# scoring & SARIF
# ---------------------------------------------------------------------------

def _f(rule="SG-AST-002", line=5, det="ast", conf=0.8, sev=Severity.WARNING) -> Finding:
    return Finding(rule_id=rule, message="m", file="a.py", line=line,
                   severity=sev, confidence=conf, detector=det)


class TestScoring:
    def test_consensus_boosts_multi_detector_locations(self):
        findings = [_f(line=10, det="ast"), _f(line=10, det="semantic", rule="SG-SEM-001")]
        scored = consensus_score(findings)
        assert all(f.confidence > 0.8 for f in scored)

    def test_single_detector_not_boosted(self):
        scored = consensus_score([_f(line=3, det="ast")])
        assert scored[0].confidence == 0.8

    def test_sorted_by_confidence_desc(self):
        scored = consensus_score([_f(conf=0.6), _f(conf=0.9, line=7)])
        assert [f.confidence for f in scored] == sorted(
            [f.confidence for f in scored], reverse=True)


class TestSarif:
    def test_schema_and_structure(self):
        sarif = build_sarif([_f()], root=None)
        assert sarif["version"] == "2.1.0"
        run = sarif["runs"][0]
        assert run["tool"]["driver"]["name"] == "slop-guard"
        assert any(r["id"] == "SG-AST-002" for r in run["tool"]["driver"]["rules"])
        res = run["results"][0]
        assert res["level"] == "warning"
        assert res["locations"][0]["physicalLocation"]["region"]["startLine"] == 5

    def test_levels_mapping(self):
        sarif = build_sarif([_f(sev=Severity.ERROR, rule="SG-SEM-001")])
        assert sarif["runs"][0]["results"][0]["level"] == "error"

    def test_fixes_serialized(self):
        f = _f()
        f.suggested_fix = "Remove the unused import `F`."
        sarif = build_sarif([f])
        assert sarif["runs"][0]["results"][0]["fixes"][0]["message"]["text"].startswith("Remove")

    def test_text_format_contains_location(self):
        out = format_text([_f()])
        assert "a.py:5" in out and "SG-AST-002" in out


# ---------------------------------------------------------------------------
# CLI smoke tests via typer runner (no subprocess overhead)
# ---------------------------------------------------------------------------

class TestCli:
    runner = CliRunner()

    def _mk_target(self, tmp_path: Path) -> Path:
        t = tmp_path / "pkg"
        t.mkdir()
        (t / "mod.py").write_text(
            "# TODO: implement real logic later\ndef process(x):\n    pass\n"
        )
        return t

    def test_scan_text_exit_code_for_warning(self, tmp_path: Path):
        target = self._mk_target(tmp_path)
        res = self.runner.invoke(app, ["scan", str(target), "--fail-on", "warning"])
        assert res.exit_code == 1  # warning found -> fail

    def test_scan_fail_on_error_passes_with_only_warnings(self, tmp_path: Path):
        target = self._mk_target(tmp_path)
        res = self.runner.invoke(app, ["scan", str(target), "--fail-on", "error"])
        assert res.exit_code == 0

    def test_sarif_output_file_written(self, tmp_path: Path):
        target = self._mk_target(tmp_path)
        out = tmp_path / "report.sarif"
        res = self.runner.invoke(
            app, ["scan", str(target), "--format", "sarif", "--output", str(out),
                  "--fail-on", "never"])
        assert res.exit_code == 0
        data = json.loads(out.read_text())
        assert data["version"] == "2.1.0"
        assert data["runs"][0]["results"]

    def test_json_output(self, tmp_path: Path):
        target = self._mk_target(tmp_path)
        res = self.runner.invoke(app, ["scan", str(target), "--format", "json",
                                       "--fail-on", "never"])
        payload = json.loads(res.stdout)
        assert payload and payload[0]["rule"] == "SG-AST-001"

    def test_missing_path_exit_2(self, tmp_path: Path):
        res = self.runner.invoke(app, ["scan", str(tmp_path / "nope")])
        assert res.exit_code == 2

    def test_languages_command_lists_python(self):
        res = self.runner.invoke(app, ["languages"])
        assert "python" in res.stdout


# ---------------------------------------------------------------------------
# console-script entry point actually installed? run as subprocess if present
# ---------------------------------------------------------------------------

def test_module_invocation_via_python_m():
    """`python -m slop_guard.cli --version` must work from src path."""
    env_path = f"{SRC}{Path.pathsep}{env_existing()}"
    res = subprocess.run(
        [sys.executable, "-m", "slop_guard.cli", "--version"],
        capture_output=True, text=True,
        env={**__import__("os").environ, "PYTHONPATH": env_path},
    )
    assert res.returncode == 0
    assert "slop-guard" in res.stdout


def env_existing() -> str:
    import os
    return os.environ.get("PYTHONPATH", "")
