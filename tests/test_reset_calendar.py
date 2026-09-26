from collections import defaultdict
from datetime import date
from pathlib import Path

import duckdb
import polars as pl
import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from armreset.db import connect, load_saved_queries
from armreset.fetch.hmda import csv_to_parquet
from armreset.fetch.lenders import KEY as LENDER_KEY
from armreset.fetch.lenders import lender_file_path
from armreset.manifest import Manifest
from armreset.model.amortization import sched_factor, survival, survival_between
from armreset.model.reset_calendar import (
    FILL_SOURCES,
    reset_year_spans,
    reset_year_weights,
    split_ctes,
    year_fraction,
)
from armreset.pipeline import build
from armreset.qa import write_report
from armreset.settings import ModelConfig, Settings, load_settings
from tests.conftest import REPO_ROOT
from tests.hmda_fixtures import (
    BANK_LEI,
    IMC_LEI,
    UNLISTED_LEI,
    lender_file_frame,
    row,
    write_lar_csv,
    write_lender_file,
)


# PLAN.md §11: reset weights
def test_reset_year_weights_examples() -> None:
    assert reset_year_weights(2021, 60) == {2026: 1.0}
    assert reset_year_weights(2021, 6) == {2021: 0.5, 2022: 0.5}
    assert reset_year_weights(2021, 84) == {2028: 1.0}


@given(st.integers(2018, 2030), st.integers(1, 480))
def test_reset_year_weights_sum_to_one(orig_year: int, intro_m: int) -> None:
    weights = reset_year_weights(orig_year, intro_m)
    assert sum(weights.values()) == pytest.approx(1.0)
    assert 1 <= len(weights) <= 2
    assert min(weights) == orig_year + intro_m // 12
    assert all(w > 0 for w in weights.values())


def test_year_fraction_is_the_end_of_the_day() -> None:
    assert year_fraction(date(2026, 6, 30)) == 2026 + 181 / 365
    assert year_fraction(date(2025, 12, 31)) == 2026.0
    assert year_fraction(date(2024, 12, 31)) == 2025.0  # leap year
    assert year_fraction(date(2026, 1, 1)) == 2026 + 1 / 365


def test_sql_split_matches_python() -> None:
    sample = [
        (year, intro)
        for year in (2018, 2021, 2025)
        for intro in (1, 6, 7, 11, 12, 13, 36, 59, 60, 61, 84, 119, 120, 180, 181, 359)
    ]
    con = duckdb.connect()
    con.execute("CREATE TABLE loans (activity_year INTEGER, intro_m INTEGER)")
    con.executemany("INSERT INTO loans VALUES (?, ?)", sample)
    rows = con.execute(
        f"WITH {split_ctes('loans', 'activity_year + intro_m / 12.0')} "
        "SELECT activity_year, intro_m, reset_year, w FROM split"
    ).fetchall()
    got: dict[tuple[int, int], dict[int, float]] = defaultdict(dict)
    for year, intro, reset_year, w in rows:
        got[(year, intro)][reset_year] = w
    assert set(got) == set(sample)
    for year, intro in sample:
        assert got[(year, intro)] == pytest.approx(reset_year_weights(year, intro)), (year, intro)


