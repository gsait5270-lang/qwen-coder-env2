"""File Router: maps files to languages and selects the detector set.

Language detection uses file extensions (fast, deterministic). In PR mode
the router can restrict itself to files touched by ``git diff``.

Responsibilities added in this revision:

* **Directory pruning** — never walk into ``.git``, virtualenvs, ``node_modules``
  etc. (rglob over ``.git/objects`` was scanning thousands of binary blobs).
* **Relative-path exclusion** — exclude patterns are matched against paths
  relative to the scan root (gitignore semantics via ``pathspec``), fixing the
  bug where ``fnmatch("/tmp/proj/tests/t.py", "tests/**")`` never matched.
* **Markdown routing** — ``.md`` files are routed *only* to the provenance
  detector (AST analysis of markdown would flag license texts and tables as
  "duplicated blocks").
* **Hard PR-mode failures** — an unusable base ref or an empty diff raises
  :class:`RouterError` (CLI exit code 2) instead of silently degrading to a
  full scan that hides regressions.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("slop_guard.router")

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

#: Languages whose *provenance* (DOI/arXiv references) we always check, even
#: when they are not listed in ``config.languages`` — documentation is exactly
#: where phantom AI citations live. AST/semantic detectors ignore them.
PROVENANCE_ONLY_LANGUAGES = {"markdown"}

#: Directory names pruned from recursive scans (VCS, envs, build artefacts).
PRUNED_DIRS = frozenset({
    ".git", ".hg", ".svn",
    ".venv", "venv", "env", ".tox", ".nox",
    "node_modules", "bower_components",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".hypothesis", ".idea", ".vscode",
    "dist", "build", ".eggs", "site-packages",
    ".next", ".nuxt", "target", "vendor",
})


class RouterError(RuntimeError):
    """Unrecoverable routing problem (bad base ref, empty PR diff...).

    The CLI maps this to exit code 2 with an actionable hint instead of
    silently falling back to a full scan.
    """


@dataclass
class RoutedFile:
    path: Path
    language: str
    #: true for files routed only to the provenance detector (e.g. markdown)
    provenance_only: bool = False

    @property
    def source(self) -> str:
        return self.path.read_text(encoding="utf-8", errors="replace")


def detect_language(path: Path) -> str | None:
    return EXTENSION_MAP.get(path.suffix.lower())


def route_paths(
    paths: Iterable[Path],
    languages: Iterable[str],
    is_excluded: Callable[[Path], bool],
    root: Path | None = None,
) -> list[RoutedFile]:
    """Filter + tag files for the pipeline.

    ``is_excluded`` receives the candidate path; callers bind the scan root so
    patterns match on *relative* paths (see :func:`Config.is_excluded`).
    Markdown is always accepted for provenance scanning (``provenance_only``),
    even if ``languages`` does not request it.
    """
    allowed = {lang.lower() for lang in languages}
    routed: list[RoutedFile] = []
    seen: set[Path] = set()
    for p in paths:
        rp = p.resolve()
        if rp in seen or not rp.is_file():
            continue
        seen.add(rp)
        lang = detect_language(rp)
        if lang is None:
            continue
        prov_only = lang in PROVENANCE_ONLY_LANGUAGES and lang not in allowed
        if not prov_only and lang not in allowed:
            continue
        if is_excluded(rp):
            log.debug("excluded: %s", rp)
            continue
        routed.append(RoutedFile(path=rp, language=lang, provenance_only=prov_only))
    return sorted(routed, key=lambda r: str(r.path))


def iter_pruned_files(root: Path) -> Iterable[Path]:
    """Yield files under *root*, pruning heavy/vendored directories early.

    Uses an explicit stack instead of ``rglob('**/*')`` so pruned trees
    (``.git/objects``, ``node_modules``, ``.venv``) are never descended into.
    Symlinked directories are skipped to avoid cycles.
    """
    root = Path(root)
    if root.is_file():
        yield root
        return
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            entries = sorted(cur.iterdir(), key=lambda e: e.name)
        except (PermissionError, OSError) as exc:      # unreadable dir -> skip
            log.debug("cannot read %s (%s)", cur, exc)
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name in PRUNED_DIRS:
                    log.debug("pruned directory: %s", entry)
                    continue
                stack.append(entry)
            elif entry.is_file():
                yield entry


def collect_files(root: Path) -> list[Path]:
    return list(iter_pruned_files(Path(root)))


def _git(repo_dir: Path, *args: str) -> str | None:
    """Run a git command; return stdout or None on *any* failure.

    (Previously a ``check=False`` mode returned stdout even on non-zero exit,
    so ``rev-parse --git-dir`` yielded ``""`` and the "not a git repository"
    branch could never trigger — callers test for ``None``.)
    """
    cmd = ["git", "-C", str(repo_dir), *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RouterError("git executable not found — --pr-mode requires git") from exc
    if proc.returncode != 0:
        log.debug("git failed: %s -> %s", " ".join(cmd), proc.stderr.strip())
        return None
    return proc.stdout


def git_changed_files(
    repo_dir: Path,
    base_ref: str = "origin/main",
    *,
    strict: bool = True,
) -> list[Path]:
    """Files changed vs *base_ref* (PR mode), via ``git diff --name-only``.

    Raises :class:`RouterError` (when ``strict``) if:

    * the repository has no such ref (typical for Actions with
      ``fetch-depth: 1``) — configure ``fetch-depth: 0`` or fetch the base;
    * the merge-base cannot be computed;
    * the diff is empty — nothing to scan must not be confused with success.

    Renames/deletions are handled with ``--diff-filter=d`` (skip deleted
    files; renamed files appear under their new path).
    """
    repo_dir = Path(repo_dir)
    if _git(repo_dir, "rev-parse", "--git-dir") is None:
        raise RouterError(f"{repo_dir} is not a git repository (required for --pr-mode)")

    # Resolve the base ref explicitly so we can give a good error message.
    if _git(repo_dir, "rev-parse", "--verify", "--quiet", base_ref) is None:
        raise RouterError(
            f"base ref '{base_ref}' not found in {repo_dir}. "
            "In GitHub Actions run `actions/checkout` with `fetch-depth: 0`, or "
            f"`git fetch --no-tags origin {base_ref.split('/')[-1]}` before scanning."
        )
    mb = _git(repo_dir, "merge-base", base_ref, "HEAD")
    if mb is None:
        raise RouterError(
            f"cannot compute merge-base between {base_ref} and HEAD in {repo_dir}"
        )

    out = _git(repo_dir, "diff", "--name-only", "--diff-filter=d",
               mb.strip(), "HEAD")
    if out is None:
        raise RouterError(f"git diff failed for range {base_ref}...HEAD")

    files: list[Path] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        p = repo_dir / line
        if p.exists():
            files.append(p)
        else:
            log.debug("changed file vanished (deleted/untracked?): %s", line)
    if not files and strict:
        raise RouterError(
            f"PR-mode diff vs {base_ref} is empty — nothing to scan. "
            "If this is intentional, run a normal scan without --pr-mode."
        )
    return files
