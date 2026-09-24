"""``armtool``: the command-line entry point (PLAN.md §9). Every command is idempotent."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from armreset import __version__
from armreset.manifest import Manifest
from armreset.periods import (
    CDR_POSTING_LAG_DAYS,
    Quarter,
    latest_published_quarter,
    parse_years,
    quarter_range,
    resolve_quarter,
)
from armreset.settings import FIRST_HMDA_YEAR, Settings, load_settings

app = typer.Typer(
    help="ARM reset exposure: FFIEC Call Reports and HMDA, side by side, never summed.",
    no_args_is_help=True,
    add_completion=False,
)
fetch_app = typer.Typer(
    help="Download source files into data/raw. Files already in the manifest are skipped.",
    no_args_is_help=True,
)
app.add_typer(fetch_app, name="fetch")

console = Console()
err_console = Console(stderr=True)


@dataclass
class _State:
    config: Path | None = None
    _settings: Settings | None = None

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            try:
                self._settings = load_settings(self.config)
            except (FileNotFoundError, ValidationError) as exc:
                err_console.print(f"[red]Config error:[/red] {exc}")
                raise typer.Exit(code=2) from exc
        return self._settings


def _settings(ctx: typer.Context) -> Settings:
    return ctx.ensure_object(_State).settings


def _show_version(value: bool) -> None:
    if value:
        console.print(f"armtool {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            dir_okay=False,
            help="Config file (default: $ARMRESET_CONFIG, then ./config.yaml).",
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_show_version, is_eager=True, help="Show the version and exit."
        ),
    ] = False,
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    ctx.obj = _State(config=config)


def _not_implemented(command: str, phase: int) -> None:
    err_console.print(
        f"[yellow]`armtool {command}` is not implemented yet; it arrives in phase {phase} "
        "(PLAN.md §12).[/yellow]"
    )
    raise typer.Exit(code=1)


def _quarter(spec: str, option: str) -> Quarter:
    try:
        return resolve_quarter(spec)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint=option) from exc


def _quarters(start: str, end: str) -> list[Quarter]:
    try:
        return quarter_range(_quarter(start, "--start"), _quarter(end, "--end"))
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--start/--end") from exc


def _years(spec: str | None, default: list[int]) -> list[int]:
    try:
        years = parse_years(spec) if spec else default
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--years") from exc
    if early := [y for y in years if y < FIRST_HMDA_YEAR]:
        raise typer.BadParameter(
            f"{early} predate {FIRST_HMDA_YEAR}, the first LAR with intro_rate_period",
            param_hint="--years",
        )
    return years


@fetch_app.command("cdr")
def fetch_cdr(
    ctx: typer.Context,
    start: Annotated[
        str | None, typer.Option(help="First quarter (2018Q1) or 'latest'. Default: cdr.start.")
    ] = None,
    end: Annotated[
        str | None, typer.Option(help="Last quarter (2026Q2) or 'latest'. Default: cdr.end.")
    ] = None,
) -> None:
    """Call Report bulk zips ("Call Reports -- Single Period", tab-delimited)."""
    s = _settings(ctx)
    quarters = _quarters(start or s.cdr.start, end or s.cdr.end)
    console.print(f"CDR quarters requested: {len(quarters)} ({quarters[0]} to {quarters[-1]})")
    _not_implemented("fetch cdr", 1)


@fetch_app.command("hmda")
def fetch_hmda(
    ctx: typer.Context,
    years: Annotated[
        str | None, typer.Option(help="Years, e.g. 2018-2025 or 2021. Default: hmda.years.")
    ] = None,
    by_state: Annotated[
        bool | None,
        typer.Option(
            "--by-state/--nationwide",
            help="Loop over states instead of one nationwide file. Default: hmda.by_state.",
        ),
    ] = None,
) -> None:
    """HMDA LAR CSVs from the Data Browser API, converted to Parquet."""
    s = _settings(ctx)
    wanted = _years(years, s.hmda.years)
    mode = "by state" if (s.hmda.by_state if by_state is None else by_state) else "nationwide"
    console.print(f"HMDA years requested: {', '.join(map(str, wanted))} ({mode})")
    _not_implemented("fetch hmda", 3)


@fetch_app.command("panel")
def fetch_panel(
    ctx: typer.Context,
    years: Annotated[
        str | None, typer.Option(help="Years, e.g. 2018-2025. Default: hmda.years.")
    ] = None,
) -> None:
    """HMDA reporter panel files (lender identity and RSSD link)."""
    s = _settings(ctx)
    wanted = _years(years, s.hmda.years)
    console.print(f"HMDA panel years requested: {', '.join(map(str, wanted))}")
    _not_implemented("fetch panel", 3)


@app.command()
def build(ctx: typer.Context) -> None:
    """Ingest -> staging -> models -> views, into the DuckDB warehouse."""
    _settings(ctx)
    _not_implemented("build", 1)


@app.command()
def validate(ctx: typer.Context) -> None:
    """Write the QA report to data/qa_report.md."""
    _settings(ctx)
    _not_implemented("validate", 1)


@app.command()
def status(ctx: typer.Context) -> None:
    """What's downloaded, as-of dates, manifest summary."""
    s = _settings(ctx)
    manifest = Manifest.for_settings(s)
    latest = latest_published_quarter()

    def where(path: Path) -> str:
        shown = _relative(path, s.root)
        return shown if path.exists() else f"{shown} [dim](not created yet)[/dim]"

    overview = Table.grid(padding=(0, 2))
    overview.add_column(style="bold")
    overview.add_column()
    overview.add_row("Config", str(s.config_path))
    overview.add_row(
        "Contact",
        s.contact_email
        if s.contact_configured
        else "[yellow]not set; set contact_email in config.yaml (sent in the User-Agent)[/yellow]",
    )
    cdr_end = s.cdr.end
    if cdr_end == "latest":
        cdr_end = f"latest ({latest} with a {CDR_POSTING_LAG_DAYS}-day posting lag)"
    overview.add_row("CDR quarters", f"{s.cdr.start} to {cdr_end}")
    overview.add_row(
        "HMDA years",
        f"{min(s.hmda.years)} to {max(s.hmda.years)} "
        f"({'by state' if s.hmda.by_state else 'nationwide'})",
    )
    overview.add_row("Raw data", where(s.raw_dir))
    overview.add_row("Staging", where(s.staging_dir))
    overview.add_row("Warehouse", where(s.warehouse_path))
    overview.add_row("Manifest", f"{where(s.manifest_path)}, {len(manifest)} entries")
    console.print(overview)

    rows = manifest.summary()
    if not rows:
        console.print("\nNo downloads yet. Start with `armtool fetch cdr`.")
        return
    table = Table(title="Downloads", title_justify="left")
    for column in ("source", "files", "size", "first period", "last period", "last fetched"):
        table.add_column(column)
    table.add_column("missing on disk", justify="right")
    for row in rows:
        table.add_row(
            row["source"],
            str(row["files"]),
            _human_bytes(row["bytes"]),
            row["first_period"] or "-",
            row["last_period"] or "-",
            row["last_fetched"],
            str(row["missing"]),
        )
    console.print(table)


@app.command("app")
def run_app(ctx: typer.Context) -> None:
    """Launch the Streamlit dashboard."""
    _settings(ctx)
    _not_implemented("app", 5)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _human_bytes(n: float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:,.0f} {units[i]}" if i == 0 else f"{n:,.1f} {units[i]}"


if __name__ == "__main__":
    app()
