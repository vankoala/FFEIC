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


def _is_latest(spec: str) -> bool:
    return spec.strip().lower() == "latest"


def _quarter(spec: str, option: str) -> Quarter:
    try:
        return Quarter.parse(spec)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint=option) from exc


def _warn_if_no_contact(s: Settings) -> None:
    if not s.contact_configured:
        err_console.print(
            "[yellow]contact_email is not set, so the User-Agent carries only the project "
            "URL. Set it in config.local.yaml.[/yellow]"
        )


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
    from armreset.fetch.cdr import CdrFetcher, manual_instructions

    s = _settings(ctx)
    start_spec, end_spec = start or s.cdr.start, end or s.cdr.end
    # Check explicit quarters before any request goes out.
    first = None if _is_latest(start_spec) else _quarter(start_spec, "--start")
    last = None if _is_latest(end_spec) else _quarter(end_spec, "--end")
    _warn_if_no_contact(s)
    fetcher = CdrFetcher(s, Manifest.for_settings(s))
    if first is None or last is None:
        latest, how = fetcher.latest_quarter()
        console.print(f"Latest CDR quarter: {latest} ({how})")
        first, last = first or latest, last or latest
    try:
        quarters = quarter_range(first, last)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--start/--end") from exc
    console.print(f"CDR quarters requested: {len(quarters)} ({first} to {last})")

    outcomes = fetcher.fetch(quarters)
    table = Table(title="CDR bulk files", title_justify="left")
    for column in ("quarter", "status", "file", "size", "published", "detail"):
        table.add_column(column)
    manifest = fetcher.manifest
    for o in outcomes:
        entry = manifest.get(f"cdr:{o.quarter.label}") if o.path else None
        table.add_row(
            o.quarter.label,
            o.status,
            o.path.name if o.path else "-",
            _human_bytes(entry.bytes) if entry else "-",
            (entry.published or "-") if entry else "-",
            o.detail,
        )
    console.print(table)
    if missing := [o.quarter for o in outcomes if o.status in ("failed", "skipped")]:
        err_console.print(manual_instructions(missing, fetcher.zip_dir))
        raise typer.Exit(code=1)


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
def build(
    ctx: typer.Context,
    force: Annotated[
        bool, typer.Option("--force", help="Re-stage every file, even when staging is current.")
    ] = False,
) -> None:
    """Ingest -> staging -> models -> views, into the DuckDB warehouse."""
    from armreset.pipeline import build as run_build

    s = _settings(ctx)
    summary = run_build(s, force=force)
    for result in summary.staged:
        console.print(
            f"Staged {result.report_date}: {result.n_banks:,} banks, "
            f"{len(result.present)} MDRM columns ({len(result.absent)} absent), "
            f"CDR data as of {result.data_as_of or 'unknown'}"
        )
    if summary.reused:
        console.print(f"Staging already current for {len(summary.reused)} file(s)")
    for key, reason in summary.unusable.items():
        err_console.print(f"[yellow]{key}: {reason}[/yellow]")
    if not summary.tables:
        err_console.print("Nothing to build yet. Run `armtool fetch cdr` first.")
        raise typer.Exit(code=1)
    table = Table(title=f"Warehouse {_relative(s.warehouse_path, s.root)}", title_justify="left")
    table.add_column("table")
    table.add_column("rows", justify="right")
    for name, rows in summary.tables.items():
        table.add_row(name, f"{rows:,}")
    console.print(table)
    if summary.views:
        console.print(f"Views: {', '.join(summary.views)}")


