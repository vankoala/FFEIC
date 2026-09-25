"""HMDA Reporter Panel: lender identity and the LEI -> RSSD link (PLAN.md §5.3).

VERIFY result (docs/verification.md, item 7): the Snapshot page is a JavaScript app, but its
data-publication code lists the panel files on ``files.ffiec.cfpb.gov`` for 2018-2023. For
2024 and later it says "The Reporter Panel is no longer being produced" and points to the
Philadelphia Fed's HMDA Lender File. For those years the fetcher saves the names-only filers
list from the Data Browser API, and a panel file placed by hand in ``data/raw/hmda_panel/``
is recorded like a download.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from armreset.http import Throttle, polite_client
from armreset.manifest import Manifest
from armreset.settings import Settings

log = logging.getLogger(__name__)

SOURCE = "hmda_panel"
FILERS_SOURCE = "hmda_filers"
FILE_SERVER = "https://files.ffiec.cfpb.gov"
FILERS_API = "https://ffiec.cfpb.gov/v2/data-browser-api/view/filers"
PANEL_YEARS = range(2018, 2024)  # Snapshot panel files listed by the Data Publication site
LENDER_FILE_URL = (
    "https://www.philadelphiafed.org/surveys-and-data/consumer-finance-data/"
    "home-mortgage-disclosure-act-lender-file"
)
REQUIRED_COLUMNS = ("lei", "respondent_rssd", "agency_code", "other_lender_code")


class PanelError(RuntimeError):
    pass


def panel_dir(settings: Settings) -> Path:
    return settings.raw_dir / "hmda_panel"


def panel_url(year: int) -> str:
    return f"{FILE_SERVER}/static-data/snapshot/{year}/{year}_public_panel_csv.zip"


def panel_path(settings: Settings, year: int) -> Path:
    return panel_dir(settings) / f"{year}_public_panel_csv.zip"


def filers_path(settings: Settings, year: int) -> Path:
    return panel_dir(settings) / f"filers_{year}.json"


def panel_header(path: Path) -> list[str]:
    """Column names of the panel CSV inside the zip (or of a bare CSV)."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            csvs = [n for n in zf.namelist() if n.lower().endswith((".csv", ".txt"))]
            if len(csvs) != 1:
                raise PanelError(f"{path.name}: expected one CSV inside, found {csvs}")
            with zf.open(csvs[0]) as f:
                first = io.TextIOWrapper(f, encoding="utf-8", errors="replace").readline()
    else:
        with path.open(encoding="utf-8", errors="replace") as f:
            first = f.readline()
    delimiter = "|" if first.count("|") > first.count(",") else ","
    return [c.strip().strip('"').lower() for c in first.rstrip("\r\n").split(delimiter)]


def check_panel(path: Path) -> list[str]:
    columns = panel_header(path)
    if missing := [c for c in REQUIRED_COLUMNS if c not in columns]:
        raise PanelError(f"{path.name} lacks panel columns {missing}; found {columns[:8]}")
    return columns


@dataclass
class Outcome:
    year: int
    status: str  # cached | downloaded | recorded | names only | failed
    path: Path | None = None
    detail: str = ""


def fetch_panels(
    settings: Settings,
    manifest: Manifest,
    years: list[int],
    client: httpx.Client | None = None,
) -> list[Outcome]:
    throttle = Throttle(settings.cdr.request_delay_s)
    own_client = client is None
    client = client or polite_client(throttle, settings.user_agent)
    outcomes = []
    try:
        for year in years:
            outcomes.append(_fetch_year(settings, manifest, year, client, throttle))
    finally:
        if own_client:
            client.close()
    return outcomes


def _fetch_year(
    settings: Settings, manifest: Manifest, year: int, client: httpx.Client, throttle: Throttle
) -> Outcome:
    key = f"{SOURCE}:{year}"
    path = panel_path(settings, year)
    if manifest.is_cached(key):
        return Outcome(year, "cached", manifest.abspath(manifest.get(key).path))
    placed = sorted(panel_dir(settings).glob(f"{year}*panel*"))
    placed = [p for p in placed if not p.name.endswith(".part")]
    if placed:
        check_panel(placed[0])
        manifest.record(key, SOURCE, placed[0], period=str(year), meta={"origin": "placed by hand"})
        return Outcome(year, "recorded", placed[0], "panel file placed by hand")
    if year in PANEL_YEARS:
        try:
            _download(client, throttle, panel_url(year), path)
            check_panel(path)
        except Exception as exc:
            log.debug("panel download failed", exc_info=True)
            path.unlink(missing_ok=True)
            return Outcome(year, "failed", detail=f"{type(exc).__name__}: {exc}")
        manifest.record(key, SOURCE, path, url=panel_url(year), period=str(year))
        return Outcome(year, "downloaded", path)
    return _names_only(settings, manifest, year, client)


def _names_only(settings: Settings, manifest: Manifest, year: int, client: httpx.Client) -> Outcome:
    key = f"{FILERS_SOURCE}:{year}"
    note = (
        f"No Reporter Panel is published for {year}, so there is no LEI-RSSD link. Saved the "
        f"names-only filers list; the Philadelphia Fed HMDA Lender File ({LENDER_FILE_URL}) "
        f"can be placed in {panel_dir(settings)}/ as {year}_*panel*.csv if it has the panel "
        "columns."
    )
    if manifest.is_cached(key):
        return Outcome(year, "names only", manifest.abspath(manifest.get(key).path), note)
    response = client.get(FILERS_API, params={"years": str(year)})
    response.raise_for_status()
    institutions = response.json().get("institutions")
    if not isinstance(institutions, list):
        raise PanelError(f"Unexpected filers response for {year}: {response.text[:200]}")
    path = filers_path(settings, year)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(institutions, indent=0), encoding="utf-8")
    manifest.record(key, FILERS_SOURCE, path, url=str(response.url), period=str(year))
    return Outcome(year, "names only", path, note)


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
        raise PanelError(f"{url} did not return a zip file")
    tmp.replace(out)
