"""Warehouse access for the dashboard. The app only reads views and small aggregates that
`armtool build` precomputed (PLAN.md §8).

Every query opens a read-only connection and closes it, so a build can still take the file.
Results are cached on the SQL, its parameters and the warehouse file's modified time, so a
rebuild shows up without restarting the app.
"""

from __future__ import annotations

from collections.abc import Sequence

import duckdb
import polars as pl
import streamlit as st

from armreset.model.reset_calendar import INTRO_BUCKET_SQL
from armreset.segments import Segments
from armreset.settings import Settings, load_settings

# (label, lower bound, upper bound) in USD of total assets, for the bank filters.
ASSET_BANDS = [
    ("All banks", None, None),
    ("Under $1bn", None, 1e9),
    ("$1bn to $10bn", 1e9, 1e10),
    ("$10bn to $100bn", 1e10, 1e11),
    ("$100bn and over", 1e11, None),
]
# The chart's five bands: the two longest buckets combine, because six steps of one hue
# can't all be told apart (see ui.py). Tables keep all six.
BUCKET_BANDS = [
    ("3 months or less", "b_le_3m"),
    ("3 to 12 months", "b_3_12m"),
    ("1 to 3 years", "b_1_3y"),
    ("3 to 5 years", "b_3_5y"),
    ("Over 5 years", "b_5_15y + b_gt_15y"),
]


def settings() -> Settings:
    return load_settings()


def ready() -> bool:
    """False, with a note on the page, until `armtool build` has written the warehouse."""
    if settings().warehouse_path.exists():
        return True
    st.info("No warehouse yet. Run `armtool build` (see the README), then reload this page.")
    return False


@st.cache_data(show_spinner=False)
def _query(sql: str, params: tuple, mtime: float, path: str) -> pl.DataFrame:
    con = duckdb.connect(path, read_only=True)
    try:
        return con.execute(sql, list(params)).pl()
    finally:
        con.close()


def query(sql: str, *params: object) -> pl.DataFrame:
    path = settings().warehouse_path
    return _query(sql, _hashable(params), path.stat().st_mtime, str(path))


def _hashable(params: Sequence[object]) -> tuple:
    return tuple(tuple(p) if isinstance(p, list) else p for p in params)


# v_bank_hmda_link.match_status, for display.
MATCH_STATUS = {
    "matched": "Files a Call Report",
    "rssd_not_a_call_report_filer": "RSSD files no Call Report",
    "no_rssd": "No RSSD",
    "not_in_lender_file": "Not in the Lender File",
    "no_lender_file_for_year": "No Lender File for the year",
}


def labels(field: str) -> dict[str, str]:
    """Readable labels for a field's codes (dim_code_label)."""
    df = query("SELECT code, label FROM dim_code_label WHERE field = ?", field)
    return dict(zip(df["code"].to_list(), df["label"].to_list(), strict=True))


def has(*relations: str) -> bool:
    names = set(query("SELECT table_name FROM information_schema.tables")["table_name"])
    return set(relations) <= names


@st.cache_data(show_spinner=False)
def segments(root: str) -> Segments:
    return Segments.load(settings().config_file("segments.yaml"))


def holder_order() -> list[str]:
    """Holder segments in config order, from the originator's own book to unknown holders."""
    s = segments(str(settings().root))
    return [*s.holder, s.unmapped]


def in_list(column: str, values: Sequence[str]) -> tuple[str, list]:
    """A WHERE fragment and its parameter; no values means no filter."""
    if not values:
        return "", []
    return f" AND list_contains(?, {column})", [list(values)]


# -- reset calendar ------------------------------------------------------------------


def scenarios() -> pl.DataFrame:
    return query("SELECT scenario, cpr FROM dim_scenario ORDER BY cpr, scenario")


def history_cprs() -> pl.DataFrame:
    return query("SELECT orig_year, cpr FROM dim_history_cpr ORDER BY orig_year")


def calendar_options() -> dict[str, list[str]]:
    """The values each calendar filter can take."""
    out = {}
    for key, column in [
        ("conforming", "conforming_label"),
        ("occupancy", "occupancy_label"),
        ("lender_type", "lender_type_label"),
        ("state", "state_code"),
    ]:
        df = query(f"SELECT DISTINCT {column} AS v FROM v_reset_calendar ORDER BY 1")
        out[key] = [v for v in df["v"].to_list() if v is not None]
    return out


