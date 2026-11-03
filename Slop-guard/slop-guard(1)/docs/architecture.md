# Architecture

```
CLI (typer)  /  GitHub Action (action.yml at repo root)
      │
      ▼
ScanPipeline (pipeline.py)
      │
      ├─ Config (config.py)            .slop-guard.yaml + .gitignore, dataclass defaults
      │
      ├─ Router (router.py)            collect files (pruned walk or git merge-base diff),
      │                                language by extension, pathspec exclusions,
      │                                markdown → provenance-only lane
      │
      ├─ Detectors (detectors/)        each: detect(path, language, source) → list[Finding]
      │     ├─ AstDetector             tree-sitter-python: stubs, dead imports,
      │     │                          noise comments, MinHash near-duplicates
      │     ├─ SemanticDetector        attribute chains → library paths (imports,
      │     │                          aliases, factory inference) → ApiExistenceCheckers
      │     │                          (offline snapshots + optional ollama LLM, consensus)
      │     └─ ProvenanceDetector      DOI/arXiv extraction → Crossref/arXiv
      │                                three-state resolve (resolved/phantom/unverifiable),
      │                                opt-in network, retries, memo + TTL disk cache
      │
      ├─ Scoring (scoring.py)          consensus boost (multi-detector agreement),
      │                                global min_confidence floor, SARIF 2.1.0 / text / json
      │
      └─ ScanResult                    findings + scanned_files + errors + exceeds(fail_on)
```

## Design principles

1. **Fail-open on uncertainty.** Unknown package, absent snapshot, dead
   network ⇒ *unverifiable* ⇒ no finding. A linter that cries wolf is worse
   than no linter; the good-fixture contract (zero findings on clean code)
   is enforced by tests.
2. **Deterministic offline core.** AST + snapshots need no network and no
   GPU; everything heavy (LLM, registries, cross-encoder) is opt-in.
3. **Immutable findings, pure scoring.** `Finding` is frozen; consensus
   scoring returns copies (`dataclasses.replace`), never mutates caller data.
4. **Stable alert identity.** `partialFingerprints` exclude line numbers so
   GitHub Code Scanning tracks the same logical issue across refactors.
5. **Schema-validated output.** SARIF is validated against the vendored
   OASIS schema in `tests/schemas/` on every CI run.
6. **Machine-readable stdout.** Reports go to stdout; summaries, warnings and
   logs go to stderr.

## Data flow of one scan

1. `ScanPipeline.plan()` resolves the file set: full pruned walk, or
   `git diff --diff-filter=d <merge-base>..HEAD` in PR mode (hard
   `RouterError` on unusable base ref — never a silent full-tree fallback).
2. `route_paths()` tags each file with a language and a
   `provenance_only` flag (markdown when `languages` doesn't include it).
3. Each routed file is read once and offered to AST → semantic → provenance
   detectors (provenance-only files skip the first two).
4. `consensus_score()` boosts confidence where independent detectors agree on
   the same location; `filter_by_threshold(min_confidence)` drops weak noise.
5. The CLI renders text/json/SARIF, prints the summary to stderr and exits
   0/1 (gate) or 2 (usage/router error).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | no findings at/above `--fail-on` |
| 1 | gate tripped: findings at/above `--fail-on` |
| 2 | usage error (bad path/enum) or `RouterError` (PR mode) |

## Module map

| File | Responsibility |
|---|---|
| `models.py` | `Finding`, `Severity`, `FailOn`, rule ids/descriptions/doc anchors, normalization helpers |
| `config.py` | dataclass config, YAML loading, pathspec exclusions (+`.gitignore`), parent-dir discovery |
| `router.py` | language routing, directory pruning, git PR diff, `RouterError` |
| `detectors/ast_detector.py` | SG-AST-001..004 (tree-sitter-python; other grammars via `[languages]` extra) |
| `detectors/semantic_detector.py` | SG-SEM-001 (import map, alias paths, factory inference, consensus veto) |
| `detectors/provenance_detector.py` | SG-PRV-001 (regex extraction, three-state HTTP resolution, caching, batching) |
| `llm/ollama_client.py` | `OfflineSnapshotClient` (version cascade), `OllamaClient`, `CachingChecker`, tolerant JSON parsing |
| `llm/cross_encoder.py` | optional semantic scorer for SG-AST-003 (`[semantic]` extra) |
| `llm/prompts.py` | LLM prompt templates |
| `snapshot_gen.py` | `slop-guard snapshot pkg==ver` — build snapshots from installed packages |
| `scoring.py` | consensus, thresholding, SARIF/text/json rendering, summary |
| `pipeline.py` | orchestration (`ScanPipeline`, `ScanResult`) |
| `cli.py` | typer app: `scan`, `snapshot`, `languages`, `--version` |
