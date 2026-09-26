"""``armtool validate`` (PLAN.md §11): writes ``data/qa_report.md``.

The report is logged for review, not asserted: it shows counts, totals and flags so a person
can judge whether a quarter looks right. Nothing here fails a build.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import polars as pl

from armreset.db import connect, relations
from armreset.ingest.cdr import MdrmSpec
from armreset.model.repricing import HIGH_NONACCRUAL_SHARE, ROUNDING_TOLERANCE_USD
from armreset.model.reset_calendar import FILL_SOURCES, INTRO_BUCKET_SQL
from armreset.segments import Segments
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
        if column == "year":
            return str(value)
        if isinstance(value, Decimal):  # DuckDB returns sums of integers as exact decimals
            value = int(value) if value == value.to_integral_value() else float(value)
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


def hmda_year_stats(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    return con.sql(
        """
        SELECT activity_year                                     AS year,
               count(*)                                          AS loans,
               sum(loan_amount) / 1e9                            AS "amount $bn",
               count(*) FILTER (WHERE rate_type = 'arm')         AS "ARM loans",
               avg((rate_type = 'arm')::INT)                     AS "ARM share (loans)",
               sum(loan_amount) FILTER (WHERE rate_type = 'arm')
                   / sum(loan_amount)                            AS "ARM share ($)",
               avg((rate_type = 'unknown')::INT)                 AS "exempt share (loans)",
               sum(loan_amount) FILTER (WHERE rate_type = 'unknown')
                   / sum(loan_amount)                            AS "exempt share ($)",
               count(*) FILTER (WHERE rate_type = 'arm' AND interest_rate IS NULL)
                                                                 AS "ARMs without a rate"
        FROM stg_hmda GROUP BY ALL ORDER BY year
        """
    ).pl()


def hmda_intro_histogram(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """Share of each year's ARM loans by months to first reset (intro_m)."""
    long = con.sql(
        f"""
        SELECT {INTRO_BUCKET_SQL} AS "intro_m (months)", activity_year::VARCHAR AS year,
               count(*) / sum(count(*)) OVER (PARTITION BY activity_year) AS share,
               min(intro_m) AS sort_key
        FROM stg_hmda WHERE rate_type = 'arm' GROUP BY 1, activity_year
        """
    ).pl()
    order = long.group_by("intro_m (months)").agg(pl.col("sort_key").min())
    wide = long.pivot(on="year", index="intro_m (months)", values="share")
    wide = wide.rename({c: f"{c} share" for c in wide.columns if c != "intro_m (months)"})
    return wide.join(order, on="intro_m (months)").sort("sort_key").drop("sort_key").fill_null(0.0)


