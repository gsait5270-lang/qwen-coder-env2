"""Tests for the API-snapshot generator and its round-trip through the
offline checker (the generator now ships inside the package)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from slop_guard.cli import app
from slop_guard.llm.ollama_client import OfflineSnapshotClient
from slop_guard.snapshot_gen import generate_snapshot


class TestGenerateSnapshot:
    def test_snapshot_of_installed_package(self, tmp_path: Path):
        # `pathspec` is a small pure-python dependency of slop-guard itself —
        # always importable in the test environment.
        out = generate_snapshot("pathspec", tmp_path, max_members=200)
        assert out == tmp_path / "pathspec.json"
        data = json.loads(out.read_text())
        assert data["package"] == "pathspec"
        assert data["version"]
        assert "PathSpec" in data["members"]["_any"]
        assert "match_file" in data["members"]["PathSpec"]

    def test_versioned_filename(self, tmp_path: Path):
        out = generate_snapshot("pathspec==9.9.9", tmp_path)
        assert out.name == "pathspec@9.9.9.json"

    def test_roundtrip_through_offline_client(self, tmp_path: Path):
        generate_snapshot("pathspec", tmp_path)
        client = OfflineSnapshotClient(tmp_path)
        assert client.check("pathspec.PathSpec", "match_file").exists is True
        bogus = client.check("pathspec.PathSpec", "match_file_totally_bogus")
        assert bogus.exists is False
        assert client.check("no_such_package_xyz", "anything").exists is None

    def test_unimportable_package_raises(self, tmp_path: Path):
        try:
            generate_snapshot("no_such_package_xyz", tmp_path)
        except ImportError:
            return
        raise AssertionError("expected ImportError")


class TestSnapshotCli:
    def test_cli_writes_snapshot(self, tmp_path: Path):
        res = CliRunner().invoke(
            app, ["snapshot", "pathspec", "--out", str(tmp_path)])
        assert res.exit_code == 0, res.stdout
        assert "wrote" in res.stdout
        assert (tmp_path / "pathspec.json").is_file()

    def test_cli_reports_unimportable_cleanly(self, tmp_path: Path):
        res = CliRunner().invoke(
            app, ["snapshot", "no_such_package_xyz", "--out", str(tmp_path)])
        assert res.exit_code == 2
        assert "not importable" in res.stderr
