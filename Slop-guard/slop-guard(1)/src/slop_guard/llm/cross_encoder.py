"""Optional cross-encoder scorer for comment/code similarity (module 2b).

The token-subset heuristic in the AST detector is fast and dependency-free,
but it misses paraphrases ("bump the counter" vs ``counter += 1``). A
cross-encoder (``cross-encoder/ms-marco-MiniLM-L-6-v2`` via
``sentence-transformers``) scores a (comment, code-line) pair directly;
scores above ~0.9 mean the comment merely restates the code.

The dependency is **heavy** (pulls torch) and therefore optional:

* install with ``pip install "slop-guard[semantic]"``;
* enable with ``detectors.ast.cross_encoder: true`` in ``.slop-guard.yaml``;
* when the flag is off — or the package is missing — the detector silently
  falls back to the token heuristic (a warning is logged once).

Import of ``sentence_transformers`` is deferred to first use so the base
package never pays the import cost.
"""

from __future__ import annotations

import logging

log = logging.getLogger("slop_guard.llm.cross_encoder")

DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

_warned_missing = False


class CrossEncoderCommentScorer:
    """Scores (comment, code) pairs; higher = more redundant."""

    def __init__(self, model_name: str = DEFAULT_MODEL):
        from sentence_transformers import CrossEncoder  # lazy: heavy import

        self._model = CrossEncoder(model_name)
        self.model_name = model_name

    def score(self, comment: str, code_line: str) -> float:
        value = self._model.predict([(comment, code_line)])[0]
        return float(value)


def get_scorer(enabled: bool, model_name: str = DEFAULT_MODEL
               ) -> CrossEncoderCommentScorer | None:
    """Return a scorer when enabled *and* importable, else None (fallback)."""
    global _warned_missing
    if not enabled:
        return None
    try:
        return CrossEncoderCommentScorer(model_name)
    except ImportError:
        if not _warned_missing:
            log.warning(
                "ast.cross_encoder is enabled but sentence-transformers is not "
                'installed — falling back to the token heuristic. Install with: '
                'pip install "slop-guard[semantic]"')
            _warned_missing = True
        return None
    except Exception as exc:  # model download failed, corrupt cache, ...
        log.warning("cross-encoder unavailable (%s) — using token heuristic", exc)
        return None
