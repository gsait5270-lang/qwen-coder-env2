"""Standalone entry point kept for backwards compatibility.

The generator itself lives *inside* the package
(:mod:`slop_guard.snapshot_gen`) so that ``slop-guard snapshot`` also works
from a wheel install — a ``tools/`` directory is not shipped in the wheel and
the previous path-based fallback (``Path(__file__).parents[3] / "tools"``) was
both off by one and useless for pip installs.

Usage (source checkout, from the repository's ``slop-guard/`` directory)::

    python -m tools.gen_snapshot pandas==2.2.3 [--out DIR] [--install]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from slop_guard.snapshot_gen import generate_snapshot, main  # noqa: E402,F401

if __name__ == "__main__":
    raise SystemExit(main())
