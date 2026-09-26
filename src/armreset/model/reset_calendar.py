"""HMDA first-reset calendar (PLAN.md §7.2): the calendar year of each ARM's first rate reset,
the balance that reaches it, and which reset years the loaded origination years cover.

Public HMDA gives the origination year but not the date, so origination dates are assumed to
be spread evenly over the year. The first reset comes ``intro_m`` months after origination, so
each loan's reset falls in at most two calendar years, with weights that sum to 1: every loan
counts once. Each scenario holds every loan once, so never add scenarios together.

Survival is split at ``model.as_of``. Up to that date each origination year prepays at its
``history_cpr``, the same in every scenario; after it, at the scenario's CPR. So a reset that
has already happened is the same in every scenario.
"""

from __future__ import annotations

import calendar
import logging
import math
from datetime import date

import duckdb
import polars as pl

from armreset.db import relations
from armreset.model.amortization import sched_factor_sql, survival_between_sql
from armreset.segments import Segments
from armreset.settings import FIRST_HMDA_YEAR, ModelConfig

log = logging.getLogger(__name__)

# A split share below this is floating-point noise, not part of a reset.
MIN_WEIGHT = 1e-9

# intro_m buckets: the QA histogram's rows, and the groups whose median fills a missing rate.
INTRO_BUCKET_SQL = """
    CASE WHEN intro_m < 12 THEN '01-11' WHEN intro_m = 12 THEN '12'
         WHEN intro_m < 36 THEN '13-35' WHEN intro_m = 36 THEN '36'
         WHEN intro_m < 60 THEN '37-59' WHEN intro_m = 60 THEN '60'
         WHEN intro_m < 84 THEN '61-83' WHEN intro_m = 84 THEN '84'
         WHEN intro_m < 120 THEN '85-119' WHEN intro_m = 120 THEN '120'
         WHEN intro_m < 180 THEN '121-179' WHEN intro_m = 180 THEN '180'
         ELSE '181+' END"""

# Reported values outside these ranges are reporting errors, and they are filled like missing
# ones (docs/verification.md, phase 4). In 2018-2025 no ARM reports a rate between 17.5% and
# 29.25%; the 19 above that include 362,500 and 5,125, i.e. 3.625% and 5.125% keyed without
# the decimal point. Terms stop at 600 months except for 116 loans, 100 of them with 999.
MAX_RATE_PCT = 20.0
MAX_TERM_M = 600

# PLAN.md §7.2 step 3: a missing interest rate takes the median for the same activity year,
# intro_m bucket and conforming status. If that group has no reported rate, the next, wider
# group is used. Loan terms are filled the same way.
FILL_GROUPS = (
    ("activity_year", "intro_bucket", "conforming"),
    ("activity_year", "intro_bucket"),
    ("activity_year",),
)
# Where a filled value came from, as rate_source and term_source record it.
FILL_SOURCES = tuple(f"median: {', '.join(keys)}" for keys in FILL_GROUPS)


def reset_year_weights(orig_year: int, intro_m: int) -> dict[int, float]:
    """Origination date ~ Uniform(orig_year). First reset = origination + intro_m months.
    Returns {calendar_year: weight}; weights sum to 1."""
    start = orig_year + intro_m / 12.0  # reset time if originated Jan 1
    end = start + 1.0  # reset time if originated Dec 31
    out, y = {}, math.floor(start)
    while y < end:
        overlap = min(end, y + 1) - max(start, y)
        if overlap > MIN_WEIGHT:
            out[y] = overlap
        y += 1
    return out


def reset_year_spans(orig_year: int, intro_m: int) -> dict[int, tuple[float, float]]:
    """The reset times, in fractional years, that fall in each calendar year:
    {calendar_year: (first, last)}. Each span's length is its :func:`reset_year_weights`
    weight."""
    start = orig_year + intro_m / 12.0
    return {
        y: (max(start, y), min(start + 1.0, y + 1)) for y in reset_year_weights(orig_year, intro_m)
    }


