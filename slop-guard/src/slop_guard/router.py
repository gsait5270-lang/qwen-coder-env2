"""File Router: maps files to languages and selects the detector set.

Language detection uses file extensions (fast, deterministic). In PR mode
the router can restrict itself to files touched by ``git diff``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

# extension -> language id used in config `languages:` list
EXTENSION_MAP: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".md": "markdown",
}

SUPPORTED_LANGUAGES = sorted(set(EXTENSION_MAP.values()))


@dataclass
class RoutedFile:
    path: Path
    language: str

    @property
    def source(self) -> str:
        return self.path.read_text(encoding="utf-8", errors="replace")


def detect_language(path: Path) -> Optional[str]:
    return EXTENSION_MAP.get(path.suffix.lower())


def route_paths(
    paths: Iterable[Path],
    languages: Iterable[str],
    is_excluded,
) -> list[RoutedFile]:
    """Filter + tag files for the pipeline."""
    allowed = {l.lower() for l in languages}
    routed: list[RoutedFile] = []
    seen: set[Path] = set()
    for p in paths:
        rp = p.resolve()
        if rp in seen or not rp.is_file():
            continue
        seen.add(rp)
        if is_excluded(rp):
            continue
        lang = detect_language(rp)
        if lang is None or lang not in allowed:
            continue
        # markdown is only relevant for provenance checks; keep it
        routed.append(RoutedFile(path=rp, language=lang))
    return sorted(routed, key=lambda r: str(r.path))


def collect_files(root: Path) -> list[Path]:
    root = Path(root)
    if root.is_file():
        return [root]
    return [p for p in root.rglob("*") if p.is_file()]


def git_changed_files(repo_dir: Path, base_ref: str = "origin/main") -> list[Path]:
    """Files changed vs *base_ref* (PR mode), via ``git diff --name-only``."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_dir), "diff", "--name-only", f"{base_ref}...HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    files: list[Path] = []
    for line in out.splitlines():
        line = line.strip()
        if line:
            p = Path(repo_dir) / line
            if p.exists():
                files.append(p)
    return files
