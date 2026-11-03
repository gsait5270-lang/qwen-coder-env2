"""Core data models shared by all detectors and the report engine."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    @property
    def rank(self) -> int:
        return {"error": 3, "warning": 2, "info": 1}[self.value]


# Rule identifiers (stable; used in SARIF reporting)
RULE_STUB_FUNCTION = "SG-AST-001"
RULE_DEAD_IMPORT = "SG-AST-002"
RULE_NOISE_COMMENT = "SG-AST-003"
RULE_DUPLICATED_BLOCK = "SG-AST-004"
RULE_HALLUCINATED_API = "SG-SEM-001"
RULE_PHANTOM_SOURCE = "SG-PRV-001"

RULE_DESCRIPTIONS: dict[str, str] = {
    RULE_STUB_FUNCTION: "Template stub function without implementation (TODO + empty body).",
    RULE_DEAD_IMPORT: "Import that is never used inside the same file.",
    RULE_NOISE_COMMENT: "Comment that merely restates the adjacent line of code (AI noise).",
    RULE_DUPLICATED_BLOCK: "Duplicated code block detected via MinHash similarity.",
    RULE_HALLUCINATED_API: "Attribute/method does not exist in the referenced library (possible API hallucination).",
    RULE_PHANTOM_SOURCE: "Referenced DOI/arXiv identifier could not be resolved in Crossref/arXiv (phantom source).",
}

RULE_HELP_URI = "https://github.com/your-org/slop-guard/blob/main/docs/architecture.md"


@dataclass
class Finding:
    """A single issue reported by a detector."""

    rule_id: str
    message: str
    file: str
    line: int                      # 1-based
    column: int = 1                # 1-based
    end_line: Optional[int] = None
    severity: Severity = Severity.WARNING
    confidence: float = 0.5
    suggested_fix: Optional[str] = None
    detector: str = ""

    @property
    def fingerprint(self) -> str:
        raw = f"{self.rule_id}:{self.file}:{self.line}:{self.message}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


_WS_RE = re.compile(r"\s+")


def normalize_code(line: str) -> str:
    """Strip inline comments and collapse whitespace for comparison heuristics."""
    # naive inline comment removal for python-ish / c-ish languages
    for marker in ("#", "//"):
        idx = line.find(marker)
        if idx >= 0:
            line = line[:idx]
    return _WS_RE.sub(" ", line).strip()


def normalize_comment(text: str) -> str:
    """Lowercase, strip punctuation/stopwords-ish noise for similarity checks."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return _WS_RE.sub(" ", text).strip()
