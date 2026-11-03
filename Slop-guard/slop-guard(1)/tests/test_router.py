"""Tests for the file router: language detection, pruning, PR-mode git logic."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from slop_guard.router import (
    RouterError,
    collect_files,
    detect_language,
    git_changed_files,
    route_paths,
)


class TestLanguageDetection:
    @pytest.mark.parametrize("name,lang", [
        ("m.py", "python"), ("m.pyi", "python"), ("a.ts", "typescript"),
        ("a.md", "markdown"), ("a.rs", "rust"), ("a.unknown", None),
    ])
    def test_by_extension(self, name, lang):
        assert detect_language(Path(name)) == lang


class TestPruning:
    def test_heavy_dirs_are_never_walked(self, tmp_path: Path):
        for d in (".git/objects", "node_modules/pkg", ".venv/lib", "__pycache__"):
            (tmp_path / d).mkdir(parents=True)
            (tmp_path / d / "blob.py").write_text("x = 1")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "real.py").write_text("x = 1")
        found = {p.relative_to(tmp_path).as_posix() for p in collect_files(tmp_path)}
        assert found == {"src/real.py"}

    def test_symlinked_dirs_skipped(self, tmp_path: Path):
        real = tmp_path / "real"
        real.mkdir()
        (real / "a.py").write_text("x = 1")
        (tmp_path / "loop").symlink_to(real, target_is_directory=True)
        found = {p.name for p in collect_files(tmp_path)}
        assert found == {"a.py"}        # counted once, via real/


class TestRoutePaths:
    def test_markdown_is_provenance_only_by_default(self, tmp_path: Path):
        md = tmp_path / "README.md"
        md.write_text("# hi")
        py = tmp_path / "m.py"
        py.write_text("x = 1")
        routed = route_paths([md, py], ["python"], lambda p: False)
        by_name = {r.path.name: r for r in routed}
        assert by_name["m.py"].provenance_only is False
        assert by_name["README.md"].provenance_only is True

    def test_markdown_not_provenance_only_when_requested(self, tmp_path: Path):
        md = tmp_path / "README.md"
        md.write_text("# hi")
        routed = route_paths([md], ["python", "markdown"], lambda p: False)
        assert routed[0].provenance_only is False

    def test_exclusion_callback_respected(self, tmp_path: Path):
        a = tmp_path / "a.py"
        a.write_text("x = 1")
        assert route_paths([a], ["python"], lambda p: True) == []


# ---------------------------------------------------------------------------
# PR mode against a real throwaway git repository
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.local")
    _git(repo, "config", "user.name", "t")
    (repo / "base.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    return repo


class TestGitChangedFiles:
    def test_not_a_repo_raises(self, tmp_path: Path):
        with pytest.raises(RouterError, match="not a git repository"):
            git_changed_files(tmp_path)

    def test_unknown_base_ref_raises_with_hint(self, git_repo: Path):
        with pytest.raises(RouterError, match="fetch-depth"):
            git_changed_files(git_repo, "origin/main")

    def test_empty_diff_raises_strict(self, git_repo: Path):
        with pytest.raises(RouterError, match="empty"):
            git_changed_files(git_repo, "HEAD")

    def test_empty_diff_ok_when_not_strict(self, git_repo: Path):
        assert git_changed_files(git_repo, "HEAD", strict=False) == []

    def test_changed_files_listed_deletions_skipped(self, git_repo: Path):
        _git(git_repo, "branch", "base")
        (git_repo / "new.py").write_text("y = 2\n")
        (git_repo / "base.py").unlink()               # deleted -> not scannable
        _git(git_repo, "add", "-A")
        _git(git_repo, "commit", "-m", "change")
        files = git_changed_files(git_repo, "base")
        assert [f.name for f in files] == ["new.py"]
