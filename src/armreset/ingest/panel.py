"""HMDA Reporter Panel -> ``dim_hmda_lender`` (PLAN.md §5.3).

One row per (lei, activity_year) with the raw agency and lender codes kept next to the mapped
``lender_type``. Years without a panel (2024 on; see docs/verification.md, item 7) get
names-only rows from the Data Browser filers list, with no RSSD and ``lender_type`` 'unknown'.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import polars as pl

from armreset.fetch.panel import FILERS_SOURCE, SOURCE, panel_member
from armreset.manifest import Manifest
from armreset.segments import Segments

INT_COLUMNS = [
    "respondent_rssd",
    "parent_rssd",
    "top_holder_rssd",
    "agency_code",
    "other_lender_code",
    "assets",
]
COLUMNS = [
    "activity_year",
    "lei",
    "name",
    "respondent_rssd",
    "parent_rssd",
    "top_holder_rssd",
    "agency_code",
    "other_lender_code",
    "assets",
    "state",
    "city",
    "in_call_reports",
    "lender_type",
    "panel_available",
]
# The 2021 panel spells these without the underscore the data dictionary shows.
RENAMES = {"topholder_rssd": "top_holder_rssd", "topholder_name": "top_holder_name"}


def read_panel(path: Path) -> pl.DataFrame:
    """The panel CSV (comma or pipe delimited, zipped or not) as text columns, lower-cased names."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            raw = zf.read(panel_member(zf))
    else:
        raw = path.read_bytes()
    first = raw.split(b"\n", 1)[0]
    separator = "|" if first.count(b"|") > first.count(b",") else ","
    df = pl.read_csv(io.BytesIO(raw), separator=separator, infer_schema_length=0)
    df = df.rename({c: c.strip().strip('"').lower() for c in df.columns})
    return df.rename({k: v for k, v in RENAMES.items() if k in df.columns})


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


def lenders_from_panel(
    panel: pl.DataFrame,
    year: int,
    segments: Segments,
    call_report_rssds: pl.DataFrame | None = None,
) -> pl.DataFrame:
    panel = panel.rename({k: v for k, v in RENAMES.items() if k in panel.columns})
    if missing := [c for c in ["lei", "respondent_name", *INT_COLUMNS] if c not in panel.columns]:
        raise ValueError(f"Panel for {year} lacks columns {missing}")
    if "activity_year" in panel.columns:
        years = set(panel["activity_year"].drop_nulls().cast(pl.Int64, strict=False).to_list())
        if years and years != {year}:
            raise ValueError(f"Panel for {year} holds activity years {sorted(years)}")
    df = (
        panel.with_columns(
            [pl.col(c).str.strip_chars().cast(pl.Int64, strict=False) for c in INT_COLUMNS]
        )
        .with_columns(
            # -1 is the panel's NULL/blank code
            [
                pl.when(pl.col(c) == -1).then(None).otherwise(pl.col(c)).alias(c)
                for c in ("respondent_rssd", "parent_rssd", "top_holder_rssd", "assets")
            ]
        )
        .select(
            pl.lit(year, dtype=pl.Int32).alias("activity_year"),
            pl.col("lei").str.strip_chars().str.to_uppercase(),
            pl.col("respondent_name").str.strip_chars().alias("name"),
            "respondent_rssd",
            "parent_rssd",
            "top_holder_rssd",
            "agency_code",
            "other_lender_code",
            "assets",
            pl.col("respondent_state").alias("state"),
            pl.col("respondent_city").alias("city"),
        )
    )
    df = _call_report_flag(df, call_report_rssds)
    return df.with_columns(
        segments.lender_type_expr().alias("lender_type"),
        pl.lit(True).alias("panel_available"),
    ).select(COLUMNS)


def lenders_from_filers(path: Path, year: int) -> pl.DataFrame:
    institutions = json.loads(path.read_text(encoding="utf-8"))
    return (
        pl.DataFrame(
            {
                "activity_year": [year] * len(institutions),
                "lei": [i["lei"].strip().upper() for i in institutions],
                "name": [i.get("name") for i in institutions],
            },
            schema_overrides={"activity_year": pl.Int32},
        )
        .with_columns(
            *[pl.lit(None, dtype=pl.Int64).alias(c) for c in INT_COLUMNS],
            pl.lit(None, dtype=pl.Utf8).alias("state"),
            pl.lit(None, dtype=pl.Utf8).alias("city"),
            pl.lit(None, dtype=pl.Boolean).alias("in_call_reports"),
            pl.lit("unknown").alias("lender_type"),
            pl.lit(False).alias("panel_available"),
        )
        .select(COLUMNS)
    )


def build_dim_hmda_lender(
    manifest: Manifest, segments: Segments, call_report_rssds: pl.DataFrame | None = None
) -> pl.DataFrame | None:
    """Every year with a panel (or, failing that, a filers list) in the manifest.

    ``call_report_rssds`` has ``activity_year`` and ``rssd_id`` for every Call Report filer.
    """
    frames: dict[int, pl.DataFrame] = {}
    for entry in manifest.entries(SOURCE):
        if manifest.is_cached(entry.key):
            year = int(entry.period or 0)
            panel = read_panel(manifest.abspath(entry.path))
            frames[year] = lenders_from_panel(panel, year, segments, call_report_rssds)
    for entry in manifest.entries(FILERS_SOURCE):
        year = int(entry.period or 0)
        if year not in frames and manifest.is_cached(entry.key):
            frames[year] = lenders_from_filers(manifest.abspath(entry.path), year)
    if not frames:
        return None
    dim = pl.concat([frames[y] for y in sorted(frames)], how="vertical_relaxed")
    if dim.select(pl.struct("activity_year", "lei").is_duplicated().any()).item():
        raise ValueError("dim_hmda_lender has repeated (activity_year, lei) rows")
    return dim.select(COLUMNS)
