"""Detector package for slop-guard.

Each detector is an independent module implementing::

    detect(path: Path, language: str, source: str) -> list[Finding]

See docs/adding-detectors.md for the contributor guide.
"""

from .ast_detector import AstDetector
from .provenance_detector import ProvenanceDetector
from .semantic_detector import SemanticDetector

__all__ = ["AstDetector", "SemanticDetector", "ProvenanceDetector"]
