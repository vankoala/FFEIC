from datetime import date

import pytest

from armreset.periods import (
    Quarter,
    latest_published_quarter,
    parse_years,
    quarter_range,
    resolve_quarter,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("2018Q1", Quarter(2018, 1)), ("2026q2", Quarter(2026, 2)), (" 2020-Q4 ", Quarter(2020, 4))],
)
def test_parse_quarter(text: str, expected: Quarter) -> None:
    assert Quarter.parse(text) == expected


@pytest.mark.parametrize("text", ["2018Q5", "2018Q0", "18Q1", "2018", "Q1 2018", ""])
def test_parse_quarter_rejects_bad_input(text: str) -> None:
    with pytest.raises(ValueError):
        Quarter.parse(text)


def test_quarter_end_dates() -> None:
    assert [Quarter(2025, q).end_date for q in (1, 2, 3, 4)] == [
        date(2025, 3, 31),
        date(2025, 6, 30),
        date(2025, 9, 30),
        date(2025, 12, 31),
    ]


def test_quarter_navigation_crosses_years() -> None:
    assert Quarter(2025, 4).next() == Quarter(2026, 1)
    assert Quarter(2026, 1).prev() == Quarter(2025, 4)
    assert Quarter.containing(date(2026, 9, 24)) == Quarter(2026, 3)


def test_quarter_range_is_inclusive_and_ordered() -> None:
    qs = quarter_range(Quarter(2018, 1), Quarter(2019, 2))
    assert [q.label for q in qs] == ["2018Q1", "2018Q2", "2018Q3", "2018Q4", "2019Q1", "2019Q2"]
    assert quarter_range(Quarter(2020, 3), Quarter(2020, 3)) == [Quarter(2020, 3)]


def test_quarter_range_rejects_backwards() -> None:
    with pytest.raises(ValueError, match="backwards"):
        quarter_range(Quarter(2020, 2), Quarter(2020, 1))


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 9, 24), Quarter(2026, 2)),  # 2026-06-30 + 45 days = 2026-08-14
        (date(2026, 8, 14), Quarter(2026, 2)),
        (date(2026, 8, 13), Quarter(2026, 1)),
        (date(2026, 2, 14), Quarter(2025, 4)),  # 2025-12-31 + 45 days = 2026-02-14
        (date(2026, 2, 13), Quarter(2025, 3)),
    ],
)
def test_latest_published_quarter_applies_posting_lag(today: date, expected: Quarter) -> None:
    assert latest_published_quarter(today) == expected


def test_resolve_quarter_latest() -> None:
    assert resolve_quarter("latest", today=date(2026, 9, 24)) == Quarter(2026, 2)
    assert resolve_quarter("LATEST", today=date(2026, 9, 24)) == Quarter(2026, 2)
    assert resolve_quarter("2019Q3") == Quarter(2019, 3)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("2018-2025", list(range(2018, 2026))),
        ("2021", [2021]),
        ("2018,2020-2021", [2018, 2020, 2021]),
        ("2021, 2019 ,2021", [2019, 2021]),
    ],
)
def test_parse_years(spec: str, expected: list[int]) -> None:
    assert parse_years(spec) == expected


@pytest.mark.parametrize("spec", ["2025-2018", "21", "2018-", "", "twenty"])
def test_parse_years_rejects_bad_input(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_years(spec)
