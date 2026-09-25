"""``armtool build`` (PLAN.md §2): raw files -> staging Parquet -> DuckDB tables and views."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import polars as pl

from armreset.db import connect, create_views, replace_table
from armreset.ingest.cdr import MdrmSpec, StageResult, stage_quarter
from armreset.ingest.hmda import StagedYear, stage_year, staged_years, stg_dir
from armreset.ingest.lenders import build_dim_hmda_lender
from armreset.manifest import Manifest
from armreset.model.repricing import build_dim_bank, build_fact, qa_flags
from armreset.segments import Segments
from armreset.settings import Settings

log = logging.getLogger(__name__)


@dataclass
class BuildSummary:
    staged: list[StageResult] = field(default_factory=list)
    reused: list[Path] = field(default_factory=list)
    unusable: dict[str, str] = field(default_factory=dict)  # manifest key -> reason
    hmda: list[StagedYear] = field(default_factory=list)
    tables: dict[str, int] = field(default_factory=dict)
    views: list[str] = field(default_factory=list)


def cdr_staging_path(settings: Settings, report_date: date) -> Path:
    return settings.staging_dir / "cdr" / f"stg_cdr_{report_date.isoformat()}.parquet"


def _has_columns(path: Path, spec: MdrmSpec) -> bool:
    return set(spec.wanted_columns) <= set(pl.read_parquet_schema(path))


def _staged_from(path: Path, sha256: str, spec: MdrmSpec) -> bool:
    """True when ``path`` was staged from the zip with this hash under the current spec."""
    if not path.exists() or not _has_columns(path, spec):
        return False
    staged = pl.scan_parquet(path).select("source_sha256").head(1).collect()
    return staged.height == 1 and staged.item() == sha256


def stage_cdr(settings: Settings, spec: MdrmSpec, force: bool, summary: BuildSummary) -> list[Path]:
    """Stage every CDR quarter in the manifest; returns the staging files to model."""
    manifest = Manifest.for_settings(settings)
    files: list[Path] = []
    for entry in manifest.entries("cdr"):
        report_date = date.fromisoformat(entry.period or "")
        zip_path = manifest.abspath(entry.path)
        out = cdr_staging_path(settings, report_date)
        if not zip_path.exists():
            # The zip may be deleted once staged; the staging file carries the data.
            if out.exists() and _has_columns(out, spec):
                summary.reused.append(out)
                files.append(out)
            else:
                summary.unusable[entry.key] = (
                    f"{zip_path.name} is missing; re-run `armtool fetch cdr`"
                )
            continue
        if not force and _staged_from(out, entry.sha256, spec):
            summary.reused.append(out)
        else:
            result = stage_quarter(zip_path, report_date, spec, out, entry.sha256)
            for col, n in result.non_numeric.items():
                log.warning("%s: %d non-numeric values in %s", report_date, n, col)
            summary.staged.append(result)
            manifest.add_derived(entry.key, out)
        files.append(out)
    return files


def stage_hmda(settings: Settings, force: bool, summary: BuildSummary) -> None:
    """Stage every HMDA year in the manifest (one source per year; see ingest/hmda.py)."""
    manifest = Manifest.for_settings(settings)
    years = sorted({int(e.period) for e in manifest.entries("hmda") if e.period})
    for year in years:
        if (staged := stage_year(settings, manifest, year, force)) is not None:
            summary.hmda.append(staged)


def build(settings: Settings, force: bool = False) -> BuildSummary:
    spec = MdrmSpec.load(settings.config_file("mdrm.yaml"))
    segments = Segments.load(settings.config_file("segments.yaml"))
    summary = BuildSummary()
    tables: dict[str, pl.DataFrame] = {}

    files = stage_cdr(settings, spec, force, summary)
    if files:
        stg = pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")
        fact = build_fact(stg, spec)
        tables["dim_bank"] = build_dim_bank(stg)
        tables["fact_cdr_repricing"] = fact
        tables["qa_cdr_flags"] = qa_flags(fact, stg, spec)

    stage_hmda(settings, force, summary)
    filers = None
    if "dim_bank" in tables:
        filers = tables["dim_bank"].select(
            pl.col("report_date").dt.year().alias("activity_year"), "rssd_id"
        )
    lenders = build_dim_hmda_lender(Manifest.for_settings(settings), segments, filers)
    if lenders is not None:
        tables["dim_hmda_lender"] = lenders
        tables["dim_institution_type"] = segments.institution_type_table()
    params = {"unmapped_segment": segments.unmapped}
    if staged_years(settings):
        tables["dim_purchaser_segment"] = segments.purchaser_table()
        params["stg_hmda_glob"] = (stg_dir(settings) / "activity_year=*" / "*.parquet").as_posix()

    if not tables:
        return summary
    con = connect(settings)
    try:
        con.begin()
        for name, df in tables.items():
            summary.tables[name] = replace_table(con, name, df)
        summary.views = create_views(con, params)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return summary