# A loan book with every case the model handles. EXPECT holds each ARM's inputs as the model
# should use them: (orig_year, intro_m, amount, rate, term, interest-only), with the filled
# values worked out by hand from PLAN.md §7.2's median rule.
ARM_2021 = {
    "A": row(intro_rate_period="60", conforming_loan_limit="NC", loan_amount="805000",
             state_code="CA"),
    "F": row(intro_rate_period="60", conforming_loan_limit="NC", loan_amount="905000",
             interest_rate="4.0", state_code="TX"),
    # Rate filled from its own group (2021, 60, NC): the median of 3.0 and 4.0.
    "E": row(intro_rate_period="60", conforming_loan_limit="NC", loan_amount="705000",
             interest_rate="NA"),
    # Term filled from its own group (2021, 60, C), where the other loan has 360 months.
    "I": row(intro_rate_period="60", interest_rate="3.25", loan_term="NA", state_code="NJ"),
    "L": row(intro_rate_period="60", interest_rate="3.25", purchaser_type="3"),
    "B": row(intro_rate_period="84", interest_rate="2.75"),
    # Group (2021, 84, U) has no rate; the wider group (2021, 84) has B's 2.75.
    "G": row(intro_rate_period="84", conforming_loan_limit="U", interest_rate="NA"),
    # Interest-only, sold to Fannie Mae.
    "C": row(intro_rate_period="120", interest_only_payment="1", purchaser_type="1",
             interest_rate="3.5", loan_amount="505000", lei=IMC_LEI),
    # Nothing else resets at 180 months in 2021: the 2021 median of 3.0, 4.0, 3.25, 3.25,
    # 2.75, 3.5, 4.0 and 3.25 is 3.25 (M's 362,500 is left out).
    "H": row(intro_rate_period="180", interest_rate="NA"),
    # Resets after 6 months: half in 2021, half in 2022.
    "D": row(intro_rate_period="6", interest_rate="4.0", loan_amount="405000"),
    # Reporting errors, filled like missing values: a rate of 362,500% (B's group has 2.75)
    # and a 999-month term (L's group has 360).
    "M": row(intro_rate_period="84", interest_rate="362500"),
    "N": row(intro_rate_period="60", interest_rate="3.25", loan_term="999", state_code="PA"),
}  # fmt: skip
ARM_2022 = {
    "J": row(activity_year="2022", intro_rate_period="36", interest_rate="5.0"),
    # Not in the Lender File: its lender type is unknown.
    "K": row(activity_year="2022", intro_rate_period="12", interest_rate="5.5", lei=UNLISTED_LEI),
}
NOT_ARMS = [row(), row(intro_rate_period="Exempt", interest_rate="Exempt", loan_term="Exempt")]
EXPECT = {
    "A": (2021, 60, 805_000, 3.0, 360, False),
    "F": (2021, 60, 905_000, 4.0, 360, False),
    "E": (2021, 60, 705_000, 3.5, 360, False),
    "I": (2021, 60, 305_000, 3.25, 360, False),
    "L": (2021, 60, 305_000, 3.25, 360, False),
    "B": (2021, 84, 305_000, 2.75, 360, False),
    "G": (2021, 84, 305_000, 2.75, 360, False),
    "C": (2021, 120, 505_000, 3.5, 360, True),
    "H": (2021, 180, 305_000, 3.25, 360, False),
    "D": (2021, 6, 405_000, 4.0, 360, False),
    "M": (2021, 84, 305_000, 2.75, 360, False),
    "N": (2021, 60, 305_000, 3.25, 360, False),
    "J": (2022, 36, 305_000, 5.0, 360, False),
    "K": (2022, 12, 305_000, 5.5, 360, False),
}
CPR = {"low": 0.06, "base": 0.10, "high": 0.15}


def _record_book(s: Settings, lender_file: bool = True) -> None:
    manifest = Manifest.for_settings(s)
    for year, rows in [
        (2021, [*ARM_2021.values(), *NOT_ARMS]),
        (2022, list(ARM_2022.values())),
    ]:
        csv = write_lar_csv(s.raw_dir / "hmda" / f"lar_{year}.csv", rows)
        csv_to_parquet(csv, csv.with_suffix(".parquet"))
        manifest.record(f"hmda:{year}:nationwide", "hmda", csv, period=str(year))
        manifest.add_derived(f"hmda:{year}:nationwide", csv.with_suffix(".parquet"))
    if lender_file:
        frame = pl.concat([lender_file_frame(year=2021), lender_file_frame(year=2022)])
        lenders = write_lender_file(lender_file_path(s), frame)
        manifest.record(LENDER_KEY, "philfed_lender", lenders, period="2021-2022")


