"""Typer CLI: `slop-guard scan`, `snapshot`, `languages`.

Exit codes: 0 clean · 1 findings at/above --fail-on · 2 usage/router error.
"""

from __future__ import annotations

import json
import logging
import sys
from enum import Enum
from pathlib import Path
from typing import Optional

import typer

from . import __version__
from .config import find_config, load_config
from .models import FailOn, Severity
from .pipeline import ScanPipeline
from .router import SUPPORTED_LANGUAGES, RouterError
from .scoring import build_sarif, format_text, summary_lines

app = typer.Typer(add_completion=False, help="Detect AI hallucinations and slop in code.")


class Format(str, Enum):
    text = "text"
    json = "json"
    sarif = "sarif"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not verbose:
        # warnings/info from our own loggers still matter for transparency
        logging.getLogger("slop_guard").setLevel(logging.INFO)


@app.command()
def scan(
    target: Path = typer.Argument(Path("."), exists=True,
                                  help="File or directory to scan."),
    config: Optional[Path] = typer.Option(None, "--config",
                                          help="Path to .slop-guard.yaml."),
    output_format: Format = typer.Option(Format.text, "--format",
                                         help="Report format."),
    fail_on: FailOn = typer.Option(FailOn.WARNING, "--fail-on",
                                   help="Exit 1 when a finding of this severity "
                                        "(or higher) is reported."),
    pr_mode: bool = typer.Option(False, "--pr-mode",
                                 help="Scan only files changed vs base-ref (git)."),
    base_ref: str = typer.Option("origin/main", "--base-ref",
                                 help="Base ref for --pr-mode."),
    min_confidence: Optional[float] = typer.Option(
        None, "--min-confidence", help="Override config min_confidence floor."),
    verify_sources: bool = typer.Option(
        False, "--verify-sources",
        help="Opt IN to online DOI/arXiv verification (network)."),
    offline: bool = typer.Option(
        False, "--offline",
        help="Force-disable all network access (provenance + ollama probe)."),
    no_semantic: bool = typer.Option(False, "--no-semantic",
                                     help="Disable the semantic detector."),
    out: Optional[Path] = typer.Option(None, "-o", "--output",
                                       help="Write report to file instead of stdout."),
    verbose: bool = typer.Option(False, "--verbose", "-v",
                                 help="Debug logging to stderr."),
) -> None:
    """Scan TARGET and report detected slop."""
    _setup_logging(verbose)
    cfg_path = config or find_config(target)
    try:
        cfg = load_config(cfg_path)
    except FileNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    if min_confidence is not None:
        cfg.min_confidence = min_confidence
    if no_semantic:
        cfg.semantic.enabled = False
    if verify_sources:
        cfg.provenance.verify_online = True
        cfg.provenance.offline = False
    if offline:
        cfg.provenance.verify_online = False
        cfg.provenance.offline = True
        cfg.semantic.enabled = False   # never probe ollama in --offline mode

    root = target if target.is_dir() else target.parent
    try:
        pipeline = ScanPipeline(cfg, root=root)
        result = pipeline.scan(target, pr_mode=pr_mode, base_ref=base_ref)
    except RouterError as exc:
        typer.secho(f"router error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    except ValueError as exc:                    # defensive; enums catch typos
        typer.secho(f"invalid option: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    findings = result.findings
    if output_format == Format.sarif:
        payload = json.dumps(build_sarif(findings, root=root), indent=2)
    elif output_format == Format.json:
        payload = json.dumps([
            {"rule": f.rule_id, "file": f.file, "line": f.line, "column": f.column,
             "severity": f.severity.value, "confidence": round(f.confidence, 3),
             "message": f.message, "fix": f.suggested_fix}
            for f in findings], indent=2)
    else:
        payload = format_text(findings, root=root)

    if out:
        out.write_text(payload, encoding="utf-8")
    else:
        typer.echo(payload)

    # human summary always on stderr (never pollutes machine-readable stdout)
    typer.echo(summary_lines(len(result.scanned_files), findings, []), err=True)
    for err in result.errors:
        typer.secho(f"error: {err}", fg=typer.colors.YELLOW, err=True)

    if fail_on == FailOn.NEVER:
        raise typer.Exit(code=0)
    threshold = {"error": Severity.ERROR, "warning": Severity.WARNING,
                 "info": Severity.INFO}[fail_on.value]
    gate = any(f.severity.rank >= threshold.rank for f in findings)
    raise typer.Exit(code=1 if gate else 0)


@app.command()
def languages() -> None:
    """Show supported language ids (routing by extension)."""
    for l in SUPPORTED_LANGUAGES:
        typer.echo(l)


@app.command()
def snapshot(
    spec: str = typer.Argument(..., help="Package spec, e.g. pandas==2.2.3"),
    out_dir: Path = typer.Option(None, "--out",
                                 help="Output dir (default: package api_snapshots/)."),
    max_members: int = typer.Option(400, "--max-members"),
) -> None:
    """Generate an offline API snapshot for an *installed* package version.

    Uses dir()/inspect on the live interpreter — see tools/gen_snapshot.py
    (kept out of the wheel; importable path is resolved relative to repo root).
    """
    target_dir = out_dir or Path(__file__).resolve().parent / "api_snapshots"
    try:
        from tools.gen_snapshot import generate_snapshot  # type: ignore[import-not-found]
    except ImportError:
        gen = Path(__file__).resolve().parents[3] / "tools" / "gen_snapshot.py"
        if not gen.exists():
            typer.secho("snapshot generator lives in tools/gen_snapshot.py "
                        "(source checkout only)", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)
        import importlib.util

        mod_spec = importlib.util.spec_from_file_location("gen_snapshot", gen)
        module = importlib.util.module_from_spec(mod_spec)
        assert mod_spec.loader is not None
        mod_spec.loader.exec_module(module)
        generate_snapshot = module.generate_snapshot  # type: ignore[assignment]
    path = generate_snapshot(spec, target_dir, max_members=max_members)
    typer.echo(f"wrote {path}")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"slop-guard {__version__}")
        raise typer.Exit(code=0)


@app.callback()
def _cli_callback(
    version: Optional[bool] = typer.Option(
        None, "--version", "-V", callback=_version_callback, is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Root callback — provides a Typer-native ``--version`` flag."""


# Backwards-compatible aliases so both ``slop_guard.cli:cli`` (pyproject entry
# point) and ``slop_guard.cli:main`` work after the refactor.
cli = app
main = app


if __name__ == "__main__":  # pragma: no cover
    app()