def calendar(
    scenario: str, first: int, last: int, filters: dict[str, Sequence[str]]
) -> pl.DataFrame:
    """First resets by reset year and holder segment, for one scenario and the filters."""
    where, params = "", []
    for column, values in [
        ("conforming_label", filters.get("conforming", [])),
        ("occupancy_label", filters.get("occupancy", [])),
        ("lender_type_label", filters.get("lender_type", [])),
        ("state_code", filters.get("state", [])),
    ]:
        clause, value = in_list(column, values)
        where += clause
        params += value
    return query(
        f"""
        SELECT reset_year, holder_segment, any_value(holder_label) AS holder_label,
               sum(bal_at_reset)                          AS bal_at_reset,
               sum(orig_amount)                           AS orig_amount,
               sum(w_loans)                               AS w_loans,
               coalesce(sum(bal_at_reset) FILTER (WHERE is_io), 0) AS bal_io,
               coalesce(sum(orig_amount) FILTER (WHERE is_io), 0)  AS orig_io
        FROM v_reset_calendar
        WHERE scenario = ? AND reset_kind = 'first' AND reset_year BETWEEN ? AND ? {where}
        GROUP BY reset_year, holder_segment
        ORDER BY reset_year
        """,
        scenario,
        first,
        last,
        *params,
    )


def exempt_shares() -> pl.DataFrame:
    return query(
        """
        SELECT activity_year AS year,
               sum(loans) FILTER (WHERE rate_type = 'unknown') / sum(loans)
                   AS "exempt share (loans)",
               sum(amount) FILTER (WHERE rate_type = 'unknown') / sum(amount)
                   AS "exempt share ($)"
        FROM v_hmda_orig_summary GROUP BY 1 ORDER BY 1
        """
    )


# -- Call Reports ---------------------------------------------------------------------


def bank_filters(band: str, states: Sequence[str], min_first_lien_mm: float) -> tuple[str, list]:
    """WHERE fragments on fact_cdr_repricing (f) and dim_bank (d) for the bank filters."""
    where, params = "", []
    lo, hi = next((lo, hi) for label, lo, hi in ASSET_BANDS if label == band)
    if lo is not None:
        where += " AND f.total_assets >= ?"
        params.append(lo)
    if hi is not None:
        where += " AND f.total_assets < ?"
        params.append(hi)
    clause, value = in_list("d.state", states)
    where += clause
    params += value
    if min_first_lien_mm:
        where += " AND f.first_lien_total >= ?"
        params.append(min_first_lien_mm * 1e6)
    return where, params


def industry(band: str, states: Sequence[str], min_first_lien_mm: float) -> pl.DataFrame:
    """The repricing buckets summed over the banks the filters keep, by quarter."""
    where, params = bank_filters(band, states, min_first_lien_mm)
    bands = ",\n".join(f'sum({expr}) AS "{label}"' for label, expr in BUCKET_BANDS)
    return query(
        f"""
        SELECT f.report_date,
               count(*)                         AS banks,
               {bands},
               sum(f.first_lien_total)          AS first_lien_total,
               sum(f.b_le_3m + f.b_3_12m)       AS within_12m,
               sum(f.b_le_3m + f.b_3_12m) / nullif(sum(f.first_lien_total)
                   FILTER (WHERE f.b_le_3m IS NOT NULL AND f.b_3_12m IS NOT NULL), 0)
                                                AS within_12m_share
        FROM fact_cdr_repricing f
        JOIN dim_bank d USING (rssd_id, report_date)
        WHERE true {where}
        GROUP BY 1 ORDER BY 1
        """,
        *params,
    )


def banks_latest(band: str, states: Sequence[str], min_first_lien_mm: float) -> pl.DataFrame:
    where, params = bank_filters(band, states, min_first_lien_mm)
    return query(
        f"""
        SELECT f.* FROM v_cdr_bank_latest f
        JOIN dim_bank d USING (rssd_id, report_date)
        WHERE true {where}
        ORDER BY f.within_12m DESC NULLS LAST
        """,
        *params,
    )