def hmda_holder_segments(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    return con.sql(
        """
        SELECT activity_year AS year, holder_segment AS "holder segment",
               sum(loans) FILTER (WHERE rate_type = 'arm')        AS "ARM loans",
               sum(amount) FILTER (WHERE rate_type = 'arm') / 1e9 AS "ARM $bn",
               sum(loans)                                         AS "all loans"
        FROM v_hmda_orig_summary GROUP BY ALL ORDER BY year, "ARM $bn" DESC NULLS LAST
        """
    ).pl()


def hmda_rssd_match(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    return con.sql(
        """
        SELECT activity_year AS year,
               coalesce(lender_type, '(not in Lender File)')        AS "lender type",
               count(*)                                              AS lenders,
               count(*) FILTER (WHERE match_status = 'matched')      AS "linked to a Call Report",
               avg((match_status = 'matched')::INT)                  AS "linked share",
               sum(loans)                                            AS loans,
               sum(arm_amount) / 1e9                                 AS "ARM $bn"
        FROM v_bank_hmda_link WHERE loans > 0 GROUP BY ALL ORDER BY year, loans DESC
        """
    ).pl()


def lender_types_by_year(con: duckdb.DuckDBPyConnection, filer_type: str) -> pl.DataFrame:
    """Lenders per year by ``lender_type``, with two checks against the Call Report link:
    lenders typed ``filer_type`` only because their RSSD files a Call Report (their institution
    code says otherwise), and lenders whose code says ``filer_type`` but whose RSSD filed no
    Call Report that year."""
    df = con.execute(
        """
        SELECT l.activity_year AS year, l.lender_type,
               coalesce(l.in_call_reports AND t.lender_type IS DISTINCT FROM ?, false)
                   AS by_link,
               coalesce(NOT l.in_call_reports AND t.lender_type = ?, false) AS no_filing
        FROM dim_hmda_lender l LEFT JOIN dim_institution_type t USING (institution_type)
        """,
        [filer_type, filer_type],
    ).pl()
    counts = df.pivot(on="lender_type", index="year", values="by_link", aggregate_function="len")
    checks = df.group_by("year").agg(
        pl.col("by_link").sum().alias(f"{filer_type} by Call Report, not by code"),
        pl.col("no_filing").sum().alias(f"{filer_type} by code, no Call Report"),
    )
    types = sorted(c for c in counts.columns if c != "year")
    return (
        counts.fill_null(0)
        .join(checks, on="year")
        .select("year", *types, *checks.columns[1:])
        .sort("year")
    )


def lender_file_coverage(settings: Settings, con: duckdb.DuckDBPyConnection) -> pl.DataFrame | None:
    """The Lender File checked against every recorded CFPB list of lenders for the year."""
    from armreset.ingest.panel import cfpb_lender_lists
    from armreset.manifest import Manifest

    lists = cfpb_lender_lists(Manifest.for_settings(settings))
    if lists is None:
        return None
    dim = con.sql(
        "SELECT activity_year::INTEGER AS activity_year, lei, respondent_rssd AS file_rssd, "
        "agency_code AS file_agency, true AS in_file FROM dim_hmda_lender"
    ).pl()
    joined = lists.join(dim, on=["activity_year", "lei"], how="left")
    panel = pl.col("cfpb_list") == "panel"
    both = pl.col("in_file").fill_null(False)
    return (
        joined.group_by("activity_year", "cfpb_list")
        .agg(
            pl.len().alias("lenders"),
            both.sum().alias("in Lender File"),
            pl.when(panel.first())
            .then((both & (pl.col("agency_code") == pl.col("file_agency"))).sum())
            .alias("agency code agrees"),
            # No RSSD in either file counts as agreeing.
            pl.when(panel.first())
            .then(
                (
                    both
                    & (pl.col("respondent_rssd").fill_null(0) == pl.col("file_rssd").fill_null(0))
                ).sum()
            )
            .alias("RSSD agrees"),
        )
        .rename({"activity_year": "year", "cfpb_list": "CFPB list"})
        .sort("year")
    )


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _percent(cpr: float) -> str:
    return f"{cpr * 100:g}%"


def reset_calendar_by_scenario(
    con: duckdb.DuckDBPyConnection, first: int, last: int, as_of_year: int
) -> pl.DataFrame:
    """First resets by reset year: the weighted ARM count and original amount, which every
    scenario shares, then the balance at reset under each CPR scenario. The current year is
    marked with an asterisk."""
    scenarios = con.sql("SELECT scenario, cpr FROM dim_scenario ORDER BY cpr, scenario").fetchall()
    one = _sql_str(scenarios[0][0])  # every scenario holds every loan once
    balances = [
        f"sum(bal_at_reset) FILTER (WHERE scenario = {_sql_str(name)}) / 1e9 "
        f'AS "{name}, {_percent(cpr)} CPR: balance $bn"'
        for name, cpr in scenarios
    ]
    return con.execute(
        f"""
        SELECT CASE WHEN reset_year = ? THEN reset_year::VARCHAR || ' *'
                    ELSE reset_year::VARCHAR END                         AS "reset year",
               round(sum(w_loans) FILTER (WHERE scenario = {one}))::BIGINT AS "ARM loans",
               sum(orig_amount) FILTER (WHERE scenario = {one}) / 1e9     AS "original $bn",
               {", ".join(balances)}
        FROM fact_reset_calendar
        WHERE reset_kind = 'first' AND reset_year BETWEEN ? AND ?
        GROUP BY reset_year ORDER BY reset_year
        """,
        [as_of_year, first, last],
    ).pl()


def reset_calendar_by_holder(
    con: duckdb.DuckDBPyConnection, scenario: str, first: int, last: int
) -> pl.DataFrame:
    """Balance at reset ($bn) by reset year and holder segment, for one scenario."""
    long = con.execute(
        """
        SELECT reset_year::VARCHAR AS "reset year", holder_segment,
               sum(bal_at_reset) / 1e9 AS bal, min(reset_year) AS sort_key
        FROM fact_reset_calendar
        WHERE reset_kind = 'first' AND scenario = ? AND reset_year BETWEEN ? AND ?
        GROUP BY ALL
        """,
        [scenario, first, last],
    ).pl()
    order = long.group_by("holder_segment").agg(pl.col("bal").sum()).sort("bal", descending=True)
    wide = long.pivot(on="holder_segment", index=["reset year", "sort_key"], values="bal")
    return wide.sort("sort_key").select("reset year", *order["holder_segment"]).fill_null(0.0)


def _intro_label(intro_m: int) -> str:
    if intro_m == 1:
        return "1 month"
    return f"{intro_m} ({intro_m // 12} yr)" if intro_m % 12 == 0 else f"{intro_m} months"


def reset_coverage_matrix(
    con: duckdb.DuckDBPyConnection, first: int, last: int, as_of_year: int, min_share: float
) -> tuple[pl.DataFrame, float]:
    """PLAN.md §7.2 step 5 as a table: one row per intro period holding at least
    ``min_share`` of ARM dollars, one column per reset year. A cell is ✓ when every
    origination year behind it is loaded; otherwise it names the missing years. Also returns
    the share of ARM dollars in the intro periods left out."""
    one = con.sql("SELECT min(scenario) FROM dim_scenario").fetchone()[0]
    shares = con.execute(
        """
        SELECT intro_m, sum(orig_amount) / sum(sum(orig_amount)) OVER () AS share
        FROM fact_reset_calendar WHERE reset_kind = 'first' AND scenario = ?
        GROUP BY intro_m ORDER BY intro_m
        """,
        [one],
    ).pl()
    shown = shares.filter(pl.col("share") >= min_share)
    loaded = {y for (y,) in con.sql("SELECT DISTINCT activity_year FROM stg_hmda").fetchall()}
    cells = con.execute(
        "SELECT * FROM v_reset_coverage WHERE reset_year BETWEEN ? AND ?", [first, last]
    ).pl()
    rows = []
    for intro_m, share in shown.iter_rows():
        row: dict[str, object] = {"months to first reset": _intro_label(intro_m)}
        row["share of ARM $"] = share
        for cell in (
            cells.filter(pl.col("intro_m") == intro_m).sort("reset_year").iter_rows(named=True)
        ):
            missing = [
                y for y in range(cell["first_cohort"], cell["last_cohort"] + 1) if y not in loaded
            ]
            gap = "✗ " + ", ".join(map(str, missing))
            text = {"complete": "✓", "missing": gap}.get(
                cell["status"], f"{cell['coverage']:.0%} ({gap})"
            )
            header = f"{cell['reset_year']} *" if cell["reset_year"] == as_of_year else None
            row[header or str(cell["reset_year"])] = text
        rows.append(row)
    return pl.DataFrame(rows), 1 - shown["share"].sum()


def _fill_group(source: str) -> str:
    """'median: activity_year, intro_bucket' -> 'year, intro bucket'."""
    group = source.removeprefix("median: ")
    return group.replace("activity_year", "year").replace("intro_bucket", "intro bucket")


def reset_input_counts(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """Filled-rate counts by origination year (PLAN.md §11), with the filled terms and the
    interest-only ARMs that the IO assumption applies to."""
    by_source = ",\n".join(
        f"coalesce(sum(rate_filled) FILTER (WHERE rate_fill_source = {_sql_str(source)}), 0) "
        f'AS "filled: {_fill_group(source)}"'
        for source in FILL_SOURCES
    )
    return con.sql(
        f"""
        SELECT activity_year                          AS year,
               sum(arm_loans)                         AS "ARM loans",
               sum(rate_reported)                     AS "rate reported",
               sum(rate_out_of_range)                 AS "rate out of range",
               {by_source},
               sum(rate_missing)                      AS "no rate",
               sum(rate_filled) / sum(arm_loans)      AS "filled share",
               sum(term_out_of_range)                 AS "term out of range",
               sum(term_filled)                       AS "term filled",
               sum(io_loans)                          AS "interest-only",
               sum(io_amount) / sum(arm_amount)       AS "interest-only share ($)"
        FROM qa_reset_inputs GROUP BY ALL ORDER BY year
        """
    ).pl()


def reset_calendar_sections(settings: Settings, con: duckdb.DuckDBPyConnection) -> list[str]:
    """The phase 4 checkpoint (PLAN.md §12): the calendar by scenario, the coverage matrix
    and the filled-rate counts."""
    model = settings.model
    first, last = model.calendar_years
    as_of_year = model.as_of.year
    scenarios = con.sql("SELECT scenario, cpr FROM dim_scenario ORDER BY cpr, scenario").fetchall()
    named = dict(scenarios)
    shown = "base" if "base" in named else scenarios[len(scenarios) // 2][0]
    listed = ", ".join(f"{name} {_percent(cpr)}" for name, cpr in scenarios)
    matrix, other_share = reset_coverage_matrix(con, first, last, as_of_year, min_share=0.01)
    loaded = [
        y for (y,) in con.sql("SELECT DISTINCT activity_year FROM stg_hmda ORDER BY 1").fetchall()
    ]
    subsequent = con.sql(
        "SELECT count(*) FROM fact_reset_calendar WHERE reset_kind = 'subsequent'"
    ).fetchone()[0]
    return [
        "",
        "## First-reset calendar",
        "",
        "HMDA ARMs by the calendar year of their first rate reset (PLAN.md §7.2). Origination "
        "dates are assumed to be spread evenly over each year, so a loan's reset can split "
        "across two calendar years, with weights that add up to 1. The balance at reset "
        "applies scheduled amortization at the note rate, then survival of (1 - CPR)^(k/12).",
        "",
        f"- **The CPR scenarios are assumptions, not estimates:** {listed} a year, prepayment "
        "and default together (`model.scenarios` in config.yaml).",
        "- **Interest-only ARMs are assumed to stay interest-only through the first reset.** "
        "HMDA doesn't report the interest-only period, so they reach it without amortizing.",
        "- **Each scenario holds every loan once.** Never add scenarios together.",
        "",
        "### Calendar by scenario",
        "",
        f"First resets in {first}-{last} (`model.calendar_years`). ARM loans are weighted "
        "counts; they and the original amount are the same in every scenario. * The current "
        f"year ({as_of_year}, from `model.as_of` {model.as_of}) includes resets that already "
        "happened earlier in the year.",
        "",
        markdown_table(reset_calendar_by_scenario(con, first, last, as_of_year)),
        "",
        f"### Balance at reset by holder segment, {shown} scenario ($bn)",
        "",
        "Holder at origination: 'retained' means not sold in the origination year, so the "
        "segment is a proxy for today's holder.",
        "",
        markdown_table(reset_calendar_by_holder(con, shown, first, last)),
        "",
        "### Coverage: reset year by months to first reset",
        "",
        f"Loaded origination years: {', '.join(map(str, loaded))}. A reset year is complete "
        "(✓) for an intro period only when every origination year behind it is loaded; ✗ "
        "names the missing years, and a percentage gives the share that is loaded. Rows are "
        "the intro periods holding at least 1% of ARM dollars; the rest "
        f"({other_share:.1%} of ARM dollars) are in `v_reset_coverage`.",
        "",
        markdown_table(matrix),
        "",
        "- **Exempt filers:** ARMs from filers exempt from reporting the intro period can't "
        "be placed in the calendar. Their share of each origination year is the 'exempt "
        "share' in the HMDA table above.",
        "- **Reporting thresholds:** lenders below HMDA's reporting thresholds don't file, so "
        "their loans are absent from every year.",
        "- **Subsequent resets** (`model.subsequent_resets`): "
        + (
            f"on; {subsequent:,} rows are modeled later resets, counted once per reset."
            if subsequent
            else "off, so the calendar holds first resets only."
        ),
        "",
        "### Interest rates and terms filled",
        "",
        "PLAN.md §7.2: a missing interest rate takes the median rate of ARMs in the same "
        "origination year, intro_m bucket and conforming status. If none has a rate, the "
        "next, wider group is used. Missing loan terms are filled the same way. "
        "Interest-only ARMs are the ones the interest-only assumption applies to.",
        "",
        markdown_table(reset_input_counts(con)),
    ]


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
        if {"stg_hmda", "v_hmda_orig_summary", "v_bank_hmda_link"} <= names:
            sections += [
                "",
                "## HMDA",
                "",
                "Originated first-lien closed-end loans on 1-4 family dwellings, excluding "
                "reverse mortgages. ARMs have an intro period shorter than the loan term; "
                "'exempt' rows come from filers exempt from reporting the intro period.",
                "",
                markdown_table(hmda_year_stats(con)),
                "",
                "### Months to first reset (share of ARM loans)",
                "",
                markdown_table(hmda_intro_histogram(con)),
                "",
                "### ARMs by holder segment",
                "",
                "Holder at origination: 'retained' means not sold in the origination year.",
                "",
                markdown_table(hmda_holder_segments(con)),
                "",
                "### Lenders linked to a Call Report filer (RSSD match)",
                "",
                "Only banks and savings associations file Call Reports, so credit unions, "
                "mortgage companies and bank affiliates are expected to be unlinked.",
                "",
                markdown_table(hmda_rssd_match(con)),
            ]
        if {"dim_hmda_lender", "dim_institution_type"} <= names:
            segments = Segments.load(settings.config_file("segments.yaml"))
            sections += [
                "",
                "## HMDA lenders",
                "",
                "From the Philadelphia Fed HMDA Lender File, one row per lender and year. "
                "`lender_type` maps the file's institution type (config/segments.yaml).",
                "",
                "### Lenders by type",
                "",
                "A lender whose RSSD files a Call Report that year is typed "
                f"{segments.call_report_filer_type}, whatever its institution code. The last two "
                "columns count where the code and the Call Report link disagree.",
                "",
                markdown_table(lender_types_by_year(con, segments.call_report_filer_type)),
            ]
            coverage = lender_file_coverage(settings, con)
            if coverage is not None:
                sections += [
                    "",
                    "### Lender File coverage",
                    "",
                    "Each CFPB list of lenders against the Lender File: the Reporter Panel for "
                    "2018-2023, the Data Browser filers list after that. The agreement columns "
                    "count lenders in both; no RSSD in either file counts as agreeing.",
                    "",
                    markdown_table(coverage),
                ]
        if {"fact_reset_calendar", "qa_reset_inputs", "dim_scenario", "v_reset_coverage"} <= names:
            sections += reset_calendar_sections(settings, con)
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