@pytest.fixture
def built(project: Path) -> Settings:
    s = load_settings()
    assert s.model.scenarios == CPR
    _record_book(s)
    summary = build(s)
    assert {"v_reset_calendar", "v_reset_coverage"} <= set(summary.views)
    return s


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    cursor = con.execute(sql)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, r, strict=True)) for r in cursor.fetchall()]


def _expected_balances(loans: dict[str, tuple], model: ModelConfig) -> dict[tuple[str, int], float]:
    """The calendar from the reference functions: balance at the first reset by scenario and
    reset year, with each origination year's history CPR up to the as-of date."""
    as_of_t = year_fraction(model.as_of)
    out: dict[tuple[str, int], float] = defaultdict(float)
    for orig_year, intro, amount, rate, term, io in loans.values():
        factor = sched_factor(rate, term, intro, io)
        for scenario, cpr in model.scenarios.items():
            history = model.history_cpr.get(orig_year, cpr)
            for reset_year, (t_lo, t_hi) in reset_year_spans(orig_year, intro).items():
                share = survival_between(history, cpr, intro, t_lo, t_hi, as_of_t)
                out[(scenario, reset_year)] += amount * factor * share
    return out


def _balances_by_year(s: Settings) -> dict[tuple[str, int], float]:
    con = connect(s, read_only=True)
    try:
        return {
            (r["scenario"], r["reset_year"]): r["bal"]
            for r in _rows(
                con,
                "SELECT scenario, reset_year, sum(bal_at_reset) AS bal FROM fact_reset_calendar "
                "GROUP BY ALL",
            )
        }
    finally:
        con.close()


def test_the_calendar_matches_the_python_model(built: Settings) -> None:
    assert {2021, 2022} <= set(built.model.history_cpr)  # the book's years have a history CPR
    got = _balances_by_year(built)
    expected = _expected_balances(EXPECT, built.model)
    assert set(got) == set(expected)
    for key, value in expected.items():
        assert got[key] == pytest.approx(value, rel=1e-9), key


def _set_model(project: Path, **values) -> None:
    """Replace keys under ``model`` in the project's config.yaml (patch_config merges)."""
    path = project / "config.yaml"
    config = yaml.safe_load(path.read_text())
    config["model"].update(values)
    path.write_text(yaml.safe_dump(config, sort_keys=False))


def test_a_year_without_history_takes_the_scenario_cpr(project: Path) -> None:
    _set_model(project, history_cpr={2021: 0.25})
    s = load_settings()
    _record_book(s)
    build(s)
    got = _balances_by_year(s)
    expected = _expected_balances(EXPECT, s.model)
    assert got == pytest.approx(expected, rel=1e-9)
    # 2022's 3-year ARM reset in 2025, before the as-of date. With no history CPR for 2022
    # it takes each scenario's CPR, the flat model of phase 4.
    for scenario, cpr in s.model.scenarios.items():
        flat = 305_000 * sched_factor(5.0, 360, 36) * survival(cpr, 36)
        assert got[(scenario, 2025)] == pytest.approx(flat, rel=1e-9)


def test_as_of_before_the_last_origination_year_is_refused(project: Path) -> None:
    _set_model(project, as_of="2022-06-30")
    s = load_settings()
    _record_book(s)
    with pytest.raises(ValueError, match=r"model.as_of \(2022-06-30\).*2022-12-31"):
        build(s)


def test_every_arm_counts_once_in_each_scenario(built: Settings) -> None:
    con = connect(built, read_only=True)
    try:
        arms = con.execute(
            "SELECT count(*), sum(loan_amount) FROM stg_hmda WHERE rate_type = 'arm'"
        ).fetchone()
        per_scenario = _rows(
            con,
            "SELECT scenario, sum(w_loans) AS loans, sum(orig_amount) AS amount, "
            "count(*) FILTER (WHERE reset_kind <> 'first') AS later "
            "FROM fact_reset_calendar GROUP BY ALL",
        )
    finally:
        con.close()
    assert arms == (len(EXPECT), sum(e[2] for e in EXPECT.values()))
    assert {r["scenario"] for r in per_scenario} == set(CPR)
    for r in per_scenario:
        assert r["loans"] == pytest.approx(arms[0])
        assert r["amount"] == pytest.approx(arms[1])
        assert r["later"] == 0  # subsequent resets are off by default


