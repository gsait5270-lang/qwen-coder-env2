# Changelog

All notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow SemVer.

## [Unreleased]

### Fixed
- **Dead-import rule no longer silenced by docstrings**: only identifiers in
  *string annotations* count as usage now; arbitrary string literals (incl.
  docstrings) no longer mark imports as used.
- **Duplicate-block rule actually scanned code**: an inverted comment filter
  dropped every code line from the window index, so SG-AST-004 could never
  fire; fixed and covered by exact/near-duplicate tests.
- **Stub detection treats docstrings as documentation**, so
  `"""TODO: implement.""" + pass` bodies are now recognised as stubs.
- **Trailing noise comments** (`i += 1  # increment i by 1`) are compared
  against their own line again (regression from the previous revision).
- **Provenance dedupes identifiers within a scan** — the same DOI cited twice
  costs one registry lookup.
- `pathspec` 1.x compatibility (uses the `gitignore` pattern factory, falls
  back to `gitwildmatch` on 0.x) — removes DeprecationWarnings.
- `router._git` failure detection: a non-git directory now reports
  "not a git repository" instead of a misleading "base ref not found".
- Ruff-clean source tree (F401/F821); console-script entry point restored
  (`cli`/`main` aliases), `--version` flag back via a Typer callback.

### Added
- **CI** (`.github/workflows/ci.yml`): fresh install, ruff, mypy (advisory),
  pytest with coverage on 3.11–3.13, CLI smoke, self-scan dogfooding, SARIF
  schema validation of a real report.
- **GitHub Action** (`action.yml` at repo root): install → PR-mode scan →
  SARIF upload (`if: always()`) → severity gate.
- **Docs**: `docs/rules.md` (targets of SARIF `helpUri`),
  `docs/architecture.md`, `docs/adding-detectors.md`,
  `docs/configuration.md`, `.slop-guard.yaml.example`.
- **Benchmarks**: `benchmarks/eval.py` + labeled `benchmarks/dataset/`
  (precision/recall harness with gate thresholds).
- **Tests**: SARIF schema validation against the vendored OASIS schema,
  config loader, router/PR-mode (real throwaway git repos), snapshot
  generator round-trip + CLI, cross-encoder wiring/fallback, tri-state
  provenance resolution, caching, fingerprint stability across line shifts.
- `slop-guard snapshot` now works from a wheel install: the generator moved
  into the package (`slop_guard/snapshot_gen.py`); `tools/gen_snapshot.py`
  is a thin wrapper.
- Optional cross-encoder scorer for SG-AST-003 (`llm/cross_encoder.py`,
  `[semantic]` extra, `detectors.ast.cross_encoder` config).
- `respect_gitignore` config flag implemented (was declared but unused).
- `ScanResult.exceeds()` / `.count()` helpers (used by the CLI gate).
- `.gitignore`; removed the stray empty `при` file from the repo root.
- Python 3.13 classifier; `py.typed` shipped (added upstream this cycle).

### Changed
- SARIF `helpUri`/`informationUri` point at the real repository
  (`SLOP_GUARD_REPO` env overrides for forks); provenance `User-Agent`
  carries the actual package version.
- `[semantic]` extra no longer pulls `jedi` (unused);
  `tree-sitter-language-pack` moved to the new `[languages]` extra.
- Human summary printed once (stderr), pipeline log demoted to debug.

## [0.1.0] - 2026-11-03

Initial published design: AST/semantic/provenance detectors, consensus
scoring, SARIF 2.1.0 reporting, Typer CLI, offline API snapshots, opt-in
ollama and registry verification.
