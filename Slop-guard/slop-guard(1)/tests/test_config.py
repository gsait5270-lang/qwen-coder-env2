"""Tests for the configuration loader (previously completely untested)."""

from __future__ import annotations

from pathlib import Path

import pytest

from slop_guard.config import Config, find_config, load_config


class TestDefaults:
    def test_defaults_work_without_file(self):
        cfg = load_config(None)
        assert cfg.ast.enabled and cfg.provenance.enabled
        assert cfg.semantic.enabled is False            # opt-in
        assert cfg.provenance.verify_online is False    # network is opt-in
        assert cfg.min_confidence == 0.5
        assert "python" in cfg.languages

    def test_explicit_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nope.yaml")


class TestLoading:
    def _write(self, tmp_path: Path, text: str, name: str = ".slop-guard.yaml") -> Path:
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_root_discovery(self, tmp_path: Path):
        self._write(tmp_path, "min_confidence: 0.8\n")
        cfg = load_config(root=tmp_path)
        assert cfg.min_confidence == 0.8

    def test_yml_variant_and_parent_walk(self, tmp_path: Path):
        self._write(tmp_path, "languages: [python, markdown]\n", name=".slop-guard.yml")
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert find_config(nested) == tmp_path / ".slop-guard.yml"
        cfg = load_config(root=nested)
        assert cfg.languages == ["python", "markdown"]

    def test_detector_sections_merge(self, tmp_path: Path):
        self._write(tmp_path, (
            "detectors:\n"
            "  ast:\n"
            "    duplicate_similarity: 0.7\n"
            "  semantic:\n"
            "    enabled: true\n"
            "    versions: {pandas: '2.2.3'}\n"
            "  provenance:\n"
            "    verify_online: true\n"
        ))
        cfg = load_config(root=tmp_path)
        assert cfg.ast.duplicate_similarity == 0.7
        assert cfg.ast.dead_imports is True             # untouched keys keep defaults
        assert cfg.semantic.enabled and cfg.semantic.versions == {"pandas": "2.2.3"}
        assert cfg.provenance.verify_online is True

    def test_legacy_offline_false_enables_online(self, tmp_path: Path):
        self._write(tmp_path, "detectors:\n  provenance:\n    offline: false\n")
        cfg = load_config(root=tmp_path)
        assert cfg.provenance.verify_online is True     # back-compat shim

    def test_unknown_keys_ignored_with_warning(self, tmp_path: Path, caplog):
        self._write(tmp_path, "detectors:\n  ast:\n    bogus_key: 1\n")
        cfg = load_config(root=tmp_path)
        assert cfg.ast.stub_threshold == 3              # defaults intact
        assert "bogus_key" in caplog.text

    def test_broken_yaml_falls_back_to_defaults(self, tmp_path: Path, caplog):
        self._write(tmp_path, "detectors: [unclosed\n")
        cfg = load_config(root=tmp_path)
        assert cfg.min_confidence == 0.5
        assert "cannot parse" in caplog.text


class TestExclusion:
    def test_relative_and_absolute_paths(self, tmp_path: Path):
        cfg = Config()
        root = tmp_path
        (root / "tests").mkdir()
        f = root / "tests" / "t.py"
        f.write_text("x = 1")
        assert cfg.is_excluded(f, root=root)            # absolute...
        assert cfg.is_excluded("tests/t.py")            # ...and relative
        assert not cfg.is_excluded(root / "src" / "m.py", root=root)

    def test_gitignore_respected_when_enabled(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("generated/\n")
        gen = tmp_path / "generated"
        gen.mkdir()
        f = gen / "g.py"
        f.write_text("x = 1")
        cfg = Config()
        assert cfg.respect_gitignore is True
        assert cfg.is_excluded(f, root=tmp_path)

    def test_gitignore_ignored_when_disabled(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("generated/\n")
        gen = tmp_path / "generated"
        gen.mkdir()
        f = gen / "g.py"
        f.write_text("x = 1")
        cfg = Config(respect_gitignore=False)
        assert not cfg.is_excluded(f, root=tmp_path)

    def test_exclude_from_yaml_is_used(self, tmp_path: Path):
        (tmp_path / ".slop-guard.yaml").write_text("exclude: ['vendor/**']\n")
        cfg = load_config(root=tmp_path)
        assert cfg.is_excluded("vendor/lib.py", root=tmp_path)
        assert not cfg.is_excluded("tests/t.py", root=tmp_path)   # default replaced