def year_fraction(d: date) -> float:
    """The end of day ``d`` as a fractional year: 2026-06-30 is 2026 + 181/365."""
    days = 366 if calendar.isleap(d.year) else 365
    return d.year + d.timetuple().tm_yday / days


def split_ctes(source: str, time: str) -> str:
    """CTEs ``timed`` and ``split``: :func:`reset_year_spans` for every row of ``source``.

    ``time`` is the reset time in years for a loan originated on January 1. Each row becomes
    one row per calendar year its reset can fall in, with ``reset_year``, weight ``w`` and the
    span of reset times in that year, ``t_lo`` to ``t_hi``."""
    return f"""
    timed AS (SELECT *, {time} AS t0 FROM {source}),
    split AS (
        SELECT * FROM (
            SELECT *, CAST(floor(t0) AS INTEGER)     AS reset_year, (floor(t0) + 1 - t0)     AS w,
                   t0 AS t_lo, floor(t0) + 1 AS t_hi
            FROM timed
            UNION ALL
            SELECT *, CAST(floor(t0) AS INTEGER) + 1 AS reset_year, 1 - (floor(t0) + 1 - t0) AS w,
                   floor(t0) + 1 AS t_lo, t0 + 1 AS t_hi
            FROM timed
        ) WHERE w > {MIN_WEIGHT}
    )"""


def scenario_table(model: ModelConfig) -> pl.DataFrame:
    """``dim_scenario``: the CPR scenarios in config.yaml, applied from ``model.as_of`` to each
    reset. Assumptions, not estimates."""
    return pl.DataFrame(
        {"scenario": list(model.scenarios), "cpr": list(model.scenarios.values())},
        schema={"scenario": pl.Utf8, "cpr": pl.Float64},
    )


def history_table(model: ModelConfig) -> pl.DataFrame:
    """``dim_history_cpr``: the CPR each origination year has shown up to ``model.as_of``, the
    same in every scenario. Assumptions until measured."""
    return pl.DataFrame(
        {"orig_year": list(model.history_cpr), "cpr": list(model.history_cpr.values())},
        schema={"orig_year": pl.Int32, "cpr": pl.Float64},
    )


def _fill_sql(column: str, stat: str, prefix: str) -> tuple[list[str], list[str], str, str]:
    """CTEs and joins for filling ``column`` from FILL_GROUPS, the filled value and its source.
    ``stat`` is the aggregate, with ``{}`` where the column goes."""
    ctes, joins, values, sources = [], [], [], []
    for i, (keys, label) in enumerate(zip(FILL_GROUPS, FILL_SOURCES, strict=True)):
        name = f"{prefix}_{i}"
        ctes.append(
            f"{name} AS (SELECT {', '.join(keys)}, {stat.format(column)} AS {name} "
            f"FROM arm WHERE {column} IS NOT NULL GROUP BY ALL)"
        )
        on = " AND ".join(f"{name}.{k} = a.{k}" for k in keys)
        joins.append(f"LEFT JOIN {name} ON {on}")
        values.append(f"{name}.{name}")
        sources.append(f"WHEN {name}.{name} IS NOT NULL THEN '{label}'")
    value = f"coalesce(a.{column}, {', '.join(values)})"
    source = f"CASE WHEN a.{column} IS NOT NULL THEN 'reported' {' '.join(sources)} ELSE 'none' END"
    return ctes, joins, value, source


