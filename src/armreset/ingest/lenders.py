"""Philadelphia Fed HMDA Lender File -> ``dim_hmda_lender`` (PLAN.md §5.3, with the source
changed; see docs/verification.md, "Lender source").

One row per ``(activity_year, lei)`` for every year the file covers, all from this one file.
The raw agency and institution-type codes are kept next to the mapped ``lender_type``.
"""

from __future__ import annotations

from pathlib import Path

import fastexcel
import polars as pl

from armreset.fetch.lenders import KEY, lender_sheet
from armreset.manifest import Manifest
from armreset.segments import Segments

COLUMNS = [
    "activity_year",
    "lei",
    "name",
    "respondent_rssd",
    "parent_rssd",
    "top_holder_rssd",
    "agency_code",
    "institution_type",
    "lender_type",
    "in_call_reports",
]
# Lender File RSSD columns, where 0 means none: filer, direct parent, regulatory high holder.
RSSD_COLUMNS = {"RSSD": "respondent_rssd", "RSSDP": "parent_rssd", "RSSDHH": "top_holder_rssd"}


def read_lender_file(path: Path) -> pl.DataFrame:
    """The lender sheet with every column as text; numbers arrive as e.g. ``"2021"``."""
    sheet = fastexcel.read_excel(path).load_sheet(lender_sheet(path), dtypes="string")
    return sheet.to_polars()


def _int(column: str) -> pl.Expr:
    return pl.col(column).str.strip_chars().cast(pl.Float64, strict=False).cast(pl.Int64)


def _call_report_flag(df: pl.DataFrame, call_report_rssds: pl.DataFrame | None) -> pl.DataFrame:
    """Add ``in_call_reports``: the RSSD filed a Call Report in the activity year. Null for
    years with no Call Reports loaded."""
    if call_report_rssds is None:
        return df.with_columns(pl.lit(None, dtype=pl.Boolean).alias("in_call_reports"))
    filers = call_report_rssds.select(
        pl.col("activity_year").cast(pl.Int32),
        pl.col("rssd_id").cast(pl.Int64).alias("respondent_rssd"),
        pl.lit(True).alias("in_call_reports"),
    ).unique()
    years = sorted(set(filers["activity_year"].to_list()))
    return df.join(filers, on=["activity_year", "respondent_rssd"], how="left").with_columns(
        pl.when(pl.col("activity_year").is_in(years))
        .then(pl.col("in_call_reports").fill_null(False))
        .otherwise(None)
        .alias("in_call_reports")
    )


def lenders_from_file(
    raw: pl.DataFrame, segments: Segments, call_report_rssds: pl.DataFrame | None = None
) -> pl.DataFrame:
    """``call_report_rssds`` has ``activity_year`` and ``rssd_id`` for every Call Report filer."""
    df = raw.select(
        _int("YEAR").cast(pl.Int32).alias("activity_year"),
        pl.col("LEI").str.strip_chars().str.to_uppercase().alias("lei"),
        pl.col("NAMET").str.strip_chars().alias("name"),
        *[pl.when(_int(src) > 0).then(_int(src)).alias(dst) for src, dst in RSSD_COLUMNS.items()],
        _int("CODE").alias("agency_code"),
        _int("TYPE").alias("institution_type"),
    )
    if blank := df.filter(pl.col("activity_year").is_null() | pl.col("lei").is_null()).height:
        raise ValueError(f"The Lender File has {blank} rows with no YEAR or LEI")
    if df.select(pl.struct("activity_year", "lei").is_duplicated().any()).item():
        raise ValueError("The Lender File has repeated (YEAR, LEI) rows")
    df = _call_report_flag(df, call_report_rssds)
    return df.with_columns(segments.lender_type_expr().alias("lender_type")).select(COLUMNS)


def build_dim_hmda_lender(
    manifest: Manifest, segments: Segments, call_report_rssds: pl.DataFrame | None = None
) -> pl.DataFrame | None:
    """``dim_hmda_lender`` from the recorded Lender File, or None if there isn't one."""
    entry = manifest.get(KEY)
    if entry is None or not manifest.abspath(entry.path).exists():
        return None
    raw = read_lender_file(manifest.abspath(entry.path))
    return lenders_from_file(raw, segments, call_report_rssds).sort("activity_year", "lei")
