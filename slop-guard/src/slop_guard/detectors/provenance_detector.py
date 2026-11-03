"""Module 2c: Provenance detector — phantom source verification.

Extracts DOI / arXiv identifiers from comments and documentation and verifies
them against public registries:

* Crossref:  https://api.crossref.org/works/{doi}
* arXiv:     https://export.arxiv.org/api/query?id_list={id}

Identifiers that cannot be resolved are reported as "phantom sources" — a
frequent artifact of AI-generated text. Network checks honour the configured
timeout (spec: 2 s) and can be disabled entirely with ``offline: true``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import httpx

from ..config import ProvenanceConfig
from ..models import Finding, RULE_PHANTOM_SOURCE, Severity

DOI_RE = re.compile(
    r"\b(?:doi[:\s]*(?:https?://dx\.doi\.org/)?|https?://dx\.doi\.org/)"
    r"(10\.\d{4,9}/[^\s\"'<>)\]}]+?)(?=[.,;]?(?:\s|$|[\"'<>)\]}]))",
    re.I,
)
ARXIV_RE = re.compile(
    r"\b(?:arxiv[:\s]*(?:https?://arxiv\.org/(?:abs|pdf)/)?"
    r"|https?://arxiv\.org/(?:abs|pdf)/)"
    r"((?:\d{4}\.\d{4,5}(?:v\d+)?|[a-z\-]+(?:\.[A-Z]{2,7})?/\d{7}(?:v\d+)?))",
    re.I,
)
CROSSREF_URL = "https://api.crossref.org/works/{doi}"
ARXIV_URL = "https://export.arxiv.org/api/query?id_list={arxiv_id}"


class ProvenanceDetector:
    name = "provenance"

    def __init__(self, config: Optional[ProvenanceConfig] = None):
        self.config = config or ProvenanceConfig()

    # -- public API -----------------------------------------------------------

    def detect(self, path: Path, language: str, source: str) -> list[Finding]:
        findings: list[Finding] = []
        for line_no, line in enumerate(source.splitlines(), start=1):
            if self.config.check_doi:
                for m in DOI_RE.finditer(line):
                    doi = m.group(1).rstrip(".,;")
                    findings.extend(self._check(path, line_no, "DOI", doi, self.resolve_doi))
            if self.config.check_arxiv:
                for m in ARXIV_RE.finditer(line):
                    aid = m.group(1)
                    findings.extend(self._check(path, line_no, "arXiv", aid, self.resolve_arxiv))
        return findings

    # -- resolution ------------------------------------------------------------

    def resolve_doi(self, doi: str) -> bool:
        """True when Crossref knows this DOI."""
        try:
            r = httpx.get(CROSSREF_URL.format(doi=doi), timeout=self.config.timeout,
                          follow_redirects=True)
            return r.status_code == 200
        except Exception:
            return False  # unreachable treated as phantom per spec step 4

    def resolve_arxiv(self, arxiv_id: str) -> bool:
        try:
            r = httpx.get(ARXIV_URL.format(arxiv_id=arxiv_id), timeout=self.config.timeout,
                          follow_redirects=True)
            return r.status_code == 200 and "<entry>" in r.text
        except Exception:
            return False

    # -- helpers -----------------------------------------------------------------

    def _check(self, path: Path, line_no: int, kind: str, ident: str, resolver) -> list[Finding]:
        if self.config.offline:
            return []
        if resolver(ident):
            return []
        return [
            Finding(
                rule_id=RULE_PHANTOM_SOURCE,
                message=f"{kind} identifier `{ident}` could not be resolved ({kind.lower()} registry lookup failed).",
                file=str(path),
                line=line_no,
                column=1,
                severity=Severity.WARNING,
                confidence=0.85,
                suggested_fix=(
                    f"Verify the {kind.lower()} `{ident}` manually; remove it if it does not exist "
                    "(AI-generated text often contains fabricated references)."
                ),
                detector=self.name,
            )
        ]