def _loans_sql(segments: Segments, with_lenders: bool) -> str:
    """The temp table ``reset_loans``: one row per ARM, with its segments and filled inputs."""
    if with_lenders:
        lender_join = (
            "LEFT JOIN dim_hmda_lender l ON l.activity_year = h.activity_year AND l.lei = h.lei"
        )
        lender_type = f"coalesce(l.lender_type, '{segments.unknown_lender_type}')"
    else:
        lender_join, lender_type = "", f"'{segments.unknown_lender_type}'"
    rate_ctes, rate_joins, rate_value, rate_source = _fill_sql(
        "interest_rate", "median({})", "rate"
    )
    # quantile_disc keeps a filled term one that some loan actually has.
    term_ctes, term_joins, term_value, term_source = _fill_sql(
        "loan_term_m", "quantile_disc({}, 0.5)", "term"
    )
    return f"""
    CREATE OR REPLACE TEMP TABLE reset_loans AS
    WITH arm AS (
        SELECT
            h.activity_year,
            h.lei,
            h.state_code,
            h.conforming_loan_limit                          AS conforming,
            h.occupancy_type,
            h.loan_amount,
            h.intro_m,
            coalesce(h.is_io, false)                         AS is_io,
            h.interest_rate                                  AS reported_rate,
            h.loan_term_m                                    AS reported_term,
            CASE WHEN h.interest_rate BETWEEN 0 AND {MAX_RATE_PCT}
                 THEN h.interest_rate END                    AS interest_rate,
            CASE WHEN h.loan_term_m BETWEEN 1 AND {MAX_TERM_M}
                 THEN h.loan_term_m END                      AS loan_term_m,
            coalesce(p.holder_segment, '{segments.unmapped}') AS holder_segment,
            {lender_type}                                    AS lender_type,
            {INTRO_BUCKET_SQL}                               AS intro_bucket
        FROM stg_hmda h
        LEFT JOIN dim_purchaser_segment p USING (purchaser_type)
        {lender_join}
        WHERE h.rate_type = 'arm'
    ),
    {", ".join(rate_ctes + term_ctes)}
    SELECT
        a.*,
        {rate_value}  AS rate_used,
        {rate_source} AS rate_source,
        {term_value}  AS term_used,
        {term_source} AS term_source
    FROM arm a
    {" ".join(rate_joins + term_joins)}
    """


# The filled-rate counts (PLAN.md §11), with the filled terms and the interest-only loans the
# IO assumption applies to: one row per activity year, intro_m bucket and conforming status.
INPUTS_SQL = """
CREATE OR REPLACE TABLE qa_reset_inputs AS
SELECT
    activity_year,
    intro_bucket,
    conforming,
    count(*)                                                        AS arm_loans,
    sum(loan_amount)                                                AS arm_amount,
    count(*) FILTER (WHERE rate_source = 'reported')                AS rate_reported,
    count(*) FILTER (WHERE rate_source <> 'reported')               AS rate_filled,
    count(*) FILTER (WHERE reported_rate IS NOT NULL
                       AND interest_rate IS NULL)                   AS rate_out_of_range,
    count(*) FILTER (WHERE rate_used IS NULL)                       AS rate_missing,
    any_value(rate_source) FILTER (WHERE rate_source <> 'reported') AS rate_fill_source,
    any_value(rate_used) FILTER (WHERE rate_source <> 'reported')   AS rate_fill_value,
    median(interest_rate)                                           AS median_reported_rate,
    count(*) FILTER (WHERE term_source <> 'reported')               AS term_filled,
    count(*) FILTER (WHERE reported_term IS NOT NULL
                       AND loan_term_m IS NULL)                     AS term_out_of_range,
    count(*) FILTER (WHERE term_used IS NULL)                       AS term_missing,
    count(*) FILTER (WHERE is_io)                                   AS io_loans,
    coalesce(sum(loan_amount) FILTER (WHERE is_io), 0)              AS io_amount
FROM reset_loans
GROUP BY ALL
ORDER BY ALL
"""