def test_scenarios_change_only_what_is_still_ahead(built: Settings) -> None:
    con = connect(built, read_only=True)
    try:
        years = _rows(
            con,
            "SELECT reset_year, "
            "sum(bal_at_reset) FILTER (WHERE scenario = 'low') AS low, "
            "sum(bal_at_reset) FILTER (WHERE scenario = 'base') AS base, "
            "sum(bal_at_reset) FILTER (WHERE scenario = 'high') AS high, "
            "sum(orig_amount) FILTER (WHERE scenario = 'low') AS orig "
            "FROM fact_reset_calendar GROUP BY ALL",
        )
    finally:
        con.close()
    as_of_year = built.model.as_of.year
    assert {y["reset_year"] for y in years} & set(range(2021, as_of_year))  # some in the past
    for y in years:
        if y["reset_year"] < as_of_year:
            # Resets before the as-of year have happened: history only, whatever the scenario.
            assert y["orig"] > y["low"] == pytest.approx(y["base"]) == pytest.approx(y["high"])
        else:
            assert y["orig"] > y["low"] > y["base"] > y["high"] > 0, y


def test_six_month_intro_splits_across_two_years(built: Settings) -> None:
    con = connect(built, read_only=True)
    try:
        split = _rows(
            con,
            "SELECT reset_year, w_loans FROM fact_reset_calendar "
            "WHERE scenario = 'base' AND intro_m = 6 ORDER BY reset_year",
        )
    finally:
        con.close()
    assert [(r["reset_year"], r["w_loans"]) for r in split] == [(2021, 0.5), (2022, 0.5)]


def test_filled_rates_and_terms_are_counted(built: Settings) -> None:
    con = connect(built, read_only=True)
    try:
        groups = {
            (r["activity_year"], r["intro_bucket"], r["conforming"]): r
            for r in _rows(con, "SELECT * FROM qa_reset_inputs")
        }
        flagged = _rows(
            con,
            "SELECT intro_m, conforming, rate_filled, term_filled, sum(w_loans) AS loans "
            "FROM fact_reset_calendar WHERE scenario = 'base' AND (rate_filled OR term_filled) "
            "GROUP BY ALL ORDER BY ALL",
        )
    finally:
        con.close()
    assert sum(g["arm_loans"] for g in groups.values()) == len(EXPECT)
    by_group, by_intro, by_year = FILL_SOURCES
    nc60 = groups[(2021, "60", "NC")]
    assert (nc60["rate_filled"], nc60["rate_fill_source"], nc60["rate_fill_value"]) == (
        1,
        by_group,
        3.5,
    )
    u84 = groups[(2021, "84", "U")]
    assert (u84["rate_fill_source"], u84["rate_fill_value"]) == (by_intro, 2.75)
    c180 = groups[(2021, "180", "C")]
    assert (c180["rate_fill_source"], c180["rate_fill_value"]) == (by_year, 3.25)
    c84 = groups[(2021, "84", "C")]  # M's 362,500 is out of range
    assert (c84["rate_out_of_range"], c84["rate_filled"], c84["rate_fill_value"]) == (1, 1, 2.75)
    c60 = groups[(2021, "60", "C")]  # I has no term, N has 999 months
    assert (c60["term_filled"], c60["term_out_of_range"]) == (2, 1)
    assert groups[(2021, "120", "C")]["io_loans"] == 1
    assert sum(g["rate_missing"] + g["term_missing"] for g in groups.values()) == 0
    assert [
        (r["intro_m"], r["conforming"], r["rate_filled"], r["term_filled"]) for r in flagged
    ] == [
        (60, "C", False, True),
        (60, "NC", True, False),
        (84, "C", True, False),
        (84, "U", True, False),
        (180, "C", True, False),
    ]


