# Configuration

slop-guard works with **no configuration at all**. When present, config is
discovered from the scan target upwards (`.slop-guard.yaml` /
`.slop-guard.yml`), or set explicitly with `--config`. Unknown keys are
ignored with a warning; broken YAML falls back to defaults (logged).

A copy-pastable template ships at
[`.slop-guard.yaml.example`](../.slop-guard.yaml.example).

## Full reference (defaults in comments)

```yaml
# Global floor: findings with lower confidence are dropped from every report.
min_confidence: 0.5

# Merge the scan root's .gitignore into `exclude` (gitignore semantics).
respect_gitignore: true

detectors:
  ast:
    enabled: true
    stub_threshold: 3          # max effective body lines considered a stub; -1 disables
    dead_imports: true
    noise_comments: true
    duplicate_blocks: true
    duplicate_min_lines: 5     # sliding-window size (normalized lines)
    duplicate_similarity: 0.9  # MinHash slot-agreement (Jaccard estimate) threshold
    cross_encoder: false       # SG-AST-003 via sentence-transformers ([semantic] extra)
    cross_encoder_model: cross-encoder/ms-marco-MiniLM-L-6-v2
    cross_encoder_threshold: 0.9

  semantic:
    enabled: false             # opt-in (snapshots always; ollama when reachable)
    model: qwen2.5:7b
    base_url: http://localhost:11434
    confidence_threshold: 0.7  # "does not exist" verdicts below this are dropped
    timeout: 30.0              # per LLM request
    probe_timeout: 1.0         # ollama reachability probe budget
    known_apis_dir: null       # default: bundled src/slop_guard/api_snapshots/
    versions: {}               # e.g. {pandas: "2.2.3"} → prompt + pkg@ver snapshot cascade
    cache_dir: null            # persist LLM answers across runs (JSON)

  provenance:
    enabled: true
    check_doi: true
    check_arxiv: true
    timeout: 2.0               # per registry request
    verify_online: false       # network checks are OPT-IN (or --verify-sources)
    offline: true              # legacy alias; offline:false implies verify_online:true
    retries: 2                 # transient failures (timeout/429/5xx) retried
    backoff: 0.3               # exponential backoff base seconds
    cache_dir: null            # TTL disk cache of resolutions between runs
    cache_ttl: 86400.0         # seconds
    mailto: null               # your email → Crossref polite pool

# Languages whose files get full AST/semantic analysis.
# Markdown is ALWAYS routed to the provenance detector regardless of this list.
languages: [python]

# gitignore-style patterns, matched against paths relative to the scan root.
exclude:
  - "tests/**"
  - "**/*.min.js"
  - "**/node_modules/**"
```

## CLI overrides

| Flag | Overrides |
|---|---|
| `--min-confidence FLOAT` | `min_confidence` |
| `--verify-sources` | `provenance.verify_online: true`, `offline: false` |
| `--offline` | forces `offline: true`, disables semantic LLM probing |
| `--no-semantic` | `semantic.enabled: false` |
| `--fail-on error\|warning\|info\|never` | gate severity (default: warning) |
| `--pr-mode [--base-ref REF]` | scan git merge-base diff instead of the tree |
| `--format text\|json\|sarif`, `-o/--output FILE` | report shape/destination |
| `-v/--verbose` | debug logging on stderr |

## Environment

| Variable | Effect |
|---|---|
| `SLOP_GUARD_REPO` | repository URL used for SARIF `helpUri`/`informationUri` (forks/mirrors) |

## Notes

* `languages` gates the **AST/semantic** lanes. Adding `markdown` there also
  runs docs through AST heuristics (rarely useful — license texts trip the
  duplicate detector); leave it out and rely on provenance-only routing.
* Exclusions use gitignore semantics via `pathspec`: `tests/**`, `*.min.js`,
  `/build` all behave as in `.gitignore`. With `respect_gitignore` (default
  on), the scan root's `.gitignore` is merged in automatically.
* Snapshots for pinned versions: `slop-guard snapshot pandas==2.2.3` writes
  `pandas@2.2.3.json`; pair with `semantic.versions: {pandas: "2.2.3"}`.
