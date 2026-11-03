# slop-guard

[![CI](https://github.com/gsait5270-lang/qwen-coder-env2/actions/workflows/ci.yml/badge.svg)](https://github.com/gsait5270-lang/qwen-coder-env2/actions/workflows/ci.yml)

**CI/CD detector for AI hallucinations and “AI slop”.**

`slop-guard` is a static analyzer that works not with syntax but with the
**semantics and provenance** of code and text. It flags characteristic
AI-generation patterns that traditional linters (Ruff, ESLint, Semgrep)
cannot see — it *complements* them, it does not compete with them.

> Status: pre-1.0. The AST + SARIF core is stable; semantic verification via
> local LLM (ollama) and online source verification are opt-in.

---

## Why

A growing share of pull requests on GitHub is initiated by AI agents. This
creates new defect classes that pass every conventional check because they are
*syntactically valid*:

| Defect class | Example | Why ordinary linters miss it |
|---|---|---|
| **API hallucination** | `df.to_parquet_s3()` — method does not exist | Syntax is correct; the linter doesn’t know the real library API |
| **Dead import** | `import torch.nn.functional as F` never used | Left by models “just in case” |
| **Noise comment** | `# increment i by 1` above `i += 1` | Semantically redundant, not an error |
| **Template stub** | `# TODO: implement real logic` + empty body | Syntactically valid |
| **Phantom source** | Citation of a non-existent arXiv/DOI | Requires external registry checks |

## Rules

| Rule ID | Name | Severity | Default confidence | Detector |
|---|---|---|---|---|
| `SG-AST-001` | Stub function (TODO + trivial body) | warning | 0.90 | ast |
| `SG-AST-002` | Dead import (never referenced in file) | warning | 0.95 | ast |
| `SG-AST-003` | Noise comment (restates adjacent line) | info | 0.75 | ast |
| `SG-AST-004` | Duplicated block (MinHash near-clone) | info | 0.70 | ast |
| `SG-SEM-001` | Hallucinated API member | error | ≥ threshold | semantic |
| `SG-PRV-001` | Phantom DOI/arXiv source | warning | 0.85 | provenance |

Rule documentation: [docs/rules.md](docs/rules.md) — every SARIF report links
back to it via `helpUri`.

## Quick start

```bash
pip install ./slop-guard         # from a clone; PyPI release planned

slop-guard scan ./src                          # human-readable report
slop-guard scan ./src --format sarif -o report.sarif
slop-guard scan . --pr-mode --fail-on warning  # CI gate
slop-guard snapshot pandas==2.2.3              # regenerate API snapshot
slop-guard languages                           # supported language ids
```

Exit codes: `0` clean · `1` findings at/above `--fail-on` · `2` usage / router
error (e.g. broken PR base ref).

### Local LLM (optional)

Semantic verification prefers deterministic **offline snapshots** bundled in
`src/slop_guard/api_snapshots/`. If a local [ollama](https://ollama.com)
server is reachable, `qwen2.5:7b` (configurable) is added as a second checker
and answers are consensus-scored. Nothing ever leaves your machine.

## Configuration — `.slop-guard.yaml`

The config is auto-discovered from the scan target upwards
(`.slop-guard.yaml` / `.slop-guard.yml`). Full defaults:

```yaml
min_confidence: 0.5          # global floor for reported findings

detectors:
  ast:
    enabled: true
    stub_threshold: 3        # max effective body lines considered a "stub"
    dead_imports: true
    noise_comments: true
    duplicate_blocks: true
    duplicate_min_lines: 5   # window size (lines) for the clone detector
    duplicate_similarity: 0.9  # minhash-vector agreement threshold (0..1)
  semantic:
    enabled: false           # opt-in: needs snapshots and/or ollama
    model: "qwen2.5:7b"
    base_url: "http://localhost:11434"
    confidence_threshold: 0.7
    timeout: 30.0
    versions: {}             # {"pandas": "2.2.3"} -> prompt + snapshot cascade
    cache_dir: null          # JSON cache of LLM answers across runs
  provenance:
    enabled: true
    check_doi: true
    check_arxiv: true
    timeout: 2.0             # per-request timeout (spec: 2 seconds)
    verify_online: false     # network checks are OPT-IN (--verify-sources)
    mailto: null             # Crossref polite pool: give your email
    cache_dir: null          # TTL disk cache of resolution results
languages: [python]          # markdown (.md) always routed to provenance
exclude: ["tests/**", "**/*.min.js", "**/node_modules/**"]
```

CLI overrides: `--min-confidence`, `--verify-sources`, `--offline`,
`--no-semantic`, `--verbose`. Copy [.slop-guard.yaml.example](.slop-guard.yaml.example)
as a starting point; full reference: [docs/configuration.md](docs/configuration.md).

## GitHub Action

```yaml
# .github/workflows/slop-check.yml
name: Slop Guard
on: [pull_request]
jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0            # pr-mode needs merge-base history
      - uses: gsait5270-lang/qwen-coder-env2@main   # action.yml lives at repo root
        with:
          pr-mode: "true"
          fail-on: warning
```

The composite action scans changed files, uploads `report.sarif` to
GitHub Code Scanning (`github/codeql-action/upload-sarif`) **always**, then
gates the job on `fail-on`. See [`action.yml`](action.yml).

## Architecture

```
CLI / GitHub Action
        │
        ▼
 File Router (language by extension, exclude globs, git diff in PR mode)
        │
   ┌────┴─────────┬──────────────┐
   ▼              ▼              ▼
 AST detector  Semantic       Provenance
 (tree-sitter) detector       (Crossref/arXiv,
               (snapshots +    three-state resolve)
                local ollama)
   │              │              │
   └──────┬───────┴──────────────┘
          ▼
 Scoring & Report Engine (consensus boost → SARIF 2.1.0 / text / json)
```

Details: [docs/architecture.md](docs/architecture.md) ·
Writing new detectors: [docs/adding-detectors.md](docs/adding-detectors.md)

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -q                 # unit + integration + SARIF schema validation
ruff check src tools tests       # config lives in pyproject.toml
mypy src                         # strict-ish typing; CI gates on it
python benchmarks/eval.py        # precision/recall against the labeled set
slop-guard scan src --fail-on error   # dogfooding: we lint ourselves
```

## Benchmarks

[`benchmarks/eval.py`](benchmarks/eval.py) measures precision/recall of the
detector pipeline against the fully labeled dataset in
[`benchmarks/dataset/`](benchmarks/dataset/). Latest measured run:

| Mode | Findings | TP | FP | FN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|---|
| offline (snapshots only) | 5 | 5 | 0 | 0 | 1.00 | 1.00 | 1.00 |
| `--verify-sources` (online) | 7 | 7 | 0 | 0 | 1.00 | 1.00 | 1.00 |

The dataset is deliberately small (a smoke set, not a statistical claim);
grow it before trusting the numbers. Targets: precision ≥ 85 %, scan time
≤ 30 s per 50-file PR on CPU. Run `python benchmarks/eval.py --json` for
machine-readable output; non-zero exit if thresholds regress.

## License

Apache-2.0 — see [LICENSE](LICENSE).