def _fact_sql(model: ModelConfig) -> str:
    """``fact_reset_calendar``: first resets, plus later resets when the toggle is on."""
    # Interest-only loans don't amortize before the first reset (model.io_assumption); after
    # it, they amortize over the rest of the term. Shifting the schedule by the intro period
    # gives both: A = 1 at the first reset.
    io_shift = "(CASE WHEN x.is_io THEN x.intro_m ELSE 0 END)"
    factor = sched_factor_sql("x.rate_used", f"x.term_used - {io_shift}", f"x.k - {io_shift}")
    # History CPR up to as_of (the scenario's where the year has none), then the scenario's.
    # The integral over each year's span of reset times already carries the weight w.
    survival = survival_between_sql(
        "coalesce(h.cpr, s.cpr)",
        "s.cpr",
        "x.k",
        "x.t_lo",
        "x.t_hi",
        repr(year_fraction(model.as_of)),
    )
    subsequent = ""
    if model.subsequent_resets.enabled:
        # PLAN.md §7.2 step 6: after the first reset, every frequency_months until maturity,
        # kept to resets that can reach the calendar years. Each row is a reset, not a loan.
        f = model.subsequent_resets.frequency_months
        first, last = model.calendar_years
        most = ((last + 1 - FIRST_HMDA_YEAR) * 12) // f + 1
        k = f"l.intro_m + r.j * {f}"
        subsequent = f"""
        UNION ALL
        SELECT l.*, 'subsequent' AS reset_kind, {k} AS k
        FROM reset_loans l
        JOIN range(1, {most + 1}) r(j)
          ON {k} < l.term_used
         AND l.activity_year + ({k}) / 12.0 < {last + 1}
         AND l.activity_year + ({k}) / 12.0 > {first - 1}"""
    return f"""
    CREATE OR REPLACE TABLE fact_reset_calendar AS
    WITH resets AS (
        SELECT *, 'first' AS reset_kind, intro_m AS k FROM reset_loans{subsequent}
    ),
    {split_ctes("resets", "activity_year + k / 12.0")}
    SELECT
        s.scenario,
        x.reset_kind,
        x.reset_year,
        x.activity_year                      AS orig_year,
        x.intro_m,
        x.holder_segment,
        x.lender_type,
        x.conforming,
        x.occupancy_type,
        x.state_code,
        x.lei,
        x.is_io,
        x.rate_source <> 'reported'          AS rate_filled,
        x.term_source <> 'reported'          AS term_filled,
        sum(x.w)                             AS w_loans,
        sum(x.w * x.loan_amount)             AS orig_amount,
        sum(x.loan_amount * ({factor}) * {survival})
                                             AS bal_at_reset
    FROM split x
    CROSS JOIN dim_scenario s
    LEFT JOIN dim_history_cpr h ON h.orig_year = x.activity_year
    GROUP BY ALL
    """


def build_reset_calendar(
    con: duckdb.DuckDBPyConnection, model: ModelConfig, segments: Segments
) -> dict[str, int]:
    """Write ``fact_reset_calendar`` and ``qa_reset_inputs`` from ``stg_hmda``; returns their
    row counts. Needs ``stg_hmda``, ``dim_purchaser_segment``, ``dim_scenario`` and
    ``dim_history_cpr``. Lender types come from ``dim_hmda_lender``, or are unknown without
    it."""
    with_lenders = "dim_hmda_lender" in {name for name, _ in relations(con)}
    con.execute(_loans_sql(segments, with_lenders))
    # History runs from origination to as_of, so every loan must be originated by then.
    last = con.execute("SELECT max(activity_year) FROM reset_loans").fetchone()[0]
    if last is not None and year_fraction(model.as_of) < last + 1:
        raise ValueError(
            f"model.as_of ({model.as_of}) is before the end of {last}, the latest origination "
            f"year loaded; set it to {last}-12-31 or later"
        )
    no_rate, no_term = con.execute(
        "SELECT count(*) FILTER (WHERE rate_used IS NULL), "
        "count(*) FILTER (WHERE term_used IS NULL) FROM reset_loans"
    ).fetchone()
    if no_rate or no_term:
        log.warning(
            "%d ARMs have no rate and %d no term, even from a median; their balances at reset "
            "are unknown (see qa_reset_inputs)",
            no_rate,
            no_term,
        )
    con.execute(INPUTS_SQL)
    con.execute(_fact_sql(model))
    con.execute("DROP TABLE reset_loans")
    # A NaN or infinite balance would make every total it joins meaningless; stop instead.
    bad = con.execute(
        "SELECT count(*) FROM fact_reset_calendar WHERE NOT isfinite(bal_at_reset)"
    ).fetchone()[0]
    if bad:
        raise ValueError(f"fact_reset_calendar: {bad} rows have a balance that isn't a number")
    return {
        name: con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        for name in ("fact_reset_calendar", "qa_reset_inputs")
    }
