from pathlib import Path

import polars as pl
import pytest
from streamlit.testing.v1 import AppTest

import armreset.app
from armreset.app import data
from armreset.db import connect
from armreset.manifest import Manifest
from armreset.pipeline import build
from armreset.settings import Settings, load_settings
from tests.cdr_fixtures import make_cdr_zip
from tests.hmda_fixtures import BANK_LEI
from tests.test_reset_calendar import _record_book

APP = Path(armreset.app.__file__).parent
PAGES = sorted((APP / "pages").glob("*.py"))


@pytest.fixture
def warehouse(project: Path) -> Settings:
    """The HMDA book of test_reset_calendar, with Call Reports for 2026Q1 and 2026Q2."""
    s = load_settings()
    _record_book(s)
    manifest = Manifest.for_settings(s)
    for key, stamp, period in [
        ("cdr:2026Q1", "03312026", "2026-03-31"),
        ("cdr:2026Q2", "06302026", "2026-06-30"),
    ]:
        path = make_cdr_zip(s.raw_dir / "cdr", stamp=stamp)
        manifest.record(key, "cdr", path, period=period)
    build(s)
    return s


def _run(page: Path) -> AppTest:
    at = AppTest.from_file(str(page), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _sql(s: Settings, sql: str) -> float:
    con = connect(s, read_only=True)
    try:
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.stem)
def test_every_page_runs(warehouse: Settings, page: Path) -> None:
    at = _run(page)
    assert at.title or at.markdown


def test_home_opens_on_the_reset_calendar(warehouse: Settings) -> None:
    at = _run(APP / "Home.py")
    assert at.title[0].value == "Reset calendar"


def test_pages_say_what_to_do_without_a_warehouse(project: Path) -> None:
    at = _run(PAGES[0])
    assert "armtool build" in at.info[0].value


def test_reset_calendar_counts_the_window_and_follows_the_filters(warehouse: Settings) -> None:
    at = _run(PAGES[0])
    metrics = {m.label: m.value for m in at.metric}
    # The default window is 2026-2028: A, E, F, I, L and N reset in 2026, B, G and M in 2028.
    assert metrics["ARM loans resetting in the window"] == "9"
    expected = _sql(
        warehouse,
        "SELECT sum(bal_at_reset) FROM v_reset_calendar WHERE scenario = 'base' "
        "AND reset_kind = 'first' AND reset_year BETWEEN 2026 AND 2028",
    )
    assert metrics["Balance at reset, first resets in 2026-2028"] == f"${expected / 1e9:,.1f}bn"

    at.multiselect[0].select("Jumbo (nonconforming)").run()  # A, E and F
    assert {m.label: m.value for m in at.metric}["ARM loans resetting in the window"] == "3"
    at.slider[0].set_value((2026, 2032)).run()
    at.radio[0].set_value("Original amount").run()
    assert not at.exception
    labels = [m.label for m in at.metric]
    assert "Original amount, first resets in 2026-2032" in labels


def test_calendar_query_reconciles_to_the_fact_table(warehouse: Settings) -> None:
    df = data.calendar("base", 2018, 2060, {})
    total = _sql(
        warehouse,
        "SELECT sum(bal_at_reset) FROM fact_reset_calendar WHERE scenario = 'base'",
    )
    assert df["bal_at_reset"].sum() == pytest.approx(total)
    jumbo = data.calendar("base", 2018, 2060, {"conforming": ["Jumbo (nonconforming)"]})
    nc = _sql(
        warehouse,
        "SELECT sum(bal_at_reset) FROM fact_reset_calendar "
        "WHERE scenario = 'base' AND conforming = 'NC'",
    )
    assert jumbo["bal_at_reset"].sum() == pytest.approx(nc)
    assert set(df["holder_segment"]) <= set(data.holder_order())


def test_industry_query_matches_the_view_without_filters(warehouse: Settings) -> None:
    mine = data.industry("All banks", [], 0)
    view = data.query("SELECT * FROM v_cdr_industry ORDER BY report_date")
    assert mine["banks"].to_list() == view["n_banks"].to_list()
    assert mine["within_12m"].to_list() == view["within_12m"].to_list()
    assert mine["within_12m_share"].to_list() == pytest.approx(
        (view["within_12m_pct_first_lien"] / 100).to_list()
    )
    bands = mine.select([label for label, _ in data.BUCKET_BANDS]).sum_horizontal()
    assert bands.to_list() == pytest.approx(view["sum_buckets"].to_list())
    small = data.industry("Under $1bn", [], 0)
    assert small["banks"].sum() < mine["banks"].sum()


def test_bank_pages_show_the_linked_bank(warehouse: Settings) -> None:
    at = _run(PAGES[2])
    assert at.selectbox[0].value == 100  # BIG BANK has the largest first-lien book
    frames = [f.value for f in at.dataframe]
    lenders = next(f for f in frames if "LEI" in f.columns)
    assert set(lenders["LEI"]) == {BANK_LEI}
    assert set(lenders["Lender type"]) == {"Bank or savings association"}
    # Its HMDA resets in the next 12 months far exceed its $30k of short buckets.
    outliers = frames[-1]
    assert list(outliers["Bank"]) == ["BIG BANK, N.A."]
    at.radio[0].set_value("3 years").run()
    assert not at.exception


def test_hmda_explorer_lists_the_arm_lenders(warehouse: Settings) -> None:
    at = _run(PAGES[3])
    assert at.selectbox[0].value == 2022
    top = at.dataframe[-1].value
    assert "BIG BANK, N.A." in set(top["Lender"])
    assert top["Share of the year's ARM $"].sum() == pytest.approx(100.0)
    assert set(top["Call Report link"]) <= set(data.MATCH_STATUS.values()) | {None}


def test_hmda_years_divide_as_numbers(warehouse: Settings) -> None:
    # Sums of counts come back from DuckDB as exact decimals unless cast; dividing those
    # gave 0% shares on the page.
    years = data.hmda_years()
    assert years.schema["loans"] == pl.Int64
    row = years.filter(pl.col("year") == 2021).row(0, named=True)
    assert row["exempt_loans"] / row["loans"] == pytest.approx(1 / 14)  # 12 ARMs, 1 fixed
    assert row["arm_loans"] / row["loans"] == pytest.approx(12 / 14)


def test_arm_mix_counts_each_arm_once(warehouse: Settings) -> None:
    mix = data.arm_mix([BANK_LEI])
    loans = _sql(
        warehouse,
        f"SELECT count(*) FROM stg_hmda WHERE rate_type = 'arm' AND lei = '{BANK_LEI}'",
    )
    assert mix["arm_loans"].sum() == pytest.approx(loans)
    assert mix.filter(pl.col("orig_year") == 2021)["intro_bucket"].to_list() == [
        "01-11",
        "60",
        "84",
        "180",
    ]
