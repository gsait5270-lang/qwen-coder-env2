"""slop-guard CLI (Typer).

Examples
--------
    slop-guard scan ./src --format sarif --output report.sarif
    slop-guard scan . --pr-mode --fail-on warning
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer

from . import __version__
from .pipeline import scan
from .router import SUPPORTED_LANGUAGES
from .scoring import build_sarif, format_text

app = typer.Typer(
    name="slop-guard",
    help="Detect AI-generated 'slop': hallucinated APIs, dead imports, "
         "noise comments, stub functions and phantom sources.",
    add_completion=False,
)


def _version_callback(value: bool):
    if value:
        typer.echo(f"slop-guard {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True,
        help="Show version and exit.",
    ),
):
    ...


# Register commands under their natural names. Without explicit `name=` Typer
# derives "scan-cmd" from the function name `scan_cmd`, which would break the
# documented UX (`slop-guard scan ./src`).
@app.command(name="scan")
def scan_cmd(
    target: Path = typer.Argument(Path("."), help="File or directory to scan."),
    config: Optional[Path] = typer.Option(None, "--config", help="Path to .slop-guard.yaml."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write report to file."),
    fmt: str = typer.Option("text", "--format", "-f", help="text | sarif | json."),
    fail_on: str = typer.Option("error", "--fail-on", help="error | warning | info | never."),
    pr_mode: bool = typer.Option(False, "--pr-mode", help="Scan only files changed vs base ref."),
    base_ref: str = typer.Option("origin/main", "--base-ref", help="Base ref for --pr-mode."),
):
    """Scan a codebase (or PR diff) for AI-slop patterns."""
    if not target.exists():
        typer.secho(f"error: path does not exist: {target}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    result = scan(target, config_path=str(config) if config else None,
                  pr_mode=pr_mode, base_ref=base_ref)

    if fmt == "sarif":
        payload = json.dumps(build_sarif(result.findings, root=target), indent=2)
    elif fmt == "json":
        payload = json.dumps(
            [
                {
                    "rule": f.rule_id, "file": f.file, "line": f.line, "column": f.column,
                    "severity": f.severity.value, "confidence": round(f.confidence, 3),
                    "message": f.message, "fix": f.suggested_fix,
                }
                for f in result.findings
            ],
            indent=2,
        )
    else:
        payload = format_text(result.findings, root=target)

    if output:
        Path(output).write_text(payload, encoding="utf-8")
        typer.echo(f"wrote {fmt} report with {len(result.findings)} finding(s) to {output}", err=True)
    else:
        typer.echo(payload)

    if fail_on != "never" and result.exceeds(fail_on):
        raise typer.Exit(code=1)


@app.command()
def languages():
    """List supported language ids for the config `languages:` key."""
    for lang in SUPPORTED_LANGUAGES:
        typer.echo(lang)


def cli():  # console_scripts entry point
    app()


if __name__ == "__main__":
    cli()
