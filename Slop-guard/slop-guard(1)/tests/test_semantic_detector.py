"""Tests for module 2b: semantic detector (API hallucination checks).

Uses the deterministic OfflineSnapshotClient so CI needs no ollama server.
Contract under test:
* bad fixture   -> SG-SEM-001 on `df.to_parquet_s3` (line 19)
* good fixture  -> zero findings
* unknown packages/members must yield *no* finding (fail-open, never guess)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slop_guard.config import SemanticConfig
from slop_guard.detectors.semantic_detector import SemanticDetector
from slop_guard.llm.ollama_client import ExistenceAnswer, OfflineSnapshotClient
from slop_guard.models import RULE_HALLUCINATED_API, Severity


class FakeChecker:
    """Scriptable checker to unit-test consensus logic without snapshots."""

    source = "fake"

    def __init__(self, answers: dict[tuple[str, str], ExistenceAnswer]):
        self.answers = answers

    def check(self, library: str, member: str, version: str = "") -> ExistenceAnswer:
        return self.answers.get((library, member), ExistenceAnswer(None, 0.0))


@pytest.fixture()
def snapshot_checker(snapshot_dir: Path) -> OfflineSnapshotClient:
    return OfflineSnapshotClient(snapshot_dir)


@pytest.fixture()
def detector(snapshot_checker) -> SemanticDetector:
    cfg = SemanticConfig(enabled=True, confidence_threshold=0.7)
    return SemanticDetector(cfg, [snapshot_checker])


# ---------------------------------------------------------------------------
# end-to-end on fixtures
# ---------------------------------------------------------------------------

class TestFixtures:
    def test_hallucinated_method_detected(self, detector, bad_fixture_path):
        src = bad_fixture_path.read_text()
        findings = [f for f in detector.detect(bad_fixture_path, "python", src)
                    if f.rule_id == RULE_HALLUCINATED_API]
        assert len(findings) == 1
        f = findings[0]
        assert f.line == 19
        assert "to_parquet_s3" in f.message
        assert f.severity is Severity.ERROR
        assert f.confidence >= 0.7
        # suggestion should point at the real method
        assert "to_parquet" in (f.suggested_fix or "")

    def test_clean_code_has_no_findings(self, detector, good_fixture_path):
        src = good_fixture_path.read_text()
        assert detector.detect(good_fixture_path, "python", src) == []

    def test_valid_pandas_calls_not_flagged(self, detector, tmp_path: Path):
        p = tmp_path / "ok.py"
        p.write_text(
            "import pandas as pd\n"
            "df = pd.read_csv('a.csv')\n"
            "out = df.to_parquet('a.parquet')\n"
            "head = df.head()\n"
            "pd.read_parquet('a.parquet')\n"
        )
        assert detector.detect(p, "python", p.read_text()) == []

    def test_non_python_language_skipped(self, detector, tmp_path: Path):
        p = tmp_path / "m.js"
        p.write_text("import fs from 'fs'; fs.toParquetS3();")
        assert detector.detect(p, "javascript", p.read_text()) == []

    def test_unknown_package_never_flagged(self, detector, tmp_path: Path):
        p = tmp_path / "u.py"
        p.write_text("import mysterylib as m\nm.some_weird_call()\n")
        assert detector.detect(p, "python", p.read_text()) == []


# ---------------------------------------------------------------------------
# snapshot client unit tests
# ---------------------------------------------------------------------------

class TestOfflineSnapshotClient:
    def test_existing_member(self, snapshot_checker):
        ans = snapshot_checker.check("pandas.DataFrame", "to_parquet")
        assert ans.exists is True and ans.confidence == 1.0

    def test_missing_member_with_suggestion(self, snapshot_checker):
        ans = snapshot_checker.check("pandas.DataFrame", "to_parquet_s3")
        assert ans.exists is False
        assert ans.suggestion == "to_parquet"

    def test_unknown_package_is_none(self, snapshot_checker):
        ans = snapshot_checker.check("nosuchpkg", "whatever")
        assert ans.exists is None

    def test_torch_functional(self, snapshot_checker):
        assert snapshot_checker.check("torch.nn.functional", "relu").exists is True
        assert snapshot_checker.check("torch.nn.functional", "gelu_approx").exists is False


# ---------------------------------------------------------------------------
# consensus logic
# ---------------------------------------------------------------------------

class TestConsensus:
    def _det(self, checkers):
        return SemanticDetector(SemanticConfig(enabled=True), checkers)

    def test_single_negative_flags(self, tmp_path: Path):
        ch = FakeChecker({("pandas", "bogus"): ExistenceAnswer(False, 0.9, "real")})
        det = self._det([ch])
        p = tmp_path / "c.py"
        p.write_text("import pandas\npandas.bogus()\n")
        found = det.detect(p, "python", p.read_text())
        assert len(found) == 1

    def test_disagreement_damps_confidence(self, tmp_path: Path):
        neg = FakeChecker({("pandas", "maybe"): ExistenceAnswer(False, 0.9)})
        pos = FakeChecker({("pandas", "maybe"): ExistenceAnswer(True, 0.8)})
        det = self._det([neg, pos])
        p = tmp_path / "c.py"
        p.write_text("import pandas\npandas.maybe()\n")
        found = det.detect(p, "python", p.read_text())
        # disagreement: 0.9 * 0.6 = 0.54 < default threshold behaviour —
        # with threshold 0.0 we would see it damped; here threshold default 0.7
        assert found == []

    def test_no_checkers_returns_empty(self, tmp_path: Path):
        det = SemanticDetector(SemanticConfig(enabled=True), [])
        p = tmp_path / "c.py"
        p.write_text("import pandas\npandas.bogus()\n")
        assert det.detect(p, "python", p.read_text()) == []