def test_segments_and_lender_types(built: Settings) -> None:
    con = connect(built, read_only=True)
    try:
        rows = _rows(
            con,
            "SELECT DISTINCT orig_year, intro_m, lei, holder_segment, lender_type "
            "FROM fact_reset_calendar",
        )
    finally:
        con.close()
    kinds = {(r["orig_year"], r["intro_m"], r["lei"]): r for r in rows}
    assert kinds[(2021, 120, IMC_LEI)]["holder_segment"] == "gse"
    assert kinds[(2021, 120, IMC_LEI)]["lender_type"] == "independent_mortgage_company"
    assert kinds[(2021, 84, BANK_LEI)]["lender_type"] == "bank"
    assert kinds[(2022, 36, BANK_LEI)]["lender_type"] == "bank"
    assert kinds[(2022, 12, UNLISTED_LEI)]["lender_type"] == "unknown"  # not in the Lender File
    assert {r["holder_segment"] for r in rows} == {"retained", "gse"}


def test_reset_calendar_view_labels_every_row(built: Settings) -> None:
    con = connect(built, read_only=True)
    try:
        facts = con.execute("SELECT count(*) FROM fact_reset_calendar").fetchone()[0]
        view = _rows(con, "SELECT * FROM v_reset_calendar")
    finally:
        con.close()
    assert len(view) == facts
    history = built.model.history_cpr
    for r in view:
        assert r["forward_cpr_assumption"] == CPR[r["scenario"]]
        assert r["history_cpr_assumption"] == history[r["orig_year"]]
        assert r["is_current_year"] == (r["reset_year"] == 2026)  # model.as_of 2026-06-30
    labels = {(r["conforming"], r["conforming_label"]) for r in view}
    assert labels == {("C", "Conforming"), ("NC", "Jumbo (nonconforming)"), ("U", "Undetermined")}
    assert {r["holder_label"] for r in view} == {
        "Retained (not sold in origination year)",
        "Sold to Fannie / Freddie",
    }
    assert {r["occupancy_label"] for r in view} == {"Principal residence"}
    assert "Unknown" in {r["lender_type_label"] for r in view}


def test_coverage_marks_missing_origination_years(built: Settings) -> None:
    con = connect(built, read_only=True)
    try:
        cells = {
            (r["reset_year"], r["intro_m"]): r for r in _rows(con, "SELECT * FROM v_reset_coverage")
        }
    finally:
        con.close()

    def cell(year: int, intro: int) -> tuple:
        c = cells[(year, intro)]
        return c["status"], round(c["coverage"], 9), c["first_cohort"], c["last_cohort"]

    # 2021 and 2022 are loaded.
    assert cell(2026, 60) == ("complete", 1.0, 2021, 2021)
    assert cell(2025, 60) == ("missing", 0.0, 2020, 2020)
    # PLAN.md §7.2: 10-year ARMs resetting in 2026 come from 2016 originations.
    assert cell(2026, 120) == ("missing", 0.0, 2016, 2016)
    # 3-year ARMs resetting in 2028 come from 2025 originations, not loaded here.
    assert cell(2028, 36) == ("missing", 0.0, 2025, 2025)
    # 6-month ARMs resetting in 2022 were originated in mid-2021 through mid-2022.
    assert cell(2022, 6) == ("complete", 1.0, 2021, 2022)
    assert cell(2021, 6) == ("partial", 0.5, 2020, 2021)
    assert cell(2023, 6) == ("partial", 0.5, 2022, 2023)


