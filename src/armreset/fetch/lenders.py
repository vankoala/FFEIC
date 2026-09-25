"""Philadelphia Fed HMDA Lender File: lender identity, type and the LEI -> RSSD link.

It replaces the CFPB Reporter Panel as the lender source for every year, because the panel
stops at 2023 (docs/verification.md). One workbook covers every HMDA filer from 2018 to the
latest year, with one row per ``(YEAR, LEI)``. The Fed replaces the file each July, at the
same URL, when it adds a year. A recorded copy is never downloaded again; to pick up a new
release, delete the file and its manifest entry, then fetch again.
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path

import fastexcel
import httpx

from armreset.http import Throttle, polite_client
from armreset.manifest import Manifest
from armreset.settings import Settings

log = logging.getLogger(__name__)

SOURCE = "philfed_lender"
KEY = f"{SOURCE}:hmda-2018-present"
URL = (
    "https://www.philadelphiafed.org/-/media/FRBP/Assets/Surveys-And-Data/hmda/"
    "hmda-2018-present.xlsx"
)
PAGE_URL = (
    "https://www.philadelphiafed.org/surveys-and-data/consumer-finance-data/"
    "home-mortgage-disclosure-act-lender-file"
)
REQUIRED_COLUMNS = ("YEAR", "LEI", "NAMET", "CODE", "TYPE", "RSSD", "RSSDP", "RSSDHH")


class LenderFileError(RuntimeError):
    pass


def lender_dir(settings: Settings) -> Path:
    return settings.raw_dir / SOURCE


def lender_file_path(settings: Settings) -> Path:
    return lender_dir(settings) / "hmda-2018-present.xlsx"


def lender_sheet(path: Path) -> str:
    """Name of the sheet that holds the lender columns (``beta3`` in the 2026 release)."""
    if not zipfile.is_zipfile(path):
        raise LenderFileError(f"{path.name} is not an .xlsx workbook")
    reader = fastexcel.read_excel(path)
    for name in reader.sheet_names:
        columns = [c.name for c in reader.load_sheet(name, n_rows=0).available_columns()]
        if all(c in columns for c in REQUIRED_COLUMNS):
            return name
    raise LenderFileError(
        f"{path.name}: no sheet has the lender columns {list(REQUIRED_COLUMNS)} "
        f"(sheets: {reader.sheet_names})"
    )


def lender_years(path: Path) -> list[int]:
    """The reporting years the file covers. Also checks the sheet and its columns."""
    sheet = fastexcel.read_excel(path).load_sheet(
        lender_sheet(path), use_columns=["YEAR"], dtypes="string"
    )
    years = sorted({int(float(y)) for y in sheet.to_polars()["YEAR"].drop_nulls()})
    if not years:
        raise LenderFileError(f"{path.name} has no rows")
    return years


@dataclass
class Outcome:
    status: str  # cached | downloaded | recorded | failed
    path: Path | None = None
    detail: str = ""


def fetch_lender_file(
    settings: Settings, manifest: Manifest, client: httpx.Client | None = None
) -> Outcome:
    path = lender_file_path(settings)
    if manifest.is_cached(KEY):
        cached = manifest.abspath(manifest.get(KEY).path)
        years, error = _check(cached)
        if error:
            return Outcome("failed", cached, f"{error}. {_KEPT.format(path=cached)}")
        return Outcome("cached", cached, f"covers {_period(years)}")
    placed = [p for p in sorted(lender_dir(settings).glob("*.xlsx")) if p.is_file()]
    if placed:
        years, error = _check(placed[0])
        if error:
            return Outcome("failed", placed[0], f"{error}. Not recorded; replace the file.")
        manifest.record(
            KEY, SOURCE, placed[0], period=_period(years), meta={"origin": "placed by hand"}
        )
        return Outcome("recorded", placed[0], f"placed by hand; covers {_period(years)}")

    throttle = Throttle(settings.cdr.request_delay_s)
    own_client = client is None
    client = client or polite_client(throttle, settings.user_agent)
    try:
        _download(client, throttle, URL, path)
    except Exception as exc:
        log.debug("lender file download failed", exc_info=True)
        return Outcome("failed", detail=f"{type(exc).__name__}: {exc}")
    finally:
        if own_client:
            client.close()
    # Recorded even when the check fails, so fixing the check never means downloading again.
    years, error = _check(path)
    if error:
        manifest.record(KEY, SOURCE, path, url=URL, meta={"check": f"failed: {error}"})
        return Outcome("failed", path, f"{error}. {_KEPT.format(path=path)}")
    manifest.record(KEY, SOURCE, path, url=URL, period=_period(years))
    return Outcome("downloaded", path, f"covers {_period(years)}")


_KEPT = "The file stays recorded at {path}; it is not downloaded again."


def _check(path: Path) -> tuple[list[int], str | None]:
    """The years a file covers, or why it can't be used. A failing file is never deleted."""
    try:
        return lender_years(path), None
    except (LenderFileError, fastexcel.FastExcelError, ValueError) as exc:
        return [], str(exc).splitlines()[0]


def _period(years: list[int]) -> str:
    return str(years[0]) if years[0] == years[-1] else f"{years[0]}-{years[-1]}"


def _download(client: httpx.Client, throttle: Throttle, url: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in response.iter_bytes(1 << 20):
                    f.write(chunk)
    finally:
        throttle.mark()
    if not zipfile.is_zipfile(tmp):
        tmp.unlink()
        raise LenderFileError(f"{url} did not return an .xlsx workbook")
    tmp.replace(out)
