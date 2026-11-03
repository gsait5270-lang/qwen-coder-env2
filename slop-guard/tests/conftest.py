"""Shared pytest fixtures for slop-guard tests."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOT_DIR = Path(__file__).resolve().parents[1] / "src" / "slop_guard" / "api_snapshots"


@pytest.fixture()
def bad_fixture_path() -> Path:
    """AI-slop sample containing every defect class the tool targets."""
    return FIXTURES / "bad" / "hallucinated_pipeline.py"


@pytest.fixture()
def good_fixture_path() -> Path:
    """Clean human-written sample that must produce zero findings."""
    return FIXTURES / "good" / "clean_module.py"


@pytest.fixture()
def snapshot_dir() -> Path:
    """Bundled offline API-signature snapshots (deterministic CI mode)."""
    assert SNAPSHOT_DIR.is_dir(), f"missing snapshot dir: {SNAPSHOT_DIR}"
    return SNAPSHOT_DIR
