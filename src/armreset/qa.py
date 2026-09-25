"""``armtool validate`` (PLAN.md §11): writes ``data/qa_report.md``.

The report is logged for review, not asserted: it shows counts, totals and flags so a person
can judge whether a quarter looks right. Nothing here fails a build.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import polars as pl

from armreset.db import connect, relations
from armreset.ingest.cdr import MdrmSpec
from armreset.model.repricing import HIGH_NONACCRUAL_SHARE, ROUNDING_TOLERANCE_USD
from armreset.settings import Settings

BUCKET_LABELS = {
    "b_le_3m": "≤3m",
    "b_3_12m": "3-12m",
    "b_1_3y": "1-3y",
    "b_3_5y": "3-5y",
    "b_5_15y": "5-15y",
    "b_gt_15y": ">15y",
}


def cdr_bank_counts(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    return con.sql(
        """
        SELECT f.report_date,
               count(*)                                              AS banks,
               count(*) FILTER (WHERE d.form = '031')                AS "031",
               count(*) FILTER (WHERE d.form = '041')                AS "041",
               count(*) FILTER (WHERE d.form = '051')                AS "051",
               count(*) FILTER (WHERE f.n_buckets_reported = 6)      AS "all six buckets",
               count(*) FILTER (WHERE f.n_buckets_reported BETWEEN 1 AND 5) AS "some buckets",
               count(*) FILTER (WHERE f.n_buckets_reported = 0)      AS "no buckets",
               sum(f.first_lien_total) FILTER (WHERE f.n_buckets_reported > 0)
                 / sum(f.first_lien_total)                           AS "first-lien coverage"
        FROM fact_cdr_repricing f
        JOIN dim_bank d USING (rssd_id, report_date)
        GROUP BY ALL ORDER BY f.report_date
        """
    ).pl()


def cdr_industry_totals(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    buckets = ",\n".join(f'sum({b}) / 1e9 AS "{label}"' for b, label in BUCKET_LABELS.items())
    return con.sql(
        f"""
        SELECT report_date,
               sum(first_lien_total) / 1e9    AS "first-lien total",
               {buckets},
               sum(sum_buckets) / 1e9         AS "sum of buckets",
               sum(implied_nonaccrual) / 1e9  AS "implied nonaccrual",
               sum(reported_nonaccrual) / 1e9 AS "reported nonaccrual",
               sum(b_le_3m + b_3_12m) / sum(first_lien_total) FILTER (WHERE n_buckets_reported > 0)
                                              AS "within 12m share"
        FROM fact_cdr_repricing
        GROUP BY ALL ORDER BY report_date
        """
    ).pl()


def cdr_flag_counts(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    counts = con.sql("SELECT report_date, flag, count(*) AS n FROM qa_cdr_flags GROUP BY ALL").pl()
    dates = con.sql("SELECT DISTINCT report_date FROM fact_cdr_repricing").pl()
    if counts.is_empty():
        return dates.sort("report_date").with_columns(pl.lit(0).alias("flags"))
    wide = counts.pivot(on="flag", index="report_date", values="n")
    return dates.join(wide, on="report_date", how="left").fill_null(0).sort("report_date")


def cdr_prefix_usage(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """Banks per quarter by the columns their values came from: one column per pattern."""
    long = con.sql(
        """
        SELECT report_date, coalesce(prefix_source, '(none)') AS columns_used, count(*) AS banks
        FROM fact_cdr_repricing GROUP BY ALL
        """
    ).pl()
    order = long.group_by("columns_used").agg(pl.col("banks").sum()).sort("banks", descending=True)
    wide = long.pivot(on="columns_used", index="report_date", values="banks")
    return wide.select("report_date", *order["columns_used"]).fill_null(0).sort("report_date")


def markdown_table(df: pl.DataFrame) -> str:
    def cell(value: object, column: str) -> str:
        if value is None:
            return ""
        if isinstance(value, float):
            return f"{value:.1%}" if "share" in column or "coverage" in column else f"{value:,.1f}"
        if isinstance(value, int):
            return f"{value:,}"
        return str(value)

    header = "| " + " | ".join(df.columns) + " |"
    rule = "|" + "|".join("---" for _ in df.columns) + "|"
    rows = [
        "| " + " | ".join(cell(v, c) for v, c in zip(row, df.columns, strict=True)) + " |"
        for row in df.iter_rows()
    ]
    return "\n".join([header, rule, *rows])


def report_path(settings: Settings) -> Path:
    return settings.warehouse_path.parent / "qa_report.md"


def write_report(settings: Settings) -> Path:
    con = connect(settings, read_only=True)
    try:
        names = {name for name, _ in relations(con)}
        sections = [
            "# QA report",
            "",
            f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC from "
            f"`{settings.warehouse_path.name}`. Logged for review, not asserted (PLAN.md §11).",
        ]
        if {"fact_cdr_repricing", "dim_bank", "qa_cdr_flags"} <= names:
            sections += [
                "",
                "## Call Reports (CDR)",
                "",
                "### Banks and bucket reporting",
                "",
                "First-lien coverage is the share of first-lien balances held by banks that "
                "report the repricing buckets.",
                "",
                markdown_table(cdr_bank_counts(con)),
                "",
                "### Industry totals ($bn)",
                "",
                "First-lien total is RCON5367 (domestic offices), the basis the buckets reconcile "
                "to. Implied nonaccrual is the first-lien total minus the buckets; reported "
                "nonaccrual is RC-N item 1.c.(2)(a), column C (RCONC229). The buckets hold "
                "loans repricing or maturing, measured from the report date.",
                "",
                markdown_table(cdr_industry_totals(con)),
                "",
                "### QA flags (bank-quarters)",
                "",
                f"Rounding tolerance ${ROUNDING_TOLERANCE_USD:,}; high-nonaccrual threshold "
                f"{HIGH_NONACCRUAL_SHARE:.0%} of the first-lien total. Flagged rows stay in "
                "the fact table.",
                "",
                markdown_table(cdr_flag_counts(con)),
                "",
                "### Columns used",
                "",
                markdown_table(cdr_prefix_usage(con)),
            ]
        else:
            sections += [
                "",
                "No Call Report tables yet: run `armtool fetch cdr` and `armtool build`.",
            ]
    finally:
        con.close()
    path = report_path(settings)
    path.write_text("\n".join(sections) + "\n", encoding="utf-8")
    return path


@dataclass
class SpotCheckRow:
    column: str
    mdrm: str | None
    caption: str
    line: str
    filed_thousands: str | None  # as filed in the bank's own report
    warehouse_usd: float | None
    match: bool


def read_sdf(path: Path) -> dict[str, dict[str, str]]:
    """A CDR SDF file as ``{MDRM: {rssd, value, caption, schedule, line}}``.

    Rows are "Call Date;Bank RSSD Identifier;MDRM #;Value;Last Update;Short Definition;
    Call Schedule;Line Number". Captions can contain semicolons, so each row is split from
    both ends. Values are thousands of dollars (ratios carry a % sign).
    """
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    lines = text.splitlines()
    if not lines or not lines[0].startswith("Call Date;"):
        raise ValueError(f"{path} is not a CDR SDF file")
    items = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        _, rssd, mdrm, value, _, rest = line.split(";", 5)
        caption, schedule, line_no = rest.rsplit(";", 2)
        items[mdrm] = {
            "rssd": rssd,
            "value": value.strip(),
            "caption": caption,
            "schedule": schedule,
            "line": line_no,
        }
    return items


def spot_check(
    con: duckdb.DuckDBPyConnection,
    spec: MdrmSpec,
    rssd_id: int,
    report_date: date,
    sdf: dict[str, dict[str, str]],
) -> list[SpotCheckRow]:
    """Compare a bank's fact row with the values in its own filed report."""
    fact = con.execute(
        "SELECT * FROM fact_cdr_repricing WHERE rssd_id = ? AND report_date = ?",
        [rssd_id, report_date],
    ).pl()
    if fact.is_empty():
        raise ValueError(f"No warehouse row for RSSD {rssd_id} on {report_date}")
    if (filer := {item["rssd"] for item in sdf.values()}) != {str(rssd_id)}:
        raise ValueError(f"The SDF belongs to RSSD {sorted(filer)}, not {rssd_id}")
    rows = []
    for column, code in spec.items.items():
        mdrm = next(
            (p + code for p in spec.prefixes_for(code) if sdf.get(p + code, {}).get("value")),
            None,
        )
        item = sdf.get(mdrm or "", {})
        filed = item.get("value")
        warehouse = fact[column][0]
        if filed is None:
            match = warehouse is None
        else:
            match = warehouse is not None and abs(float(filed) * 1_000 - warehouse) < 0.5
        rows.append(
            SpotCheckRow(
                column,
                mdrm,
                item.get("caption", ""),
                f"{item.get('schedule', '')} {item.get('line', '')}".strip(),
                filed,
                warehouse,
                match,
            )
        )
    return rows
