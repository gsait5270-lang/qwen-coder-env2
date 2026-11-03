# slop-guard rules

Every finding carries a stable rule id. SARIF reports link here via `helpUri`
(anchors below are generated from the headings — keep them in sync with
`RULE_DOC_ANCHORS` in `src/slop_guard/models.py`).

Severity/defaults:

| Rule | Name | Severity | Typical confidence |
|---|---|---|---|
| SG-AST-001 | Stub function | warning | 0.90 |
| SG-AST-002 | Dead import | warning | 0.95 |
| SG-AST-003 | Noise comment | info | 0.75 (0.95 with cross-encoder) |
| SG-AST-004 | Duplicated block | info | 0.5 + similarity/2 |
| SG-SEM-001 | Hallucinated API | error | ≥ threshold (0.7 default) |
| SG-PRV-001 | Phantom source | warning | 0.85 |

## SG-AST-001 Stub function

**What.** A function whose body is trivial (`pass`, `...`, bare `return`,
`raise NotImplementedError`) *and* carrying a TODO/FIXME/"implement later"/
"placeholder" marker in a leading comment, docstring, or the body itself.
Docstrings do not count as implementation, so

```python
def process(data):
    """TODO: implement real logic later."""
    pass
```

is flagged, while an implemented function that merely mentions TODO in a
docstring is not.

**Why it matters.** LLMs routinely emit scaffolding they never fill in; the
code is syntactically valid, imports cleanly, and silently does nothing.

**Tuning.** `detectors.ast.stub_threshold` (max effective body lines, default
3); `stub_threshold: -1` disables the rule.

**Suppress.** `# slop-guard: ignore[SG-AST-001]` anywhere in the file.

## SG-AST-002 Dead import

**What.** A top-level import binding never referenced in the same file.
Handles `import a.b` (binds `a`), aliases, `from m import n as o`,
parenthesised import lists. Names in `__all__` count as used (re-exports);
identifiers inside *string annotations* count as used; docstrings and other
string literals deliberately do **not**. `from x import *` disables the rule
for the whole file (bindings unknowable).

**Why it matters.** `import torch.nn.functional as F  # just in case` is a
signature AI artifact — dead weight that misleads readers and review tools.

**Tuning.** `detectors.ast.dead_imports: false` disables.

**Suppress.** `# slop-guard: ignore[SG-AST-002]`.

## SG-AST-003 Noise comment

**What.** A comment that merely restates adjacent code: standalone comments
are compared with the next (else previous) code line; trailing comments with
the code portion of their own line. Matching is token-based after removing
filler words ("the", "increment", "returns", ...). Optional semantic upgrade:
`detectors.ast.cross_encoder: true` (needs `pip install "slop-guard[semantic]"`)
scores (comment, code) pairs with `cross-encoder/ms-marco-MiniLM-L-6-v2`;
scores above `cross_encoder_threshold` (default 0.9) are noise.

**Why it matters.** `# increment i by 1` over `i += 1` adds zero information
and is one of the most recognizable AI-generation tells.

**Suppress.** `# slop-guard: ignore[SG-AST-003]`; directives
(`# type: ignore`, `# noqa`, `# pragma`) are never flagged.

## SG-AST-004 Duplicated block

**What.** Near-identical code windows within a file. Sliding windows of
`duplicate_min_lines` (default 5) normalized lines are fingerprinted with a
64-slot MinHash vector over 3-token shingles; window pairs whose slot
agreement (Jaccard estimate) reaches `duplicate_similarity` (default 0.9) are
reported. Exact duplicates always match; overlapping windows are skipped;
very large files fall back to exact grouping above an internal cap.

**Why it matters.** Model output often repeats a logic block with renamed
variables instead of extracting a helper.

**Tuning.** `duplicate_min_lines`, `duplicate_similarity`,
`duplicate_blocks: false`.

**Suppress.** `# slop-guard: ignore[SG-AST-004]`.

## SG-SEM-001 Hallucinated API

**What.** An attribute/method access whose target resolves to a known library
path, where every consulted backend definitively answers "does not exist"
(any authoritative "exists" vetoes the finding). Resolution covers:

* module attributes — `pd.read_csvv(...)` → `pandas.read_csvv`;
* aliased submodules — `import torch.nn.functional as F; F.gelu_approx(...)`
  → `torch.nn.functional.gelu_approx`;
* instance methods via factory inference — `df = pd.read_csv(p)` binds
  `pandas.DataFrame`, so `df.to_parquet_s3(...)` is checked against the
  DataFrame member pool.

Backends: bundled/generated JSON snapshots (`slop-guard snapshot pkg==ver`)
and an optional local LLM (ollama) with consensus scoring. Unknown packages
or partial knowledge ⇒ *unverifiable* ⇒ silent (fail-open by design).

**Why it matters.** Invented methods are the most damaging AI hallucination:
the code reads plausibly and fails only at runtime — or never runs at all
because tests were hallucinated too.

**Tuning.** `detectors.semantic.*` (see configuration.md), `--no-semantic`.

## SG-PRV-001 Phantom source

**What.** A DOI or arXiv identifier (in code comments or Markdown docs) that
the registry *definitively* does not know: Crossref answers 404, or the arXiv
API returns a feed without the entry. Three-state resolution:

* **resolved** — registry knows it → silent;
* **phantom** — definitive 404 → finding (confidence 0.85, warning);
* **unverifiable** — network error/timeout/429/5xx → **never** a finding
  (logged at INFO); a CI box without internet must not flag real citations.

Network checks are **opt-in**: `--verify-sources` or
`detectors.provenance.verify_online: true`. arXiv ids are batched into one
request; results are memoized per scan and optionally cached on disk
(`cache_dir`, `cache_ttl`); `mailto` enables Crossref's polite pool.
`.md` files are always routed to this detector even when `languages` lists
only code languages — documentation is where phantom citations live.

**Why it matters.** Fabricated references are pervasive in LLM-written
documentation and papers; they are checkable, so they must be checked.
