"""Tests for module 2a: the tree-sitter AST detector.

Contract under test:
* bad fixture   -> SG-AST-001 (stub), SG-AST-002 (dead import), SG-AST-003 (noise)
* good fixture  -> zero findings (no false positives on clean code)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from slop_guard.config import AstConfig
from slop_guard.detectors.ast_detector import AstDetector
from slop_guard.models import (
    RULE_DEAD_IMPORT,
    RULE_DUPLICATED_BLOCK,
    RULE_NOISE_COMMENT,
    RULE_STUB_FUNCTION,
    Severity,
)


@pytest.fixture()
def detector() -> AstDetector:
    return AstDetector(AstConfig())


def _rule_lines(findings) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for f in findings:
        out.setdefault(f.rule_id, []).append(f.line)
    return {k: sorted(v) for k, v in out.items()}


# ---------------------------------------------------------------------------
# positive detection on the "bad" fixture
# ---------------------------------------------------------------------------

class TestBadFixture:
    def test_all_expected_rules_present(self, detector, bad_fixture_path):
        src = bad_fixture_path.read_text()
        found = _rule_lines(detector.detect(bad_fixture_path, "python", src))
        assert RULE_STUB_FUNCTION in found
        assert RULE_DEAD_IMPORT in found
        assert RULE_NOISE_COMMENT in found

    def test_stub_function_line(self, detector, bad_fixture_path):
        src = bad_fixture_path.read_text()
        found = _rule_lines(detector.detect(bad_fixture_path, "python", src))
        # `def process_data` sits on line 24 of the fixture
        assert found[RULE_STUB_FUNCTION] == [24]

    def test_dead_import_is_the_alias_not_module(self, detector, bad_fixture_path):
        src = bad_fixture_path.read_text()
        findings = [f for f in detector.detect(bad_fixture_path, "python", src)
                    if f.rule_id == RULE_DEAD_IMPORT]
        assert len(findings) == 1
        assert "`F`" in findings[0].message
        assert findings[0].line == 11
        assert findings[0].severity is Severity.WARNING

    def test_noise_comment_line(self, detector, bad_fixture_path):
        src = bad_fixture_path.read_text()
        found = _rule_lines(detector.detect(bad_fixture_path, "python", src))
        assert found[RULE_NOISE_COMMENT] == [30]

    def test_findings_carry_fix_suggestions(self, detector, bad_fixture_path):
        src = bad_fixture_path.read_text()
        for f in detector.detect(bad_fixture_path, "python", src):
            assert f.suggested_fix, f"{f.rule_id} must propose a fix"
            assert f.confidence > 0.5


# ---------------------------------------------------------------------------
# negative tests — clean code must not be flagged
# ---------------------------------------------------------------------------

class TestGoodFixture:
    def test_zero_findings(self, detector, good_fixture_path):
        src = good_fixture_path.read_text()
        findings = detector.detect(good_fixture_path, "python", src)
        assert findings == [], [f.message for f in findings]

    def test_from_import_used_name_not_flagged(self, detector, tmp_path: Path):
        """Regression: `from pathlib import Path` + usage of `Path` must not
        flag `pathlib` or `Path` as dead."""
        p = tmp_path / "m.py"
        p.write_text("from pathlib import Path\n\ndef f():\n    return Path('.')\n")
        assert detector.detect(p, "python", p.read_text()) == []

    def test_plain_dotted_import_usage(self, detector, tmp_path: Path):
        """`import os.path` binds `os`; using `os.path.join` is alive."""
        p = tmp_path / "m.py"
        p.write_text("import os.path\n\ndef f(x):\n    return os.path.join(x, 'a')\n")
        assert detector.detect(p, "python", p.read_text()) == []

    def test_star_import_never_flagged(self, detector, tmp_path: Path):
        p = tmp_path / "m.py"
        p.write_text("from os.path import *\n\nprint(join('a', 'b'))\n")
        dead = [f for f in detector.detect(p, "python", p.read_text())
                if f.rule_id == RULE_DEAD_IMPORT]
        assert dead == []


# ---------------------------------------------------------------------------
# targeted micro-fixtures per rule
# ---------------------------------------------------------------------------

class TestStubRule:
    def test_ellipsis_stub(self, detector, tmp_path: Path):
        p = tmp_path / "s.py"
        p.write_text("# TODO: implement later\ndef noop():\n    ...\n")
        found = detector.detect(p, "python", p.read_text())
        assert [f.rule_id for f in found] == [RULE_STUB_FUNCTION]

    def test_implemented_function_not_flagged(self, detector, tmp_path: Path):
        p = tmp_path / "s.py"
        p.write_text("def add(a, b):\n    # TODO: docstring\n    return a + b\n")
        found = [f for f in detector.detect(p, "python", p.read_text())
                 if f.rule_id == RULE_STUB_FUNCTION]
        assert found == []


class TestDuplicateBlocks:
    def test_identical_blocks_detected(self, detector, tmp_path: Path):
        block = "\n".join(
            [
                "def a(xs):",
                "    total = 0",
                "    for x in xs:",
                "        total += x * 2",
                "    return total",
            ]
        )
        clone = block.replace("def a(", "def b(")
        p = tmp_path / "d.py"
        p.write_text(block + "\n\n" + clone + "\n")
        found = [f for f in detector.detect(p, "python", p.read_text())
                 if f.rule_id == RULE_DUPLICATED_BLOCK]
        # bodies are identical modulo the def line; at least one duplicate window
        assert len(found) >= 1

    def test_short_files_skipped(self, detector, tmp_path: Path):
        p = tmp_path / "tiny.py"
        p.write_text("x = 1\nx = 1\n")
        found = [f for f in detector.detect(p, "python", p.read_text())
                 if f.rule_id == RULE_DUPLICATED_BLOCK]
        assert found == []


class TestRouterBehaviour:
    def test_non_python_skipped_for_dead_imports(self, detector, tmp_path: Path):
        p = tmp_path / "m.js"
        p.write_text("import fs from 'fs';\nconsole.log('hi');\n")
        found = [f for f in detector.detect(p, "javascript", p.read_text())
                 if f.rule_id == RULE_DEAD_IMPORT]
        assert found == []  # JS dead-import logic intentionally not implemented yet

    def test_unparseable_language_returns_empty(self, detector, tmp_path: Path):
        p = tmp_path / "m.xyz"
        p.write_text("totally bogus source")
        assert detector.detect(p, "cobol", p.read_text()) == []


# ---------------------------------------------------------------------------
# property-based tests (hypothesis): detectors must never crash and must
# only emit sane finding coordinates.
# ---------------------------------------------------------------------------

@settings(max_examples=40, deadline=None)
@given(st.text(max_size=600))
def test_ast_detector_never_crashes_on_arbitrary_text(code: str):
    detector = AstDetector(AstConfig())
    import tempfile, pathlib as _p
    with tempfile.TemporaryDirectory() as td:
        f = _p.Path(td) / "gen.py"
        f.write_text(code)
        findings = detector.detect(f, "python", code)
    for x in findings:
        assert x.line >= 1
        assert x.column >= 1
        assert 0.0 <= x.confidence <= 1.0
        assert x.rule_id.startswith("SG-AST")