def bank_choices() -> pl.DataFrame:
    """Every bank that ever filed, with its latest name, and its latest first-lien book."""
    return query(
        """
        SELECT d.rssd_id, arg_max(d.name, d.report_date) AS name,
               arg_max(d.state, d.report_date)           AS state,
               max(d.report_date)                        AS last_report,
               arg_max(f.first_lien_total, f.report_date) AS first_lien_total
        FROM dim_bank d JOIN fact_cdr_repricing f USING (rssd_id, report_date)
        GROUP BY 1 ORDER BY first_lien_total DESC NULLS LAST
        """
    )


def bank_history(rssd_id: int) -> pl.DataFrame:
    return query(
        """
        SELECT f.*, d.name, d.city, d.state, d.form, d.fdic_cert
        FROM fact_cdr_repricing f JOIN dim_bank d USING (rssd_id, report_date)
        WHERE f.rssd_id = ? ORDER BY f.report_date
        """,
        rssd_id,
    )


def bank_flags(rssd_id: int) -> pl.DataFrame:
    return query(
        "SELECT report_date, flag, detail FROM qa_cdr_flags WHERE rssd_id = ? "
        "ORDER BY report_date DESC, flag",
        rssd_id,
    )


# -- HMDA -----------------------------------------------------------------------------


def bank_lenders(rssd_id: int) -> pl.DataFrame:
    """The HMDA lenders whose RSSD is this bank's, by year (v_bank_hmda_link)."""
    return query(
        """
        SELECT activity_year, lei, name, lender_type, match_status,
               loans, arm_loans, arm_amount
        FROM v_bank_hmda_link WHERE respondent_rssd = ?
        ORDER BY activity_year, arm_amount DESC
        """,
        rssd_id,
    )


def arm_mix(leis: Sequence[str]) -> pl.DataFrame:
    """ARM originations by origination year and months to first reset, for some lenders.
    From the calendar's first resets in one scenario: every ARM counts once there."""
    return query(
        f"""
        SELECT orig_year, {INTRO_BUCKET_SQL} AS intro_bucket, min(intro_m) AS sort_key,
               sum(w_loans) AS arm_loans, sum(orig_amount) AS arm_amount
        FROM fact_reset_calendar
        WHERE reset_kind = 'first' AND list_contains(?, lei)
          AND scenario = (SELECT min(scenario) FROM dim_scenario)
        GROUP BY ALL ORDER BY orig_year, sort_key
        """,
        list(leis),
    )


def crosscheck(scenario: str) -> pl.DataFrame:
    return query(
        "SELECT * FROM v_bank_reset_crosscheck WHERE scenario = ? ORDER BY within_12m DESC",
        scenario,
    )


def hmda_years() -> pl.DataFrame:
    """Originations by year: all loans, ARMs, jumbo ARMs and exempt rows."""
    return query(
        """
        SELECT activity_year AS year,
               sum(loans)::BIGINT                                          AS loans,
               sum(amount)                                                 AS amount,
               (sum(loans) FILTER (WHERE rate_type = 'arm'))::BIGINT       AS arm_loans,
               sum(amount) FILTER (WHERE rate_type = 'arm')                AS arm_amount,
               sum(amount) FILTER (WHERE rate_type = 'arm' AND conforming = 'NC')
                                                                           AS jumbo_arm_amount,
               (sum(loans) FILTER (WHERE rate_type = 'unknown'))::BIGINT   AS exempt_loans,
               sum(amount) FILTER (WHERE rate_type = 'unknown')            AS exempt_amount
        FROM v_hmda_orig_summary GROUP BY 1 ORDER BY 1
        """
    )


def intro_mix() -> pl.DataFrame:
    """Share of each year's ARM loans by months to first reset (qa_reset_inputs buckets)."""
    return query(
        """
        SELECT activity_year AS year, intro_bucket,
               sum(arm_loans) / sum(sum(arm_loans)) OVER (PARTITION BY activity_year) AS share
        FROM qa_reset_inputs GROUP BY 1, 2 ORDER BY 1, 2
        """
    )


def top_originators(year: int, limit: int = 25) -> pl.DataFrame:
    return query(
        """
        SELECT coalesce(name, lei) AS lender, lender_type, match_status,
               arm_loans, arm_amount, arm_amount / sum(arm_amount) OVER () AS share, loans
        FROM v_bank_hmda_link WHERE activity_year = ? AND arm_loans > 0
        ORDER BY arm_amount DESC LIMIT ?
        """,
        year,
        limit,
    )
