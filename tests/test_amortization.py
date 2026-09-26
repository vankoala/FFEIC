import duckdb
import pytest

from armreset.model.amortization import sched_factor, sched_factor_sql, survival, survival_sql


# PLAN.md §11: sched_factor
def test_sched_factor_endpoints() -> None:
    assert sched_factor(6.0, 360, 0) == 1.0
    assert sched_factor(6.0, 360, 360) == 0.0
    assert sched_factor(6.0, 360, 400) == 0.0


def test_sched_factor_zero_rate_is_linear() -> None:
    assert sched_factor(0.0, 360, 60) == pytest.approx(1 - 60 / 360)
    assert sched_factor(0.0, 360, 180) == pytest.approx(0.5)


def test_sched_factor_interest_only_does_not_amortize() -> None:
    assert sched_factor(6.0, 360, 120, io=True) == 1.0


def test_sched_factor_matches_an_amortization_schedule() -> None:
    # $100,000 at 6% over 30 years pays $599.55 a month; after 60 payments $93,054.36 is left.
    assert 100_000 * sched_factor(6.0, 360, 60) == pytest.approx(93_054.36, abs=0.01)
    # A higher rate amortizes more slowly, a shorter term faster.
    assert sched_factor(8.0, 360, 60) > sched_factor(6.0, 360, 60) > sched_factor(6.0, 180, 60)


def test_sql_matches_python() -> None:
    grid = [
        (rate, term, k)
        for rate in (0.0, 0.001, 2.5, 6.875, 12.0)
        for term in (120, 360, 480)
        for k in (-1, 0, 1, 60, 119, 120, 360, 480)
    ]
    con = duckdb.connect()
    con.execute("CREATE TABLE g (rate DOUBLE, term INTEGER, k INTEGER)")
    con.executemany("INSERT INTO g VALUES (?, ?, ?)", grid)
    got = con.execute(
        f"SELECT rate, term, k, {sched_factor_sql('rate', 'term', 'k')}, "
        f"{survival_sql('0.1', 'k')} FROM g"
    ).fetchall()
    assert len(got) == len(grid)
    for rate, term, k, factor, surv in got:
        assert factor == pytest.approx(sched_factor(rate, term, k), abs=1e-12), (rate, term, k)
        assert surv == pytest.approx(survival(0.1, k))


def test_sql_stays_finite_for_an_absurd_rate() -> None:
    # A 2019 ARM reports 362,500 (3.625% keyed without the decimal point). The textbook form
    # overflows to NaN at that rate; the SQL form doesn't.
    with pytest.raises(OverflowError):
        sched_factor(362_500.0, 360, 84)
    factor = duckdb.sql(f"SELECT {sched_factor_sql('362500.0', '360', '84')}").fetchone()[0]
    assert factor == pytest.approx(1.0)


def test_survival() -> None:
    assert survival(0.10, 0) == 1.0
    assert survival(0.10, 60) == pytest.approx(0.9**5)
    assert survival(0.0, 120) == 1.0
    assert survival(0.15, 60) < survival(0.10, 60) < survival(0.06, 60)
