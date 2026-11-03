# Adding a detector

A detector is any object with:

```python
class MyDetector:
    name = "mydetector"          # used by consensus scoring to tell
                                 # independent sources apart

    def __init__(self, config: MyDetectorConfig): ...

    def detect(self, path: Path, language: str, source: str) -> list[Finding]:
        ...
```

`detect()` must be **pure and side-effect free** w.r.t. the repository
(network is allowed only for provenance-style detectors and must be opt-in,
cacheable, and three-state — see below). It must never raise on malformed
input; the property-based test (`test_ast_detector_never_crashes_on_arbitrary_text`)
encodes this contract for the AST detector and yours should hold the same bar.

## Checklist

1. **Rule id + metadata** (`src/slop_guard/models.py`):
   * add `RULE_MY_RULE = "SG-XXX-001"`;
   * add entries to `RULE_DESCRIPTIONS` and `RULE_DOC_ANCHORS`;
   * document the rule in `docs/rules.md` with a heading whose GitHub anchor
     matches `RULE_DOC_ANCHORS` (e.g. `## SG-XXX-001 My rule` →
     `#sg-xxx-001-my-rule`).

2. **Config section** (`src/slop_guard/config.py`):
   * add a `MyDetectorConfig` dataclass with an `enabled: bool` and sane
     defaults (detector must work with no config file present);
   * wire it into `Config` and `load_config()` (`detectors.mydetector:` YAML key);
   * document every key in `docs/configuration.md` and
     `.slop-guard.yaml.example`. **Never add a config key the code does not
     read** — dead options are how `duplicate_similarity` ended up broken once.

3. **Detector module** (`src/slop_guard/detectors/my_detector.py`):
   * emit `Finding(rule_id=..., detector=self.name, ...)` with 1-based
     line/column, `severity`, honest `confidence` (≥ `min_confidence` only
     when you would show it to a user), and a one-line `suggested_fix`;
   * skip unsupported languages early (`if language != "python": return []`);
   * **fail-open**: anything uncertain must produce *no* finding.

4. **Pipeline wiring** (`src/slop_guard/pipeline.py`): instantiate in
   `ScanPipeline.__init__` behind `config.mydetector.enabled` and call it in
   the scan loop (respect `RoutedFile.provenance_only` semantics if your
   detector shouldn't see docs-only files).

5. **Tests** (`tests/test_my_detector.py`):
   * positive micro-fixtures per rule variant;
   * negative tests: clean code, near-miss code, unsupported language;
   * add the defect class to `tests/fixtures/bad/` and a clean counterpart to
     `tests/fixtures/good/` (the good fixture must stay at **zero findings**);
   * no network in tests — inject fakes (see `test_provenance_detector.py`
     for the resolver-injection pattern, `test_cross_encoder.py` for
     monkeypatching heavy optional deps).

6. **Docs**: README rule table + `docs/rules.md` section.

## Severity guidance

| Severity | Meaning | Gate default |
|---|---|---|
| error | near-certain defect, breaks runtime or truthfulness (hallucinated API) | fails `--fail-on error` |
| warning | high-confidence slop, human should look (dead import, stub, phantom source) | fails `--fail-on warning` |
| info | stylistic AI tell (noise comment, duplication) | informational |

## Confidence guidance

Confidence is calibrated against the global `min_confidence` floor (0.5
default) and consensus boosting (+0.1 per additional agreeing detector,
capped at 0.99). Reserve ≥0.9 for deterministic evidence (AST facts,
registry 404s); use 0.6–0.85 for heuristics; never emit <0.5 (it would be
silently dropped anyway).

## Optional heavy dependencies

Anything beyond the core runtime deps must be an *extra* and lazily imported
inside the using function (see `llm/cross_encoder.py`), with a logged
fallback so scans never crash because an extra is missing.
