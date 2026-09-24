"""One bank's Call Report from CDR, as PDF and SDF, for spot checks (PLAN.md §11).

CDR's direct link ``ViewFacsimileDirect.aspx?ds=call&idType=fdiccert&id=<cert>&date=<MMDDYYYY>``
returns an ASP.NET page, not the file. Its "Download PDF" and "Download SDF" buttons post the
page's form state back for the file. The SDF is the same report as semicolon-delimited text:
one row per MDRM item with its value (thousands of dollars), caption, schedule and line.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from urllib.parse import urljoin

import httpx

from armreset.fetch.cdr import cdr_stamp
from armreset.http import Throttle, polite_client
from armreset.manifest import Manifest
from armreset.periods import Quarter
from armreset.settings import Settings

SOURCE = "cdr_facsimile"
DIRECT_URL = "https://cdr.ffiec.gov/Public/ViewFacsimileDirect.aspx"
BUTTONS = {"pdf": ("Download_PDF_2", "Download PDF"), "sdf": ("Download_SDF_3", "Download SDF")}
SIGNATURES = {"pdf": b"%PDF", "sdf": b"Call Date;"}

_FORM_RE = re.compile(r'<form[^>]*\baction="([^"]+)"', re.I)
_HIDDEN_RE = re.compile(r'<input[^>]*type="hidden"[^>]*>', re.I)
_ATTR_RE = re.compile(r'(name|value)="([^"]*)"', re.I)


class FacsimileError(RuntimeError):
    pass


def facsimile_path(settings: Settings, fdic_cert: str, quarter: Quarter, fmt: str) -> Path:
    return settings.raw_dir / SOURCE / f"call_cert{fdic_cert}_{cdr_stamp(quarter)}.{fmt}"


def manifest_key(fdic_cert: str, quarter: Quarter, fmt: str) -> str:
    return f"{SOURCE}:{fdic_cert}:{quarter.label}:{fmt}"


def form_state(page: str) -> tuple[str, dict[str, str]]:
    """The form's action URL and its hidden fields (__VIEWSTATE and friends)."""
    action = _FORM_RE.search(page)
    if action is None:
        raise FacsimileError("CDR facsimile page has no form; the page layout may have changed")
    fields = {}
    for tag in _HIDDEN_RE.findall(page):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
        if "name" in attrs:
            fields[attrs["name"]] = html.unescape(attrs.get("value", ""))
    return html.unescape(action[1]), fields


def fetch_facsimile(
    settings: Settings,
    manifest: Manifest,
    fdic_cert: str,
    quarter: Quarter,
    formats: tuple[str, ...] = ("pdf", "sdf"),
    client: httpx.Client | None = None,
) -> dict[str, Path]:
    """Download (or reuse) the bank's report in each format; returns format -> path."""
    paths = {fmt: facsimile_path(settings, fdic_cert, quarter, fmt) for fmt in formats}
    todo = [fmt for fmt in formats if not manifest.is_cached(manifest_key(fdic_cert, quarter, fmt))]
    if not todo:
        return paths
    params = {"ds": "call", "idType": "fdiccert", "id": fdic_cert, "date": cdr_stamp(quarter)}
    own_client = client is None
    client = client or polite_client(Throttle(settings.cdr.request_delay_s), settings.user_agent)
    try:
        page = client.get(DIRECT_URL, params=params)
        page.raise_for_status()
        action, fields = form_state(page.text)
        for fmt in todo:
            name, label = BUTTONS[fmt]
            response = client.post(
                urljoin(str(page.url), action),
                data={**fields, f"ctl00$MainContentHolder$viewTabStrip${name}": label},
            )
            response.raise_for_status()
            if not response.content.startswith(SIGNATURES[fmt]):
                raise FacsimileError(
                    f"CDR returned {response.headers.get('content-type')} instead of the "
                    f"{fmt.upper()} for FDIC cert {fdic_cert}, {quarter}; check the cert and date"
                )
            path = paths[fmt]
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".part")
            tmp.write_bytes(response.content)
            tmp.replace(path)
            manifest.record(
                manifest_key(fdic_cert, quarter, fmt),
                SOURCE,
                path,
                url=str(page.url),
                period=quarter.end_date.isoformat(),
                meta={"fdic_cert": fdic_cert, "format": fmt.upper()},
            )
    finally:
        if own_client:
            client.close()
    return paths
