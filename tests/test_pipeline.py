import urllib.parse
from datetime import date
from pathlib import Path

import httpx
import pytest

from armreset.db import connect, relations
from armreset.fetch.facsimile import FacsimileError, fetch_facsimile, form_state
from armreset.ingest.cdr import MdrmSpec
from armreset.manifest import Manifest
from armreset.periods import Quarter
from armreset.pipeline import build, cdr_staging_path
from armreset.qa import read_sdf, report_path, spot_check, write_report
from armreset.settings import Settings, load_settings
from tests.cdr_fixtures import BANKS, make_cdr_zip

REPORT_DATE = date(2026, 6, 30)


def _record_zip(settings: Settings, banks: dict | None = None) -> Path:
    path = make_cdr_zip(settings.raw_dir / "cdr", banks=banks)
    Manifest.for_settings(settings).record("cdr:2026Q2", "cdr", path, period="2026-06-30")
    return path


@pytest.fixture
def settings(project: Path) -> Settings:
    return load_settings()


@pytest.fixture
def built(settings: Settings) -> Settings:
    _record_zip(settings)
    build(settings)
    return settings


def _counts(settings: Settings) -> dict[str, int]:
    con = connect(settings, read_only=True)
    try:
        return {
            name: con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name, _ in relations(con)
        }
    finally:
        con.close()


def test_build_writes_the_cdr_tables_and_views(built: Settings) -> None:
    assert _counts(built) == {
        "dim_bank": len(BANKS),
        "fact_cdr_repricing": len(BANKS),
        "qa_cdr_flags": 8,  # see test_repricing.test_qa_flags_per_bank
        "v_cdr_industry": 1,  # one quarter
        "v_cdr_bank_latest": len(BANKS),
    }


def test_build_is_idempotent(built: Settings) -> None:
    summary = build(built)
    assert summary.staged == []
    assert summary.reused == [cdr_staging_path(built, REPORT_DATE)]
    assert _counts(built)["fact_cdr_repricing"] == len(BANKS)


def test_build_restages_when_the_zip_changes(built: Settings) -> None:
    fewer = {k: v for k, v in BANKS.items() if k != "700"}
    _record_zip(built, banks=fewer)
    summary = build(built)
    assert [r.report_date for r in summary.staged] == [REPORT_DATE]
    assert _counts(built)["fact_cdr_repricing"] == len(fewer)


def test_staging_outlives_the_zip(built: Settings) -> None:
    manifest = Manifest.for_settings(built)
    staged = cdr_staging_path(built, REPORT_DATE)
    assert manifest.get("cdr:2026Q2").derived == [staged.relative_to(built.root).as_posix()]
    manifest.abspath(manifest.get("cdr:2026Q2").path).unlink()
    assert manifest.is_cached("cdr:2026Q2")  # so `fetch cdr` won't download it again
    summary = build(built)
    assert summary.reused == [staged] and summary.unusable == {}


def test_build_with_nothing_fetched(settings: Settings) -> None:
    summary = build(settings)
    assert summary.tables == {}
    assert not settings.warehouse_path.exists()


def test_validate_writes_the_report(built: Settings) -> None:
    path = write_report(built)
    assert path == report_path(built) == built.warehouse_path.parent / "qa_report.md"
    text = path.read_text()
    for expected in (
        "## Call Reports (CDR)",
        "| 2026-06-30 | 7 | 1 | 4 | 2 |",  # banks by form: 031, 041, 051
        "implied_nonaccrual_negative_rounding",
        "RCFD2170,RCON5367,RCONA564-A569,RCONC229",
    ):
        assert expected in text


SDF_HEADER = (
    "Call Date;Bank RSSD Identifier;MDRM #;Value;Last Update;Short Definition;"
    "Call Schedule;Line Number"
)


def _sdf(tmp_path: Path, rssd: str = "100", **overrides: str) -> Path:
    values = {
        "RCFD2170": ("1000", "Total balance sheet assets", "RCRII", "11"),
        "RCFD5367": ("600", "Secured by first liens", "RCCI", "1c2a"),
        "RCON5367": ("500", "Secured by first liens", "RCCI", "1c2a"),
        "RCONA564": ("10", "Three months or less", "RCCI", "M2a1"),
        "RCONA565": ("20", "Over three months through 12 months", "RCCI", "M2a2"),
        "RCONA566": ("30", "Over one year through three years", "RCCI", "M2a3"),
        "RCONA567": ("40", "Over three years through five years", "RCCI", "M2a4"),
        "RCONA568": ("100", "Over five years through 15 years", "RCCI", "M2a5"),
        "RCONA569": ("290", "Over 15 years", "RCCI", "M2a6"),
        "RCONC229": ("10", "Secured by first liens", "RCN", "1c2a"),
        "RCFA7205": ("16.2%", "Total capital ratio (Column A; see instructions)", "RCRI", "49"),
    }
    lines = [SDF_HEADER]
    for mdrm, (value, caption, schedule, line) in values.items():
        value = overrides.get(mdrm, value)
        lines.append(f"20260630;{rssd};{mdrm};{value};20260804;{caption};{schedule};{line}")
    path = tmp_path / "bank.sdf"
    path.write_text("\r\n".join(lines) + "\r\n")
    return path


