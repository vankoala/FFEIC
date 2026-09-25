"""DuckDB warehouse (PLAN.md §6): connections, table writes and view definitions."""

from __future__ import annotations

import re
from pathlib import Path

import duckdb
import polars as pl

from armreset.settings import Settings

# Views are (re)created by `armtool build` once the tables and views they read exist, in
# this order. Percent columns are percentage points (8.7 means 8.7%). Bucket amounts are
# "repricing or maturing", measured from each report date; see docs/methodology.md.
# {placeholders} are filled from create_views(params=...); a view whose placeholder has no
# value is left as it is.
VIEWS: dict[str, tuple[frozenset[str], str]] = {
    # stg_hmda stays in Parquet (PLAN.md §6); the view reads it in place.
    "stg_hmda": (
        frozenset(),
        "SELECT * FROM read_parquet('{stg_hmda_glob}', hive_partitioning = false, "
        "union_by_name = true)",
    ),
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
    "v_hmda_orig_summary": (
        frozenset({"stg_hmda", "dim_purchaser_segment"}),
        """
        SELECT
            h.activity_year,
            h.rate_type,
            coalesce(s.holder_segment, '{unmapped_segment}') AS holder_segment,
            h.conforming_loan_limit                         AS conforming,
            count(*)                                        AS loans,
            sum(h.loan_amount)                              AS amount
        FROM stg_hmda h
        LEFT JOIN dim_purchaser_segment s USING (purchaser_type)
        GROUP BY ALL
        """,
    ),
    # One row per (activity_year, lei): the Lender File's RSSD and whether it files a Call Report.
    "v_bank_hmda_link": (
        frozenset({"stg_hmda", "dim_hmda_lender", "dim_bank"}),
        """
        WITH lar AS (
            SELECT activity_year, lei,
                   count(*)                                         AS loans,
                   sum(loan_amount)                                 AS amount,
                   count(*) FILTER (WHERE rate_type = 'arm')        AS arm_loans,
                   sum(loan_amount) FILTER (WHERE rate_type = 'arm') AS arm_amount
            FROM stg_hmda GROUP BY ALL
        ),
        call_reports AS (
            SELECT rssd_id, year(report_date) AS activity_year,
                   max(report_date) AS report_date, arg_max(name, report_date) AS name
            FROM dim_bank GROUP BY ALL
        ),
        lender_years AS (SELECT DISTINCT activity_year FROM dim_hmda_lender)
        SELECT
            coalesce(l.activity_year, x.activity_year)      AS activity_year,
            coalesce(l.lei, x.lei)                          AS lei,
            l.name,
            l.lender_type,
            l.agency_code,
            l.institution_type,
            l.respondent_rssd,
            CASE
                WHEN l.lei IS NULL AND y.activity_year IS NULL THEN 'no_lender_file_for_year'
                WHEN l.lei IS NULL                  THEN 'not_in_lender_file'
                WHEN l.respondent_rssd IS NULL      THEN 'no_rssd'
                WHEN c.rssd_id IS NOT NULL          THEN 'matched'
                ELSE 'rssd_not_a_call_report_filer'
            END                                             AS match_status,
            c.name                                          AS call_report_name,
            c.report_date                                   AS call_report_date,
            coalesce(x.loans, 0)                            AS loans,
            coalesce(x.amount, 0)                           AS amount,
            coalesce(x.arm_loans, 0)                        AS arm_loans,
            coalesce(x.arm_amount, 0)                       AS arm_amount
        FROM dim_hmda_lender l
        FULL JOIN lar x ON x.lei = l.lei AND x.activity_year = l.activity_year
        LEFT JOIN lender_years y ON y.activity_year = x.activity_year
        LEFT JOIN call_reports c
               ON c.rssd_id = l.respondent_rssd AND c.activity_year = l.activity_year
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


def create_views(con: duckdb.DuckDBPyConnection, params: dict[str, str] | None = None) -> list[str]:
    """(Re)create every view whose inputs exist; returns the names created."""
    params = {"unmapped_segment": "unmapped", **(params or {})}
    available = {name for name, _ in relations(con)}
    created = []
    for name, (needs, template) in VIEWS.items():
        placeholders = set(re.findall(r"\{(\w+)\}", template))
        if not needs <= available or not placeholders <= params.keys():
            continue
        sql = template
        for key in placeholders:
            sql = sql.replace("{" + key + "}", params[key].replace("'", "''"))
        con.execute(f"CREATE OR REPLACE VIEW {name} AS {sql}")
        available.add(name)
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
