import copy
from datetime import date
from pathlib import Path

import duckdb
import pytest

from armreset.db import VIEWS, connect, create_views, load_saved_queries
from armreset.manifest import Manifest
from armreset.pipeline import build
from armreset.settings import Settings, load_settings
from tests.cdr_fixtures import BANKS, make_cdr_zip
from tests.conftest import REPO_ROOT

Q1, Q2 = date(2026, 3, 31), date(2026, 6, 30)
BUCKETS = ["b_le_3m", "b_3_12m", "b_1_3y", "b_3_5y", "b_5_15y", "b_gt_15y"]


def _q2_banks() -> dict[str, dict]:
    """Q2: bank 700 has stopped filing; bank 100 has moved balances into the short buckets."""
    banks = copy.deepcopy({k: v for k, v in BANKS.items() if k != "700"})
    banks["100"]["values"].update({"RCONA564": "50", "RCONA565": "60", "RCONA569": "210"})
    return banks


@pytest.fixture
def con(project: Path):
    s: Settings = load_settings()
    manifest = Manifest.for_settings(s)
    for key, stamp, banks in [
        ("cdr:2026Q1", "03312026", BANKS),
        ("cdr:2026Q2", "06302026", _q2_banks()),
    ]:
        path = make_cdr_zip(s.raw_dir / "cdr", stamp=stamp, banks=banks)
        period = f"{stamp[4:]}-{stamp[:2]}-{stamp[2:4]}"
        manifest.record(key, "cdr", path, period=period)
    assert build(s).views == ["v_cdr_industry", "v_cdr_bank_latest"]
    connection = connect(s, read_only=True)
    yield connection
    connection.close()


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    cursor = con.execute(sql)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def test_industry_view_reconciles_to_the_fact_table(con) -> None:
    view = {r["report_date"]: r for r in _rows(con, "SELECT * FROM v_cdr_industry")}
    assert sorted(view) == [Q1, Q2]
    for report_date, row in view.items():
        facts = _rows(
            con, f"SELECT * FROM fact_cdr_repricing WHERE report_date = DATE '{report_date}'"
        )
        assert row["n_banks"] == len(facts)
        assert row["n_banks_reporting_buckets"] == sum(f["n_buckets_reported"] > 0 for f in facts)
        for column in [*BUCKETS, "first_lien_total", "sum_buckets", "reported_nonaccrual"]:
            assert row[column] == sum(f[column] for f in facts if f[column] is not None), column
        both = [f for f in facts if f["b_le_3m"] is not None and f["b_3_12m"] is not None]
        within = sum(f["b_le_3m"] + f["b_3_12m"] for f in both)
        assert row["within_12m"] == within
        assert row["within_12m_pct_first_lien"] == pytest.approx(
            100 * within / sum(f["first_lien_total"] for f in both)
        )
        reporting = sum(f["first_lien_total"] for f in facts if f["n_buckets_reported"] > 0)
        assert row["bucket_coverage_pct"] == pytest.approx(
            100 * reporting / sum(f["first_lien_total"] for f in facts)
        )
    assert (view[Q1]["n_banks"], view[Q2]["n_banks"]) == (7, 6)
    assert (view[Q1]["n_banks_reporting_buckets"], view[Q2]["n_banks_reporting_buckets"]) == (6, 5)


def test_bank_latest_holds_only_the_latest_quarters_filers(con) -> None:
    rows = {r["rssd_id"]: r for r in _rows(con, "SELECT * FROM v_cdr_bank_latest")}
    assert set(rows) == {int(k) for k in _q2_banks()}  # 700 stopped filing: not "latest"
    assert {r["report_date"] for r in rows.values()} == {Q2}

    big = rows[100]
    assert (big["b_le_3m"], big["b_3_12m"], big["b_1_3y"]) == (50_000, 60_000, 30_000)
    assert big["within_12m"] == 110_000
    assert big["within_3y"] == 140_000
    assert big["within_12m_pct_first_lien"] == pytest.approx(100 * 110_000 / 500_000)
    assert big["within_12m_pct_assets"] == pytest.approx(100 * 110_000 / 1_000_000)
    assert (big["name"], big["state"], big["form"]) == ("BIG BANK, N.A.", "NY", "031")
    assert big["n_qa_flags"] == 0 and big["qa_flags"] is None

    assert rows[300]["within_12m"] is None  # no buckets reported: unknown, not zero
    assert rows[600]["qa_flags"] == "buckets_partial, unparseable_value"
    assert rows[600]["within_12m_pct_assets"] is None  # total assets was "CONF"


def test_saved_queries_parse_and_the_cdr_ones_run(con) -> None:
    queries = load_saved_queries(REPO_ROOT / "config" / "saved_queries.sql")
    assert len(queries) >= 3
    assert all(sql.rstrip().endswith(";") for sql in queries.values())
    views = {name for (name,) in con.execute("SELECT view_name FROM duckdb_views()").fetchall()}
    ran = 0
    for name, sql in queries.items():
        if (
            any(v in sql for v in VIEWS)
            and all(v in views for v in VIEWS if v in sql)
            and "v_reset_calendar" not in sql
        ):
            result = con.execute(sql).fetchall()
            assert result, name
            ran += 1
    assert ran == 2  # the reset-calendar query waits for phase 4


def test_create_views_skips_views_whose_tables_are_missing(tmp_path: Path) -> None:
    with duckdb.connect(str(tmp_path / "empty.duckdb")) as fresh:
        assert create_views(fresh) == []
