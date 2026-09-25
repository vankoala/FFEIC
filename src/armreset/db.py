"""DuckDB warehouse (PLAN.md §6): connections, table writes and view definitions."""

from __future__ import annotations

import re
from pathlib import Path

import duckdb
import polars as pl

from armreset.settings import Settings

# Views are (re)created by `armtool build` once the tables they read exist. Percent columns
# are percentage points (8.7 means 8.7%). Bucket amounts are "repricing or maturing",
# measured from each report date; see docs/methodology.md.
VIEWS: dict[str, tuple[frozenset[str], str]] = {
    "v_cdr_industry": (
        frozenset({"fact_cdr_repricing"}),
        """
        SELECT
            report_date,
            count(*)                                        AS n_banks,
            count(*) FILTER (WHERE n_buckets_reported > 0)  AS n_banks_reporting_buckets,
            count(*) FILTER (WHERE n_buckets_reported = 6)  AS n_banks_all_buckets,
            sum(total_assets)                               AS total_assets,
            sum(first_lien_total)                           AS first_lien_total,
            sum(b_le_3m)                                    AS b_le_3m,
            sum(b_3_12m)                                    AS b_3_12m,
            sum(b_1_3y)                                     AS b_1_3y,
            sum(b_3_5y)                                     AS b_3_5y,
            sum(b_5_15y)                                    AS b_5_15y,
            sum(b_gt_15y)                                   AS b_gt_15y,
            sum(sum_buckets)                                AS sum_buckets,
            sum(implied_nonaccrual)                         AS implied_nonaccrual,
            sum(reported_nonaccrual)                        AS reported_nonaccrual,
            sum(b_le_3m + b_3_12m)                          AS within_12m,
            sum(b_le_3m + b_3_12m + b_1_3y)                 AS within_3y,
            100 * sum(b_le_3m + b_3_12m) / nullif(sum(first_lien_total)
                FILTER (WHERE b_le_3m IS NOT NULL AND b_3_12m IS NOT NULL), 0)
                                                            AS within_12m_pct_first_lien,
            100 * sum(b_le_3m + b_3_12m + b_1_3y) / nullif(sum(first_lien_total)
                FILTER (WHERE b_le_3m IS NOT NULL AND b_3_12m IS NOT NULL
                        AND b_1_3y IS NOT NULL), 0)         AS within_3y_pct_first_lien,
            100 * sum(first_lien_total) FILTER (WHERE n_buckets_reported > 0)
                / nullif(sum(first_lien_total), 0)          AS bucket_coverage_pct
        FROM fact_cdr_repricing
        GROUP BY report_date
        """,
    ),
    # Banks that filed in the latest quarter in the warehouse. A bank whose last report is
    # older (merged, failed, converted) is left out: its loans now sit with another filer.
    "v_cdr_bank_latest": (
        frozenset({"fact_cdr_repricing", "dim_bank", "qa_cdr_flags"}),
        """
        WITH latest AS (
            SELECT max(report_date) AS report_date FROM fact_cdr_repricing
        ),
        flags AS (
            SELECT rssd_id, report_date, count(*) AS n_qa_flags,
                   string_agg(flag, ', ' ORDER BY flag) AS qa_flags
            FROM qa_cdr_flags GROUP BY ALL
        )
        SELECT
            f.rssd_id,
            f.report_date,
            d.name,
            d.city,
            d.state,
            d.form,
            d.fdic_cert,
            f.total_assets,
            f.first_lien_total,
            f.b_le_3m,
            f.b_3_12m,
            f.b_1_3y,
            f.b_3_5y,
            f.b_5_15y,
            f.b_gt_15y,
            f.b_le_3m + f.b_3_12m                                     AS within_12m,
            f.b_le_3m + f.b_3_12m + f.b_1_3y                          AS within_3y,
            100 * (f.b_le_3m + f.b_3_12m) / nullif(f.first_lien_total, 0)
                                                                      AS within_12m_pct_first_lien,
            100 * (f.b_le_3m + f.b_3_12m + f.b_1_3y) / nullif(f.first_lien_total, 0)
                                                                      AS within_3y_pct_first_lien,
            100 * (f.b_le_3m + f.b_3_12m) / nullif(f.total_assets, 0) AS within_12m_pct_assets,
            100 * (f.b_le_3m + f.b_3_12m + f.b_1_3y) / nullif(f.total_assets, 0)
                                                                      AS within_3y_pct_assets,
            f.implied_nonaccrual,
            f.reported_nonaccrual,
            coalesce(q.n_qa_flags, 0)                                 AS n_qa_flags,
            q.qa_flags,
            f.prefix_source
        FROM fact_cdr_repricing f
        JOIN latest USING (report_date)
        JOIN dim_bank d USING (rssd_id, report_date)
        LEFT JOIN flags q USING (rssd_id, report_date)
        """,
    ),
}


def connect(settings: Settings, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    path = settings.warehouse_path
    if read_only:
        if not path.exists():
            raise FileNotFoundError(f"No warehouse at {path}; run `armtool build` first.")
        return duckdb.connect(str(path), read_only=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


def replace_table(con: duckdb.DuckDBPyConnection, name: str, df: pl.DataFrame) -> int:
    """``CREATE OR REPLACE TABLE name`` from ``df``; returns the row count."""
    if not name.isidentifier():
        raise ValueError(f"Not a table name: {name!r}")
    con.register("incoming_df", df.to_arrow())
    try:
        con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM incoming_df")
    finally:
        con.unregister("incoming_df")
    return df.height


def relations(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    """(name, 'BASE TABLE' | 'VIEW') for everything in the main schema."""
    return con.execute(
        "SELECT table_name, table_type FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall()


def create_views(con: duckdb.DuckDBPyConnection) -> list[str]:
    """(Re)create every view whose tables exist; returns the names created."""
    tables = {name for name, kind in relations(con) if kind == "BASE TABLE"}
    created = []
    for name, (needs, sql) in VIEWS.items():
        if needs <= tables:
            con.execute(f"CREATE OR REPLACE VIEW {name} AS {sql}")
            created.append(name)
    return created


def load_saved_queries(path: Path) -> dict[str, str]:
    """The ``-- name: <title>`` blocks of ``config/saved_queries.sql``, in file order."""
    queries: dict[str, str] = {}
    name: str | None = None
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if match := re.match(r"--\s*name:\s*(.+)", line.strip()):
            if name is not None:
                queries[name] = "\n".join(lines).strip()
            name, lines = match[1].strip(), []
        elif name is not None:
            lines.append(line)
    if name is not None:
        queries[name] = "\n".join(lines).strip()
    return queries
