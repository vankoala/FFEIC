"""Balance reaching a rate reset (PLAN.md §7.2 step 3): scheduled amortization and survival.

``bal_at_reset = loan_amount * A(k) * S(k)``, where k is the number of months from origination
to the reset. The Python functions are the reference; the SQL expressions compute the same
thing for the whole table, and the tests hold the two together.
"""

from __future__ import annotations


def sched_factor(rate_pct: float, term_m: int, k_m: int, io: bool = False) -> float:
    """A(k): the share of the original balance still scheduled after ``k_m`` monthly payments
    on a level-payment loan at note rate ``rate_pct`` over ``term_m`` months.

    Interest-only loans don't amortize: HMDA doesn't report the interest-only period, so it
    is assumed to last at least until the first reset (``model.io_assumption``)."""
    if io or k_m <= 0:
        return 1.0
    if k_m >= term_m:
        return 0.0
    i = rate_pct / 1200
    if i == 0:
        return 1 - k_m / term_m
    g = 1 + i
    return (g**term_m - g**k_m) / (g**term_m - 1)


def survival(cpr: float, k_m: float) -> float:
    """S(k): the share of loans still outstanding after ``k_m`` months at an annual rate of
    prepayment and default ``cpr``. CPR comes from a scenario: an assumption, not an estimate."""
    return (1 - cpr) ** (k_m / 12)


def sched_factor_sql(rate: str, term: str, k: str) -> str:
    """:func:`sched_factor` without the interest-only case, as a DuckDB expression over the
    given column expressions. Callers handle interest-only loans by shifting ``term`` and
    ``k`` (see ``model/reset_calendar.py``).

    The formula is divided through by g^n: (1 - g^(k-n)) / (1 - g^-n) equals
    (g^n - g^k) / (g^n - 1), but its powers stay below 1 for a positive rate, so it can't
    overflow to NaN however high a reported rate is."""
    g = f"(1 + ({rate}) / 1200.0)"
    return (
        f"CASE WHEN ({k}) <= 0 THEN 1.0 "
        f"WHEN ({k}) >= ({term}) THEN 0.0 "
        f"WHEN ({rate}) = 0 THEN 1 - ({k})::DOUBLE / ({term}) "
        f"ELSE (1 - pow({g}, ({k}) - ({term}))) / (1 - pow({g}, -({term}))) END"
    )


def survival_sql(cpr: str, k: str) -> str:
    """:func:`survival` as a DuckDB expression."""
    return f"pow(1 - ({cpr}), ({k}) / 12.0)"