def test_subsequent_resets_roll_the_balance_forward(project: Path, patch_config) -> None:
    patch_config({"model": {"subsequent_resets": {"enabled": True, "frequency_months": 12}}})
    s = load_settings()
    _record_book(s)
    build(s)
    con = connect(s, read_only=True)
    try:
        later = _rows(
            con,
            "SELECT lei, state_code, reset_year, w_loans, bal_at_reset FROM fact_reset_calendar "
            "WHERE scenario = 'base' AND reset_kind = 'subsequent' ORDER BY reset_year",
        )
        first = con.execute(
            "SELECT sum(w_loans) FROM fact_reset_calendar "
            "WHERE scenario = 'base' AND reset_kind = 'first'"
        ).fetchone()[0]
    finally:
        con.close()
    assert first == pytest.approx(len(EXPECT))  # first resets are unchanged
    as_of_t, history = year_fraction(s.model.as_of), s.model.history_cpr[2021]

    def survives(k: int, reset_year: int) -> float:
        return survival_between(history, 0.10, k, reset_year, reset_year + 1, as_of_t)

    # Loan A resets first in 2026 (60 months), then every 12 months through 2032.
    a = [r for r in later if r["state_code"] == "CA"]
    assert [r["reset_year"] for r in a] == list(range(2027, 2033))
    for r in a:
        k = 12 * (r["reset_year"] - 2021)
        assert r["w_loans"] == 1.0
        expected = 805_000 * sched_factor(3.0, 360, k) * survives(k, r["reset_year"])
        assert r["bal_at_reset"] == pytest.approx(expected, rel=1e-9)
    # The interest-only loan C amortizes only after its first reset in 2031.
    [c] = [r for r in later if r["lei"] == IMC_LEI]
    assert c["reset_year"] == 2032
    assert c["bal_at_reset"] == pytest.approx(
        505_000 * sched_factor(3.5, 360 - 120, 12) * survives(132, 2032), rel=1e-9
    )
    assert max(r["reset_year"] for r in later) <= 2033  # model.calendar_years ends in 2032


def test_without_the_lender_file_lender_types_are_unknown(project: Path) -> None:
    s = load_settings()
    _record_book(s, lender_file=False)
    build(s)
    con = connect(s, read_only=True)
    try:
        types = con.execute("SELECT DISTINCT lender_type FROM v_reset_calendar").fetchall()
    finally:
        con.close()
    assert types == [("unknown",)]


def test_saved_reset_query_runs(built: Settings) -> None:
    queries = load_saved_queries(REPO_ROOT / "config" / "saved_queries.sql")
    [sql] = [q for q in queries.values() if "v_reset_calendar" in q]
    con = connect(built, read_only=True)
    try:
        rows = con.execute(sql).fetchall()
    finally:
        con.close()
    # Retained jumbo ARMs: A, F and E (60 months, 2021) all reset in 2026.
    assert [r[0] for r in rows] == [2026]
    assert rows[0][2] == pytest.approx(3.0)


def test_qa_report_shows_the_phase_4_checkpoint(built: Settings) -> None:
    text = write_report(built).read_text()
    for expected in (
        "## First-reset calendar",
        "### Calendar by scenario",
        "assumptions, not estimates",
        "| 2026 * |",
        "### Balance at reset by holder segment, base scenario",
        "### Coverage: reset year by months to first reset",
        "| 60 (5 yr) |",
        "✗ 2016",
        "### Interest rates and terms filled",
        # 12 ARMs: 8 rates used as reported, 1 out of range; filled by group 2, 1, 1.
        "| 2021 | 12 | 8 | 1 | 2 | 1 | 1 | 0 |",
    ):
        assert expected in text, expected


def test_a_balance_that_is_not_a_number_stops_the_build(project: Path) -> None:
    s = load_settings()
    csv = write_lar_csv(
        s.raw_dir / "hmda" / "lar_2021.csv", [row(intro_rate_period="60", loan_amount="nan")]
    )
    csv_to_parquet(csv, csv.with_suffix(".parquet"))
    manifest = Manifest.for_settings(s)
    manifest.record("hmda:2021:nationwide", "hmda", csv, period="2021")
    manifest.add_derived("hmda:2021:nationwide", csv.with_suffix(".parquet"))
    with pytest.raises(ValueError, match="isn't a number"):
        build(s)
