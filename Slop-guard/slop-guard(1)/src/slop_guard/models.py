"""Core data models shared by all detectors and the report engine."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    @property
    def rank(self) -> int:
        return {"error": 3, "warning": 2, "info": 1}[self.value]


class FailOn(StrEnum):
    """Allowed values for ``--fail-on`` (validated by the CLI enum so a typo
    produces a clean usage error instead of a raw ValueError traceback)."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    NEVER = "never"


# Repository home; overridable via env SLOP_GUARD_REPO for forks/mirrors.
REPO_URL = os.environ.get(
    "SLOP_GUARD_REPO", "https://github.com/gsait5270-lang/qwen-coder-env2"
)
# Docs live in the package subdirectory of the repository (see slop-guard/docs/).
DOCS_URL = f"{REPO_URL}/blob/main/slop-guard/docs"


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

# Per-rule documentation anchors; SARIF helpUri points at these so the links
# rendered in the GitHub Code Scanning UI resolve to real pages.
RULE_DOC_ANCHORS: dict[str, str] = {
    RULE_STUB_FUNCTION: "rules.md#sg-ast-001-stub-function",
    RULE_DEAD_IMPORT: "rules.md#sg-ast-002-dead-import",
    RULE_NOISE_COMMENT: "rules.md#sg-ast-003-noise-comment",
    RULE_DUPLICATED_BLOCK: "rules.md#sg-ast-004-duplicated-block",
    RULE_HALLUCINATED_API: "rules.md#sg-sem-001-hallucinated-api",
    RULE_PHANTOM_SOURCE: "rules.md#sg-prv-001-phantom-source",
}


def rule_help_uri(rule_id: str) -> str:
    anchor = RULE_DOC_ANCHORS.get(rule_id, "rules.md")
    return f"{DOCS_URL}/{anchor}"


@dataclass(frozen=True)
class Finding:
    """A single issue reported by a detector.

    Immutable on purpose: scoring passes (consensus boost) use
    :func:`dataclasses.replace` instead of mutating caller-owned objects.
    """

    rule_id: str
    message: str
    file: str
    line: int                      # 1-based
    column: int = 1                # 1-based
    end_line: int | None = None
    severity: Severity = Severity.WARNING
    confidence: float = 0.5
    suggested_fix: str | None = None
    detector: str = ""

    @property
    def fingerprint(self) -> str:
        """Stable alert identity for SARIF ``partialFingerprints``.

        Deliberately excludes the *line number*: any unrelated edit above the
        finding shifts lines and would otherwise "recreate" the GitHub alert.
        The message is included only up to its first backtick-quoted symbol
        when present, so location stays rule+file+symbol based.
        """
        import posixpath

        norm_file = posixpath.normpath(str(self.file).replace("\\", "/"))
        raw = f"{self.rule_id}:{norm_file}:{self._anchor()}"
        return hashlib.blake2b(raw.encode("utf-8"), digest_size=8).hexdigest()

    def _anchor(self) -> str:
        """Symbol-ish anchor extracted from the message (first `code span`)."""
        m = re.search(r"`([^`]+)`", self.message)
        if m:
            return m.group(1)
        # fall back: normalized prefix of the message (no volatile numbers)
        return re.sub(r"\d+", "#", self.message.lower())[:80]


_WS_RE = re.compile(r"\s+")
_STR_START_RE = re.compile(r"[\"']")


def normalize_code(line: str) -> str:
    """Strip inline comments and collapse whitespace for comparison heuristics.

    Quote-aware: a ``#`` inside a string literal (e.g. a URL fragment or a
    colour code like ``"#ff0000"``) must not truncate the line.
    """
    out: list[str] = []
    quote: str | None = None
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if quote is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n:      # escaped char inside string
                out.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "#":                          # true comment start
            break
        if ch == "/" and i + 1 < n and line[i + 1] == "/":
            break                              # c-style comment
        out.append(ch)
        i += 1
    return _WS_RE.sub(" ", "".join(out)).strip()


def normalize_comment(text: str) -> str:
    """Lowercase, strip punctuation/stopwords-ish noise for similarity checks."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return _WS_RE.sub(" ", text).strip()
