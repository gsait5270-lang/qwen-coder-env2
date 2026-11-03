"""Tests for module 2c: provenance detector (phantom DOI/arXiv sources).

Network calls are mocked — unit tests must never hit crossref/arXiv. The
detector's resolvers are monkeypatched to deterministic stubs so we can test
both extraction regexes and the reporting logic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slop_guard.config import ProvenanceConfig
from slop_guard.detectors.provenance_detector import (
    ARXIV_RE,
    DOI_RE,
    ProvenanceDetector,
)
from slop_guard.models import RULE_PHANTOM_SOURCE, Severity


@pytest.fixture()
def detector() -> ProvenanceDetector:
    # verify_online=True + offline=False opens the network gate; the resolvers
    # below are monkeypatched, so no real HTTP happens.
    cfg = ProvenanceConfig(enabled=True, offline=False, verify_online=True,
                           timeout=2.0)
    det = ProvenanceDetector(cfg)
    # known-good identifiers; everything else is treated as phantom
    known_dois = {"10.1038/nature12373", "10.1145/3582085"}
    known_arxiv = {"1706.03762", "2005.14165v4"}
    det.resolve_doi = lambda doi: doi in known_dois          # type: ignore[method-assign]
    det.resolve_arxiv = lambda aid: aid in known_arxiv        # type: ignore[method-assign]
    return det


# ---------------------------------------------------------------------------
# extraction regexes
# ---------------------------------------------------------------------------

class TestExtraction:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("see doi:10.1038/nature12373", ["10.1038/nature12373"]),
            ("DOI 10.1145/3582085.", ["10.1145/3582085"]),
            ("https://dx.doi.org/10.1038/nature12373", ["10.1038/nature12373"]),
            ("no dois here", []),
        ],
    )
    def test_doi_regex(self, text: str, expected: list[str]):
        assert [m.group(1) for m in DOI_RE.finditer(text)] == expected

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("arXiv:1706.03762", ["1706.03762"]),
            ("arXiv 2005.14165v4 paper", ["2005.14165v4"]),
            ("https://arxiv.org/abs/1706.03762", ["1706.03762"]),
            ("old-style arXiv:cs.CL/0205001v1", ["cs.CL/0205001v1"]),
            ("nothing", []),
        ],
    )
    def test_arxiv_regex(self, text: str, expected: list[str]):
        assert [m.group(1) for m in ARXIV_RE.finditer(text)] == expected


# ---------------------------------------------------------------------------
# reporting logic
# ---------------------------------------------------------------------------

class TestPhantomReporting:
    def test_fake_doi_flagged(self, detector, tmp_path: Path):
        p = tmp_path / "doc.md"
        p.write_text("As shown in doi:10.9999/nonexistent.42 the method works.\n")
        found = detector.detect(p, "markdown", p.read_text())
        assert len(found) == 1
        f = found[0]
        assert f.rule_id == RULE_PHANTOM_SOURCE
        assert f.line == 1
        assert "10.9999/nonexistent.42" in f.message
        assert f.severity is Severity.WARNING

    def test_real_doi_not_flagged(self, detector, tmp_path: Path):
        p = tmp_path / "doc.md"
        p.write_text("Per doi:10.1038/nature12373 attention is all you need-ish.\n")
        assert detector.detect(p, "markdown", p.read_text()) == []

    def test_fake_arxiv_flagged(self, detector, tmp_path: Path):
        p = tmp_path / "notes.py"
        p.write_text("# reference: arXiv:2401.99999v3\nx = 1\n")
        found = detector.detect(p, "python", p.read_text())
        assert len(found) == 1
        assert found[0].line == 1
        assert "arXiv" in found[0].message

    def test_real_arxiv_not_flagged(self, detector, tmp_path: Path):
        p = tmp_path / "notes.py"
        p.write_text("# transformer paper: arXiv:1706.03762\n")
        assert detector.detect(p, "python", p.read_text()) == []

    def test_multiple_ids_on_one_line(self, detector, tmp_path: Path):
        p = tmp_path / "d.md"
        # NOTE: DOI registrant prefixes are 4-9 digits per the DOI spec;
        # `10.5/...` would never match DOI_RE (and is not a valid DOI shape).
        p.write_text("Compare doi:10.9999/fake1 and doi:10.9999/fake2 side by side.\n")
        found = detector.detect(p, "markdown", p.read_text())
        assert len(found) == 2

    def test_offline_mode_skips_network(self, tmp_path: Path):
        det = ProvenanceDetector(ProvenanceConfig(offline=True))
        # would explode without network if not skipped
        p = tmp_path / "d.md"
        p.write_text("doi:10.9999/definitely-not-real\n")
        assert det.detect(p, "markdown", p.read_text()) == []

    def test_check_flags_respected(self, tmp_path: Path):
        det = ProvenanceDetector(ProvenanceConfig(offline=False, verify_online=True,
                                                  check_doi=False, check_arxiv=False))
        det.resolve_doi = lambda d: False      # type: ignore[method-assign]
        det.resolve_arxiv = lambda a: False    # type: ignore[method-assign]
        p = tmp_path / "d.md"
        p.write_text("doi:10.9999/f arXiv:9999.99999\n")
        assert det.detect(p, "markdown", p.read_text()) == []

    def test_bad_fixture_contains_phantoms(self, detector, bad_fixture_path):
        """The bad fixture cites a fake DOI and fake arXiv id (last lines)."""
        src = bad_fixture_path.read_text()
        found = detector.detect(bad_fixture_path, "python", src)
        ids = {f.message for f in found}
        assert any("10.1007/s00366-020-01234-5678" in m for m in ids)
        assert any("2401.99999v3" in m for m in ids)


class TestTriStateResolution:
    """404 -> phantom; network failure -> unverifiable (never a finding)."""

    def test_default_config_is_offline(self, tmp_path: Path):
        det = ProvenanceDetector(ProvenanceConfig())   # defaults
        p = tmp_path / "d.md"
        p.write_text("doi:10.9999/definitely-not-real\n")
        assert det.detect(p, "markdown", p.read_text()) == []

    def test_resolver_exception_is_unverifiable_not_phantom(self, tmp_path: Path):
        det = ProvenanceDetector(ProvenanceConfig(offline=False, verify_online=True))

        def boom(_kind: str, _ident: str):
            raise OSError("network down")

        det.resolve_fn = boom
        p = tmp_path / "d.md"
        p.write_text("doi:10.9999/cannot-tell\n")
        assert det.detect(p, "markdown", p.read_text()) == []

    def test_phantom_status_flagged(self, tmp_path: Path):
        from slop_guard.detectors.provenance_detector import ResolveStatus

        det = ProvenanceDetector(ProvenanceConfig(offline=False, verify_online=True))
        det.resolve_fn = lambda kind, ident: ResolveStatus.PHANTOM
        p = tmp_path / "d.md"
        p.write_text("doi:10.9999/fake\n")
        found = det.detect(p, "markdown", p.read_text())
        assert len(found) == 1 and found[0].rule_id == RULE_PHANTOM_SOURCE

    def test_memoization_dedupes_repeat_lookups(self, tmp_path: Path):
        from slop_guard.detectors.provenance_detector import ResolveStatus

        calls: list[str] = []
        det = ProvenanceDetector(ProvenanceConfig(offline=False, verify_online=True))

        def fake_http(doi: str) -> ResolveStatus:
            calls.append(doi)
            return ResolveStatus.PHANTOM

        # patch the HTTP layer (not resolve_fn: injected resolvers are
        # deliberately never memoized so tests can observe every call)
        det._http_resolve_doi = fake_http  # type: ignore[method-assign]
        p = tmp_path / "d.md"
        p.write_text("doi:10.9999/same and again doi:10.9999/same\n")
        found = det.detect(p, "markdown", p.read_text())
        assert len(found) == 2            # both occurrences reported...
        assert calls == ["10.9999/same"]  # ...but resolved only once

    def test_disk_cache_roundtrip(self, tmp_path: Path):
        from slop_guard.detectors.provenance_detector import ResolutionCache, ResolveStatus

        cache_dir = tmp_path / "cache"
        c1 = ResolutionCache(cache_dir, ttl=60.0)
        c1.put("DOI:10.1/x", ResolveStatus.RESOLVED)
        c1.flush()
        c2 = ResolutionCache(cache_dir, ttl=60.0)
        assert c2.get("DOI:10.1/x") is ResolveStatus.RESOLVED
        # expired entries are dropped
        c3 = ResolutionCache(cache_dir, ttl=-1.0)
        assert c3.get("DOI:10.1/x") is None


class TestTimeoutHonoured:
    def test_default_timeout_is_two_seconds(self):
        cfg = ProvenanceConfig()
        assert cfg.timeout == 2.0  # spec §5 step 4
