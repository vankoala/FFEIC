"""HMDA LAR from the CFPB Data Browser API (PLAN.md §5.2).

One nationwide CSV per year (or one per state with ``--by-state``), filtered at the source to
originated first-lien loans; staging keeps the 1-4 family dwellings. Each file is streamed to
a ``.part`` file, checked for the ``activity_year`` header, recorded in the manifest and
converted straight to Parquet (all columns as text). The CSV is then deleted unless
``hmda.keep_raw_csv`` is set.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from armreset.http import Throttle, polite_client
from armreset.manifest import Manifest
from armreset.settings import Settings

log = logging.getLogger(__name__)

SOURCE = "hmda"
API = "https://ffiec.cfpb.gov/v2/data-browser-api/view"
# VERIFY (docs/verification.md, item 5): the API takes at most two filter criteria and
# answers a third with 400 "provide-two-or-less-filter-criteria". The plan's dwelling filter
# (1-4 family, site-built and manufactured) is applied in staging instead; it only drops
# the few multifamily loans.
FILTERS = {"actions_taken": "1", "lien_statuses": "1"}
DWELLING_CATEGORIES = (
    "Single Family (1-4 Units):Site-Built",
    "Single Family (1-4 Units):Manufactured",
)
# PLAN.md §5.2 fallback: every state plus DC and PR.
STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY", "PR",
]  # fmt: skip


class HmdaFetchError(RuntimeError):
    pass


def raw_dir(settings: Settings) -> Path:
    return settings.raw_dir / "hmda"


def manifest_key(year: int, state: str | None = None) -> str:
    return f"{SOURCE}:{year}:{state or 'nationwide'}"


def file_stem(year: int, state: str | None = None) -> str:
    return f"lar_{year}" + (f"_{state}" if state else "")


def request_url(year: int, state: str | None = None) -> tuple[str, dict[str, str]]:
    url = f"{API}/csv" if state else f"{API}/nationwide/csv"
    params = {"years": str(year), **FILTERS, **({"states": state} if state else {})}
    return url, params


@dataclass
class Streamed:
    path: Path
    bytes: int
    seconds: float
    final_url: str
    redirects: list[str] = field(default_factory=list)


def _transient(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


@retry(
    retry=retry_if_exception(_transient),
    stop=stop_after_attempt(4),
    wait=wait_exponential(min=10, max=120),
    reraise=True,
)
def stream_csv(
    client: httpx.Client, throttle: Throttle, url: str, params: dict[str, str], out: Path
) -> Streamed:
    """Stream one CSV to ``out`` via a ``.part`` file; refuse anything that isn't HMDA LAR."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    started = time.monotonic()
    try:
        with client.stream("GET", url, params=params) as response:
            response.raise_for_status()
            final_url, redirects = str(response.url), [str(r.url) for r in response.history]
            with tmp.open("wb") as f:
                for chunk in response.iter_bytes(1 << 20):
                    f.write(chunk)
    finally:
        throttle.mark()  # the interval counts from the end of the body, not the headers
    with tmp.open("rb") as f:
        first_line = f.readline()
    if b"activity_year" not in first_line:
        tmp.unlink()
        raise HmdaFetchError(f"Not an HMDA LAR CSV (first line: {first_line[:120]!r})")
    tmp.replace(out)
    return Streamed(out, out.stat().st_size, time.monotonic() - started, final_url, redirects)


def csv_to_parquet(csv: Path, out: Path) -> int:
    """Copy every column as text into zstd Parquet; returns the row count."""
    tmp = out.with_name(out.name + ".part")
    quoted = csv.as_posix().replace("'", "''")
    target = tmp.as_posix().replace("'", "''")
    con = duckdb.connect()
    try:
        con.execute(
            f"COPY (SELECT * FROM read_csv('{quoted}', all_varchar = true, header = true)) "
            f"TO '{target}' (FORMAT parquet, COMPRESSION zstd)"
        )
        rows = con.execute(f"SELECT count(*) FROM read_parquet('{target}')").fetchone()[0]
    finally:
        con.close()
    tmp.replace(out)
    return rows


@dataclass
class Outcome:
    year: int
    state: str | None
    status: str  # cached | downloaded | converted | failed
    parquet: Path | None = None
    rows: int | None = None
    streamed: Streamed | None = None
    detail: str = ""


class HmdaFetcher:
    def __init__(
        self,
        settings: Settings,
        manifest: Manifest,
        client: httpx.Client | None = None,
        throttle: Throttle | None = None,
    ) -> None:
        self.settings = settings
        self.manifest = manifest
        self.throttle = throttle or Throttle(settings.cdr.request_delay_s)
        self._client = client

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = polite_client(self.throttle, self.settings.user_agent)
        return self._client

    def fetch(self, years: list[int], by_state: bool = False) -> list[Outcome]:
        outcomes = []
        for year in years:
            for state in STATES if by_state else [None]:
                outcomes.append(self._one(year, state))
                if outcomes[-1].status == "failed":
                    return outcomes  # stop at the first failure; later files would fail too
        return outcomes

    def _one(self, year: int, state: str | None) -> Outcome:
        key = manifest_key(year, state)
        csv = raw_dir(self.settings) / f"{file_stem(year, state)}.csv"
        parquet = csv.with_suffix(".parquet")
        entry = self.manifest.get(key)
        if entry is not None and parquet.exists():
            rows = entry.meta.get("rows")
            return Outcome(year, state, "cached", parquet, rows)
        streamed = None
        if entry is None or not csv.exists():
            url, params = request_url(year, state)
            try:
                streamed = stream_csv(self.client, self.throttle, url, params, csv)
            except Exception as exc:
                log.debug("HMDA download failed", exc_info=True)
                return Outcome(year, state, "failed", detail=f"{type(exc).__name__}: {exc}")
            self.manifest.record(
                key,
                SOURCE,
                csv,
                url=str(httpx.URL(url, params=params)),
                period=str(year),
                meta={
                    "filters": FILTERS,
                    "state": state,
                    "seconds": round(streamed.seconds, 1),
                    "final_url": streamed.final_url,
                    "redirects": streamed.redirects,
                },
            )
        rows = csv_to_parquet(csv, parquet)
        entry = self.manifest.get(key)
        entry.meta["rows"] = rows
        self.manifest.add_derived(key, parquet)  # also saves the rows count
        if not self.settings.hmda.keep_raw_csv:
            csv.unlink()
        return Outcome(
            year, state, "downloaded" if streamed else "converted", parquet, rows, streamed
        )
