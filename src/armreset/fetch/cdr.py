"""CDR bulk Call Report download (PLAN.md §5.1).

Product "Call Reports -- Single Period" in the "Tab Delimited" format: one zip per quarter,
saved unchanged in ``data/raw/cdr/``. Downloads go through ``ffiec-data-collector`` with our
throttled session. If that fails, the user downloads the zip by hand into the same directory
and re-runs; the fetcher then records the hand-placed file instead.
"""

from __future__ import annotations

import logging
import os
import zipfile
from dataclasses import dataclass
from datetime import date
from importlib.metadata import version
from pathlib import Path
from typing import Protocol

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from armreset.http import PoliteSession, Throttle
from armreset.manifest import Manifest
from armreset.periods import CDR_POSTING_LAG_DAYS, Quarter, latest_published_quarter
from armreset.settings import Settings

log = logging.getLogger(__name__)

SOURCE = "cdr"
BULK_PAGE_URL = "https://cdr.ffiec.gov/public/PWS/DownloadBulkData.aspx"
PRODUCT_LABEL = "Call Reports -- Single Period"
FORMAT_LABEL = "Tab Delimited"


class CdrFetchError(RuntimeError):
    """The automated download failed, or a zip on disk is not a usable CDR bulk file."""


def manifest_key(quarter: Quarter) -> str:
    return f"{SOURCE}:{quarter.label}"


def cdr_stamp(quarter: Quarter) -> str:
    """The quarter-end date as MMDDYYYY, the format CDR uses in file names."""
    return quarter.end_date.strftime("%m%d%Y")


def zip_dir(settings: Settings) -> Path:
    return settings.raw_dir / "cdr"


def manual_instructions(quarters: list[Quarter], directory: Path) -> str:
    listed = ", ".join(f"{q} ({q.end_date:%m/%d/%Y})" for q in quarters)
    return (
        f"Download these quarters by hand: {listed}\n"
        f"  1. Open {BULK_PAGE_URL}\n"
        f'  2. Choose "{PRODUCT_LABEL}", the period, and "{FORMAT_LABEL}", then Download.\n'
        f"  3. Save each zip unchanged into {directory}/\n"
        "  4. Re-run `armtool fetch cdr` to record them in the manifest."
    )


def find_zip(directory: Path, quarter: Quarter) -> Path | None:
    """A zip already in ``directory`` whose name carries the quarter's MMDDYYYY stamp."""
    matches = sorted(p for p in directory.glob("*.zip") if cdr_stamp(quarter) in p.name)
    if len(matches) > 1:
        names = ", ".join(p.name for p in matches)
        raise CdrFetchError(f"More than one zip for {quarter} in {directory}: {names}")
    return matches[0] if matches else None


