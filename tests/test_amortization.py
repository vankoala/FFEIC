import duckdb
import pytest

from armreset.model.amortization import (
    sched_factor,
    sched_factor_sql,
    survival,
    survival_between,
    survival_between_sql,
    survival_sql,
)

AS_OF = 2026 + 181 / 365  # 2026-06-30, end of day
# (history CPR, forward CPR, months to reset, first and last reset time): resets entirely
# before the as-of date, across it, entirely after it, with equal rates, a 1-month intro.
SPANS = [
    (0.20, 0.10, 84, 2025.3, 2026.0),
    (0.20, 0.10, 84, 2026.0, 2027.0),
    (0.05, 0.15, 60, 2026.0, 2027.0),
    (0.05, 0.15, 120, 2027.0, 2027.4),
    (0.10, 0.10, 60, 2026.2, 2026.9),
    (0.30, 0.06, 1, 2026.4, 2026.6),
    (0.00, 0.25, 36, 2026.5, 2027.5),
]


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


def _survival_numeric(history, forward, k_m, t_lo, t_hi, steps=20_000) -> float:
    """The survival integral by brute force: midpoints of many small steps."""
    width = (t_hi - t_lo) / steps
    total = 0.0
    for i in range(steps):
        ahead = max(t_lo + (i + 0.5) * width - AS_OF, 0.0)  # years after the as-of date
        total += (1 - history) ** (k_m / 12 - ahead) * (1 - forward) ** ahead * width
    return total


@pytest.mark.parametrize(("history", "forward", "k_m", "t_lo", "t_hi"), SPANS)
def test_survival_between_matches_a_numeric_integral(history, forward, k_m, t_lo, t_hi) -> None:
    got = survival_between(history, forward, k_m, t_lo, t_hi, AS_OF)
    assert got == pytest.approx(_survival_numeric(history, forward, k_m, t_lo, t_hi), rel=1e-7)


def test_survival_between_splits_at_the_as_of_date() -> None:
    # Before the as-of date only history counts; with equal rates it is plain survival.
    assert survival_between(0.2, 0.1, 84, 2025.0, 2026.0, AS_OF) == pytest.approx(0.8**7)
    assert survival_between(0.2, 0.9, 84, 2025.0, 2026.0, AS_OF) == pytest.approx(0.8**7)
    assert survival_between(0.1, 0.1, 60, 2026.0, 2027.0, AS_OF) == pytest.approx(0.9**5)
    # After it, the forecast rate takes over year by year.
    later = survival_between(0.2, 0.1, 120, 2030.0, 2030.5, AS_OF)
    assert later > survival_between(0.2, 0.2, 120, 2030.0, 2030.5, AS_OF)


def test_survival_between_sql_matches_python() -> None:
    con = duckdb.connect()
    con.execute("CREATE TABLE s (h DOUBLE, c DOUBLE, k INTEGER, lo DOUBLE, hi DOUBLE)")
    con.executemany("INSERT INTO s VALUES (?, ?, ?, ?, ?)", SPANS)
    got = con.execute(
        f"SELECT h, c, k, lo, hi, {survival_between_sql('h', 'c', 'k', 'lo', 'hi', repr(AS_OF))} "
        "FROM s"
    ).fetchall()
    assert len(got) == len(SPANS)
    for h, c, k, lo, hi, value in got:
        assert value == pytest.approx(survival_between(h, c, k, lo, hi, AS_OF), rel=1e-12)