@app.command()
def validate(ctx: typer.Context) -> None:
    """Write the QA report to data/qa_report.md."""
    from armreset.qa import write_report

    s = _settings(ctx)
    try:
        path = write_report(s)
    except FileNotFoundError as exc:
        err_console.print(str(exc))
        raise typer.Exit(code=1) from exc
    console.print(f"Wrote {_relative(path, s.root)}")


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
    overview.add_row("Config", ", ".join(_relative(f, s.root) for f in s.config_files))
    overview.add_row(
        "Contact",
        s.contact_email
        if s.contact_configured
        else "[yellow]not set; set contact_email in config.local.yaml (sent in the "
        "User-Agent)[/yellow]",
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
    _print_downloads(rows)
    if s.warehouse_path.exists():
        _print_warehouse(s)


def _print_downloads(rows: list[dict]) -> None:
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


def _print_warehouse(s: Settings) -> None:
    from armreset.db import connect, relations

    con = connect(s, read_only=True)
    try:
        table = Table(title="Warehouse", title_justify="left")
        for column in ("relation", "type", "rows"):
            table.add_column(column, justify="right" if column == "rows" else "left")
        for name, kind in relations(con):
            rows = con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            table.add_row(name, "view" if kind == "VIEW" else "table", f"{rows:,}")
    finally:
        con.close()
    console.print(table)


@app.command()
def spotcheck(
    ctx: typer.Context,
    rssd: Annotated[int, typer.Argument(help="The bank's IDRSSD, e.g. 852218.")],
    quarter: Annotated[
        str,
        typer.Option(
            help="Report quarter, e.g. 2026Q2. Default: the bank's latest in the warehouse."
        ),
    ] = "latest",
) -> None:
    """Compare one bank's warehouse figures with its own Call Report from CDR."""
    from armreset.db import connect
    from armreset.fetch.facsimile import fetch_facsimile
    from armreset.ingest.cdr import MdrmSpec
    from armreset.qa import read_sdf, spot_check

    s = _settings(ctx)
    spec = MdrmSpec.load(s.config_file("mdrm.yaml"))
    try:
        con = connect(s, read_only=True)
    except FileNotFoundError as exc:
        err_console.print(str(exc))
        raise typer.Exit(code=1) from exc
    try:
        if _is_latest(quarter):
            report_date = con.execute(
                "SELECT max(report_date) FROM dim_bank WHERE rssd_id = ?", [rssd]
            ).fetchone()[0]
        else:
            report_date = _quarter(quarter, "--quarter").end_date
        bank = con.execute(
            "SELECT name, form, fdic_cert FROM dim_bank WHERE rssd_id = ? AND report_date = ?",
            [rssd, report_date],
        ).fetchone()
        if bank is None:
            err_console.print(f"RSSD {rssd} has no Call Report in the warehouse for {quarter}.")
            raise typer.Exit(code=1)
        name, form, cert = bank
        if not cert or cert == "0":
            err_console.print(f"{name} has no FDIC certificate number; CDR facsimiles need one.")
            raise typer.Exit(code=1)
        paths = fetch_facsimile(s, Manifest.for_settings(s), cert, Quarter.containing(report_date))
        rows = spot_check(con, spec, rssd, report_date, read_sdf(paths["sdf"]))
    finally:
        con.close()

    console.print(f"{name}: RSSD {rssd}, FDIC cert {cert}, FFIEC {form}, report date {report_date}")
    table = Table(title_justify="left")
    table.add_column("column", no_wrap=True)
    table.add_column("MDRM", no_wrap=True)
    table.add_column("line", no_wrap=True)
    table.add_column("caption on the form")
    table.add_column("filed $mm", justify="right", no_wrap=True)
    table.add_column("warehouse $mm", justify="right", no_wrap=True)
    table.add_column("match", no_wrap=True)
    for r in rows:
        table.add_row(
            r.column,
            r.mdrm or "-",
            r.line,
            r.caption,
            f"{float(r.filed_thousands) / 1e3:,.1f}" if r.filed_thousands else "-",
            f"{r.warehouse_usd / 1e6:,.1f}" if r.warehouse_usd is not None else "-",
            "yes" if r.match else "[red]NO[/red]",
        )
    console.print(table)
    console.print(f"Filed report: {_relative(paths['pdf'], s.root)}")
    if not all(r.match for r in rows):
        raise typer.Exit(code=1)


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
