"""Typer CLI: `slop-guard scan`, `snapshot`, `languages`.

Exit codes: 0 clean · 1 findings at/above --fail-on · 2 usage/router error.
"""

from __future__ import annotations

import json
import logging
import sys
from enum import StrEnum
from pathlib import Path

import typer

from . import __version__
from .config import find_config, load_config
from .models import FailOn
from .pipeline import ScanPipeline
from .router import SUPPORTED_LANGUAGES, RouterError
from .scoring import build_sarif, format_text, summary_lines

app = typer.Typer(add_completion=False, help="Detect AI hallucinations and slop in code.")


class Format(StrEnum):
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
    config: Path | None = typer.Option(None, "--config",
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
    min_confidence: float | None = typer.Option(
        None, "--min-confidence", help="Override config min_confidence floor."),
    verify_sources: bool = typer.Option(
        False, "--verify-sources",
        help="Opt IN to online DOI/arXiv verification (network)."),
    offline: bool = typer.Option(
        False, "--offline",
        help="Force-disable all network access (provenance + ollama probe)."),
    no_semantic: bool = typer.Option(False, "--no-semantic",
                                     help="Disable the semantic detector."),
    out: Path | None = typer.Option(None, "-o", "--output",
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
        raise typer.Exit(code=2) from exc

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
        raise typer.Exit(code=2) from exc
    except ValueError as exc:                    # defensive; enums catch typos
        typer.secho(f"invalid option: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

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

    raise typer.Exit(code=1 if result.exceeds(fail_on) else 0)


@app.command()
def languages() -> None:
    """Show supported language ids (routing by extension)."""
    for lang in SUPPORTED_LANGUAGES:
        typer.echo(lang)


@app.command()
def snapshot(
    spec: str = typer.Argument(..., help="Package spec, e.g. pandas==2.2.3"),
    out_dir: Path = typer.Option(None, "--out",
                                 help="Output dir (default: package api_snapshots/)."),
    max_members: int = typer.Option(400, "--max-members"),
    submodule: list[str] | None = typer.Option(
        None, "--submodule", help="Extra dotted submodule to snapshot (repeatable)."),
) -> None:
    """Generate an offline API snapshot for an *installed* package version.

    Uses dir()/inspect on the live interpreter. The generator ships inside
    the package (``slop_guard.snapshot_gen``), so this works from a wheel
    install as well as a source checkout.
    """
    from .snapshot_gen import generate_snapshot

    target_dir = out_dir or Path(__file__).resolve().parent / "api_snapshots"
    try:
        path = generate_snapshot(spec, target_dir, max_members=max_members,
                                 submodules=submodule or [])
    except ImportError:
        typer.secho(f"package for spec '{spec}' is not importable in this "
                    "environment — install it first (e.g. pip install "
                    f"'{spec.split('==')[0]}'), then re-run.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None
    typer.echo(f"wrote {path}")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"slop-guard {__version__}")
        raise typer.Exit(code=0)


@app.callback()
def _cli_callback(
    version: bool | None = typer.Option(
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
