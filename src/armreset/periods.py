"""Quarter and year helpers for config values and CLI options ("2018Q1", "latest", "2018-2025")."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

# PLAN.md §5.1: CDR bulk files post about 45 days after quarter-end. This only picks a
# default for "latest"; the CDR fetcher checks it against the periods CDR actually lists.
CDR_POSTING_LAG_DAYS = 45

_QUARTER_RE = re.compile(r"(\d{4})\s*-?\s*[Qq]([1-4])")
_QUARTER_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


@dataclass(frozen=True, order=True)
class Quarter:
    year: int
    q: int

    def __post_init__(self) -> None:
        if self.q not in _QUARTER_END:
            raise ValueError(f"Quarter number must be 1-4, got {self.q}")

    @classmethod
    def parse(cls, text: str) -> Quarter:
        m = _QUARTER_RE.fullmatch(text.strip())
        if not m:
            raise ValueError(f"Not a quarter: {text!r} (expected e.g. 2018Q1)")
        return cls(int(m[1]), int(m[2]))

    @classmethod
    def containing(cls, d: date) -> Quarter:
        return cls(d.year, (d.month - 1) // 3 + 1)

    @property
    def end_date(self) -> date:
        month, day = _QUARTER_END[self.q]
        return date(self.year, month, day)

    @property
    def label(self) -> str:
        return f"{self.year}Q{self.q}"

    def next(self) -> Quarter:
        return Quarter(self.year + 1, 1) if self.q == 4 else Quarter(self.year, self.q + 1)

    def prev(self) -> Quarter:
        return Quarter(self.year - 1, 4) if self.q == 1 else Quarter(self.year, self.q - 1)

    def __str__(self) -> str:
        return self.label


def latest_published_quarter(
    today: date | None = None, lag_days: int = CDR_POSTING_LAG_DAYS
) -> Quarter:
    """Most recent quarter whose end date is at least ``lag_days`` before ``today``."""
    today = today or date.today()
    q = Quarter.containing(today)
    while q.end_date + timedelta(days=lag_days) > today:
        q = q.prev()
    return q


def resolve_quarter(spec: str, today: date | None = None) -> Quarter:
    """Parse a quarter label, or resolve ``latest`` with :func:`latest_published_quarter`."""
    if spec.strip().lower() == "latest":
        return latest_published_quarter(today)
    return Quarter.parse(spec)


def quarter_range(start: Quarter, end: Quarter) -> list[Quarter]:
    """Every quarter from ``start`` through ``end``, inclusive."""
    if start > end:
        raise ValueError(f"Quarter range runs backwards: {start} to {end}")
    out = [start]
    while out[-1] < end:
        out.append(out[-1].next())
    return out


def parse_years(spec: str) -> list[int]:
    """``"2018-2025"``, ``"2021"`` or ``"2018,2020-2021"`` -> sorted unique years."""
    years: set[int] = set()
    for part in (p.strip() for p in spec.split(",")):
        if not part:
            continue
        if m := re.fullmatch(r"(\d{4})\s*-\s*(\d{4})", part):
            first, last = int(m[1]), int(m[2])
            if first > last:
                raise ValueError(f"Year range runs backwards: {part!r}")
            years.update(range(first, last + 1))
        elif re.fullmatch(r"\d{4}", part):
            years.add(int(part))
        else:
            raise ValueError(f"Not a year or year range: {part!r} (expected e.g. 2018-2025)")
    if not years:
        raise ValueError("No years given")
    return sorted(years)
