#!/usr/bin/env python3
"""Precision/recall harness for the slop-guard pipeline.

Runs the full pipeline (AST + semantic via bundled snapshots; provenance
offline unless --verify-sources) over ``benchmarks/dataset/`` and scores the
findings against ``manifest.json`` ground truth:

    TP  finding matches a labeled (rule, line)
    FP  finding with no label (any unlabeled finding counts against precision,
        so dataset files must be exhaustively labeled)
    FN  label with no matching finding

Usage:
    python benchmarks/eval.py [--min-precision 0.85] [--min-recall 0.85]
                              [--verify-sources] [--json out.json]

Exit code is non-zero when precision/recall fall below the targets (README
publishes numbers only from actual runs of this harness).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from slop_guard.config import Config  # noqa: E402
from slop_guard.pipeline import ScanPipeline  # noqa: E402


def load_manifest(dataset: Path) -> list[dict]:
    return json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))["files"]


def run(dataset: Path, verify_sources: bool) -> tuple[Counter, Counter, Counter, list[str]]:
    manifest = load_manifest(dataset)
    tp: Counter = Counter()
    fp: Counter = Counter()
    fn: Counter = Counter()
    skipped: list[str] = []

    for entry in manifest:
        rel = entry["path"]
        if entry.get("requires_online") and not verify_sources:
            skipped.append(rel)
            continue
        path = dataset / rel
        if not path.is_file():
            print(f"error: manifest references missing file {rel}", file=sys.stderr)
            sys.exit(2)

        cfg = Config()
        cfg.semantic.enabled = True
        cfg.exclude = []                    # dataset is pre-filtered by manifest
        cfg.respect_gitignore = False
        if entry.get("requires_online"):
            cfg.provenance.verify_online = True
            cfg.provenance.offline = False

        # scan the single file via its own directory-as-root
        result = ScanPipeline(cfg, root=path.parent).scan(path)

        expected = Counter((e["rule"], e["line"]) for e in entry["expect"])
        got = Counter((f.rule_id, f.line) for f in result.findings)
        for key in expected & got:                       # multiset intersection
            tp[key[0]] += min(expected[key], got[key])
        for key in got - expected:
            fp[key[0]] += (got - expected)[key]
        for key in expected - got:
            fn[key[0]] += (expected - got)[key]

    return tp, fp, fn, skipped


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", default=str(HERE / "dataset"))
    ap.add_argument("--min-precision", type=float, default=0.85)
    ap.add_argument("--min-recall", type=float, default=0.85)
    ap.add_argument("--verify-sources", action="store_true",
                    help="include online provenance samples (network)")
    ap.add_argument("--json", default=None, help="also write the raw metrics here")
    ns = ap.parse_args(argv)

    tp, fp, fn, skipped = run(Path(ns.dataset), ns.verify_sources)
    rules = sorted(set(tp) | set(fp) | set(fn))

    print(f"{'rule':12s} {'TP':>3s} {'FP':>3s} {'FN':>3s}  {'P':>6s} {'R':>6s} {'F1':>6s}")
    rows = {}
    for rule in rules:
        p, r, f1 = prf(tp[rule], fp[rule], fn[rule])
        rows[rule] = {"tp": tp[rule], "fp": fp[rule], "fn": fn[rule],
                      "precision": round(p, 4), "recall": round(r, 4), "f1": round(f1, 4)}
        print(f"{rule:12s} {tp[rule]:3d} {fp[rule]:3d} {fn[rule]:3d}  {p:6.3f} {r:6.3f} {f1:6.3f}")

    T, F, N = sum(tp.values()), sum(fp.values()), sum(fn.values())
    P, R, F1 = prf(T, F, N)
    print("-" * 44)
    print(f"{'TOTAL':12s} {T:3d} {F:3d} {N:3d}  {P:6.3f} {R:6.3f} {F1:6.3f}")
    if skipped:
        print(f"skipped (needs --verify-sources): {', '.join(skipped)}")

    if ns.json:
        payload = {"per_rule": rows,
                   "total": {"tp": T, "fp": F, "fn": N,
                             "precision": round(P, 4), "recall": round(R, 4),
                             "f1": round(F1, 4)},
                   "skipped": skipped}
        Path(ns.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"wrote {ns.json}")

    ok = P >= ns.min_precision and R >= ns.min_recall
    if not ok:
        print(f"FAIL: targets precision>={ns.min_precision} recall>={ns.min_recall}",
              file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