def check_zip(path: Path, quarter: Quarter) -> list[str]:
    """Confirm ``path`` is an intact zip holding the quarter's files; return member names."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            corrupt = zf.testzip()
    except zipfile.BadZipFile as exc:
        raise CdrFetchError(f"{path.name} is not a valid zip file: {exc}") from exc
    if corrupt is not None:
        raise CdrFetchError(f"{path.name} is damaged (bad CRC in {corrupt}); delete it and re-run")
    if not any(cdr_stamp(quarter) in name for name in names):
        raise CdrFetchError(f"{path.name} holds no files dated {cdr_stamp(quarter)} ({quarter})")
    return names


@dataclass
class Downloaded:
    path: Path
    published: date | None  # CDR's "Call Updated" date when the file was downloaded


class BulkSource(Protocol):
    def available_quarters(self) -> list[Quarter]: ...

    def download(self, quarter: Quarter) -> Downloaded: ...


class CollectorSource:
    """``ffiec-data-collector`` driven through a :class:`PoliteSession`.

    The library makes 4-6 requests per download (page, product, period, format, download).
    The swapped-in session puts every one of them through the throttle and replaces the
    library's browser User-Agent with ours. Downloads land in ``work_dir`` first.
    """

    def __init__(self, throttle: Throttle, user_agent: str, work_dir: Path) -> None:
        from ffiec_data_collector import FFIECDownloader

        self._dl = FFIECDownloader(download_dir=work_dir)
        session = PoliteSession(throttle, user_agent)
        for name, value in self._dl.session.headers.items():
            if name.lower() != "user-agent":
                session.headers[name] = value
        self._dl.session = session
        self._listed: list[Quarter] | None = None

    def available_quarters(self) -> list[Quarter]:
        if self._listed is None:
            from ffiec_data_collector import Product

            listed = []
            for period in self._dl.select_product(Product.CALL_SINGLE):
                day = period.date.date()
                quarter = Quarter.containing(day)
                if quarter.end_date == day:
                    listed.append(quarter)
                else:
                    log.warning("Ignoring CDR period %s: not a quarter-end", period.date_str)
            self._listed = sorted(listed)
        return self._listed

    def download(self, quarter: Quarter) -> Downloaded:
        from ffiec_data_collector import FileFormat, Product

        try:
            result = self._dl.download(
                Product.CALL_SINGLE, quarter.end_date.strftime("%Y%m%d"), FileFormat.TSV
            )
        except Exception:
            self._dl._viewstate = None  # a retry starts again from a fresh page load
            raise
        if not result.success or result.file_path is None:
            raise CdrFetchError(result.error_message or "CDR returned no file")
        return Downloaded(Path(result.file_path), result.call_updated)


def _transient(exc: BaseException) -> bool:
    if isinstance(exc, requests.ConnectionError | requests.Timeout):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code >= 500
    return False


_with_retries = retry(
    retry=retry_if_exception(_transient),
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=10, max=120),
    reraise=True,
)


@dataclass
class Outcome:
    quarter: Quarter
    status: str  # cached | recorded | downloaded | not listed | failed | skipped
    path: Path | None = None
    detail: str = ""


class CdrFetcher:
    def __init__(
        self,
        settings: Settings,
        manifest: Manifest,
        source: BulkSource | None = None,
        throttle: Throttle | None = None,
    ) -> None:
        self.settings = settings
        self.manifest = manifest
        self.zip_dir = zip_dir(settings)
        self._source = source
        self._throttle = throttle or Throttle(settings.cdr.request_delay_s)

    @property
    def source(self) -> BulkSource:
        # Created on first use, so runs that only touch cached files make no requests.
        if self._source is None:
            work_dir = self.zip_dir / ".partial"
            self._source = CollectorSource(self._throttle, self.settings.user_agent, work_dir)
        return self._source

    def latest_quarter(self) -> tuple[Quarter, str]:
        """The newest quarter CDR lists, or the posting-lag guess when CDR can't be reached."""
        try:
            listed = self.source.available_quarters()
        except Exception as exc:
            guess = latest_published_quarter()
            log.warning(
                "Could not list CDR periods (%s); assuming %s from the %d-day posting lag",
                exc,
                guess,
                CDR_POSTING_LAG_DAYS,
            )
            return guess, f"guessed from the {CDR_POSTING_LAG_DAYS}-day posting lag"
        if not listed:
            raise CdrFetchError(f'CDR lists no periods for "{PRODUCT_LABEL}"')
        return listed[-1], "newest period listed by CDR"

    def fetch(self, quarters: list[Quarter]) -> list[Outcome]:
        self.zip_dir.mkdir(parents=True, exist_ok=True)
        outcomes: list[Outcome] = []
        network_failed = False
        for quarter in quarters:
            key = manifest_key(quarter)
            if self.manifest.is_cached(key):
                entry = self.manifest.get(key)
                outcomes.append(Outcome(quarter, "cached", self.manifest.abspath(entry.path)))
                continue
            placed = find_zip(self.zip_dir, quarter)
            if placed is not None:
                check_zip(placed, quarter)
                self.manifest.record(
                    key,
                    SOURCE,
                    placed,
                    period=quarter.end_date.isoformat(),
                    meta={"origin": "placed by hand", "product": PRODUCT_LABEL},
                )
                outcomes.append(Outcome(quarter, "recorded", placed, "zip placed by hand"))
                continue
            if network_failed:
                outcomes.append(Outcome(quarter, "skipped", detail="after an earlier failure"))
                continue
            try:
                if quarter not in self.source.available_quarters():
                    outcomes.append(Outcome(quarter, "not listed", detail="CDR has no such period"))
                    continue
                path = self._download(quarter)
            except Exception as exc:
                log.debug("CDR download failed", exc_info=True)
                network_failed = True
                outcomes.append(Outcome(quarter, "failed", detail=f"{type(exc).__name__}: {exc}"))
                continue
            outcomes.append(Outcome(quarter, "downloaded", path))
        return outcomes

    def _download(self, quarter: Quarter) -> Path:
        got = _with_retries(self.source.download)(quarter)
        check_zip(got.path, quarter)
        final = self.zip_dir / got.path.name
        os.replace(got.path, final)
        self.manifest.record(
            manifest_key(quarter),
            SOURCE,
            final,
            url=BULK_PAGE_URL,
            period=quarter.end_date.isoformat(),
            published=got.published.isoformat() if got.published else None,
            meta={
                "product": PRODUCT_LABEL,
                "format": FORMAT_LABEL,
                "via": f"ffiec-data-collector {version('ffiec-data-collector')}",
            },
        )
        return final