def test_read_sdf_handles_semicolons_in_captions(tmp_path: Path) -> None:
    items = read_sdf(_sdf(tmp_path))
    assert items["RCFA7205"]["caption"] == "Total capital ratio (Column A; see instructions)"
    assert (items["RCFA7205"]["schedule"], items["RCFA7205"]["line"]) == ("RCRI", "49")
    assert items["RCONA568"]["value"] == "100"


def test_spot_check_matches_the_filed_report(built: Settings, tmp_path: Path) -> None:
    spec = MdrmSpec.load(built.config_file("mdrm.yaml"))
    con = connect(built, read_only=True)
    try:
        rows = spot_check(con, spec, 100, REPORT_DATE, read_sdf(_sdf(tmp_path)))
        assert all(r.match for r in rows)
        # The 031 filer's first-lien total is checked against RCON5367, not RCFD5367.
        first_lien = next(r for r in rows if r.column == "first_lien_total")
        assert (first_lien.mdrm, first_lien.filed_thousands) == ("RCON5367", "500")

        off = spot_check(con, spec, 100, REPORT_DATE, read_sdf(_sdf(tmp_path, RCONA568="101")))
        assert [r.column for r in off if not r.match] == ["b_5_15y"]
        with pytest.raises(ValueError, match="belongs to RSSD"):
            spot_check(con, spec, 100, REPORT_DATE, read_sdf(_sdf(tmp_path, rssd="999")))
    finally:
        con.close()


# The shape of CDR's ViewFacsimileDirect.aspx page (trimmed).
FACSIMILE_PAGE = "\n".join(
    [
        '<form method="post" action="ViewPDFFacsimile.aspx?ds=call&amp;idType=fdiccert&amp;'
        'id=628&amp;date=06302026" id="form1">',
        '<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="abc+/=" />',
        '<input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" '
        'value="A250BEAE" />',
        '<input type="submit" name="ctl00$MainContentHolder$viewTabStrip$Download_PDF_2" '
        'value="Download PDF" />',
        "</form>",
    ]
)


def test_form_state_reads_action_and_hidden_fields() -> None:
    action, fields = form_state(FACSIMILE_PAGE)
    assert action == "ViewPDFFacsimile.aspx?ds=call&idType=fdiccert&id=628&date=06302026"
    assert fields == {"__VIEWSTATE": "abc+/=", "__VIEWSTATEGENERATOR": "A250BEAE"}


def _cdr_handler(seen: list, pdf: bytes = b"%PDF-1.4 fake") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, text=FACSIMILE_PAGE)
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        assert form["__VIEWSTATE"] == "abc+/="
        if "ctl00$MainContentHolder$viewTabStrip$Download_PDF_2" in form:
            return httpx.Response(200, content=pdf)
        return httpx.Response(200, content=(SDF_HEADER + "\r\n").encode())

    return httpx.MockTransport(handler)


def test_fetch_facsimile_posts_back_once_per_format_then_caches(settings: Settings) -> None:
    seen: list = []
    client = httpx.Client(transport=_cdr_handler(seen))
    manifest = Manifest.for_settings(settings)
    paths = fetch_facsimile(settings, manifest, "628", Quarter(2026, 2), client=client)
    assert seen == [
        ("GET", "/Public/ViewFacsimileDirect.aspx"),
        ("POST", "/Public/ViewPDFFacsimile.aspx"),
        ("POST", "/Public/ViewPDFFacsimile.aspx"),
    ]
    assert paths["pdf"].read_bytes().startswith(b"%PDF")
    assert paths["sdf"].read_text().startswith("Call Date;")
    fetch_facsimile(settings, manifest, "628", Quarter(2026, 2), client=client)
    assert len(seen) == 3  # cached


def test_fetch_facsimile_rejects_a_page_instead_of_a_file(settings: Settings) -> None:
    client = httpx.Client(transport=_cdr_handler([], pdf=FACSIMILE_PAGE.encode()))
    with pytest.raises(FacsimileError, match="instead of the PDF"):
        fetch_facsimile(
            settings, Manifest.for_settings(settings), "628", Quarter(2026, 2), client=client
        )
