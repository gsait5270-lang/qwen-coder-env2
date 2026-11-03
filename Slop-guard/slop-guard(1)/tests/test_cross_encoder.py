"""Tests for the optional cross-encoder comment scorer (module 2b).

The heavy dependency (sentence-transformers) is NOT required: we monkeypatch
the scorer factory so the wiring, fallback and configuration behaviour are
tested deterministically.
"""

from __future__ import annotations

from pathlib import Path

import slop_guard.llm.cross_encoder as ce
from slop_guard.config import AstConfig
from slop_guard.detectors.ast_detector import AstDetector
from slop_guard.models import RULE_NOISE_COMMENT


class FakeScorer:
    """Deterministic stand-in: 0.95 when comment and code share a meaningful
    token (the paraphrase case the token heuristic misses), else 0.1."""

    def score(self, comment: str, code_line: str) -> float:
        c = set(comment.lower().split())
        k = set(code_line.lower().split())
        return 0.95 if {t for t in c & k if len(t) > 2} else 0.1


class TestGetScorer:
    def test_disabled_returns_none(self):
        assert ce.get_scorer(False) is None

    def test_missing_dependency_falls_back(self, monkeypatch):
        def boom(*a, **k):
            raise ImportError("no sentence_transformers")
        monkeypatch.setattr(ce, "CrossEncoderCommentScorer", boom)
        monkeypatch.setattr(ce, "_warned_missing", False)
        assert ce.get_scorer(True) is None

    def test_broken_model_falls_back(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("model download failed")
        monkeypatch.setattr(ce, "CrossEncoderCommentScorer", boom)
        assert ce.get_scorer(True) is None


class TestAstIntegration:
    CODE = ("def f():\n"
            "    counter = 0\n"
            "    # bump the counter value\n"      # paraphrase: no token subset
            "    counter += 1\n"
            "    return counter\n")

    def _detect(self, cfg: AstConfig, tmp_path: Path):
        d = AstDetector(cfg)
        p = tmp_path / "m.py"
        p.write_text(self.CODE)
        return [f for f in d.detect(p, "python", self.CODE)
                if f.rule_id == RULE_NOISE_COMMENT]

    def test_token_heuristic_misses_paraphrase(self, tmp_path: Path):
        # "bump" is not a code token -> subset heuristic stays silent.
        assert self._detect(AstConfig(), tmp_path) == []

    def test_cross_encoder_catches_paraphrase(self, tmp_path: Path, monkeypatch):
        # AstDetector imports get_scorer lazily inside the method, so patching
        # the module attribute is enough.
        monkeypatch.setattr(ce, "get_scorer", lambda enabled, model=None: FakeScorer())
        cfg = AstConfig(cross_encoder=True, cross_encoder_threshold=0.5)
        found = self._detect(cfg, tmp_path)
        assert len(found) == 1
        assert found[0].line == 3
        assert found[0].confidence == 0.95

    def test_cross_encoder_below_threshold_silent(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(ce, "get_scorer", lambda enabled, model=None: FakeScorer())
        cfg = AstConfig(cross_encoder=True, cross_encoder_threshold=0.99)
        assert self._detect(cfg, tmp_path) == []
