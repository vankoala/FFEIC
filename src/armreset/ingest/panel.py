"""CFPB lender lists: the HMDA Reporter Panel (2018-2023) and the Data Browser filers list.

They no longer feed ``dim_hmda_lender``: the Philadelphia Fed HMDA Lender File does, for every
year (docs/verification.md, "Lender source"). The QA report uses these lists to check the
Lender File's coverage, RSSD IDs and agency codes.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import polars as pl

from armreset.fetch.panel import FILERS_SOURCE, SOURCE, panel_member
from armreset.manifest import Manifest

# The 2021 panel spells these without the underscore the data dictionary shows.
RENAMES = {"topholder_rssd": "top_holder_rssd", "topholder_name": "top_holder_name"}
LIST_COLUMNS = ["activity_year", "lei", "cfpb_list", "respondent_rssd", "agency_code"]


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


def _code(column: str) -> pl.Expr:
    return pl.col(column).str.strip_chars().cast(pl.Int64, strict=False)


def cfpb_lender_lists(manifest: Manifest) -> pl.DataFrame | None:
    """Every recorded CFPB lender list, one row per ``(activity_year, lei)``.

    ``cfpb_list`` is 'panel' or 'filers list'; a year with a panel ignores its filers list.
    ``respondent_rssd`` and ``agency_code`` come from the panel only (the panel's -1 is null).
    """
    frames: dict[int, pl.DataFrame] = {}
    for entry in manifest.entries(SOURCE):
        if manifest.is_cached(entry.key):
            year = int(entry.period or 0)
            frames[year] = read_panel(manifest.abspath(entry.path)).select(
                pl.lit(year, dtype=pl.Int32).alias("activity_year"),
                pl.col("lei").str.strip_chars().str.to_uppercase(),
                pl.lit("panel").alias("cfpb_list"),
                pl.when(_code("respondent_rssd") > 0)
                .then(_code("respondent_rssd"))
                .alias("respondent_rssd"),
                _code("agency_code").alias("agency_code"),
            )
    for entry in manifest.entries(FILERS_SOURCE):
        year = int(entry.period or 0)
        if year in frames or not manifest.is_cached(entry.key):
            continue
        institutions = json.loads(manifest.abspath(entry.path).read_text(encoding="utf-8"))
        frames[year] = pl.DataFrame(
            {
                "activity_year": [year] * len(institutions),
                "lei": [i["lei"].strip().upper() for i in institutions],
                "cfpb_list": ["filers list"] * len(institutions),
                "respondent_rssd": [None] * len(institutions),
                "agency_code": [None] * len(institutions),
            },
            schema={
                "activity_year": pl.Int32,
                "lei": pl.Utf8,
                "cfpb_list": pl.Utf8,
                "respondent_rssd": pl.Int64,
                "agency_code": pl.Int64,
            },
        )
    if not frames:
        return None
    return pl.concat([frames[y] for y in sorted(frames)], how="vertical_relaxed").select(
        LIST_COLUMNS
    )
