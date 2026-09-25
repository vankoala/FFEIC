from pathlib import Path

import httpx
import polars as pl
import pytest

from armreset.fetch.lenders import (
    KEY,
    URL,
    LenderFileError,
    fetch_lender_file,
    lender_file_path,
    lender_sheet,
    lender_years,
)
from armreset.ingest.lenders import build_dim_hmda_lender, lenders_from_file, read_lender_file
from armreset.manifest import Manifest
from armreset.segments import Segments
from armreset.settings import Settings, load_settings
from tests.conftest import REPO_ROOT
from tests.hmda_fixtures import (
    BANK_LEI,
    CU_LEI,
    IMC_LEI,
    LENDER_ROWS,
    lender_file_frame,
    write_lender_file,
)

AFFILIATE_LEI = LENDER_ROWS[3][0]


@pytest.fixture
def segments() -> Segments:
    return Segments.load(REPO_ROOT / "config" / "segments.yaml")


@pytest.fixture
def settings(project: Path) -> Settings:
    return load_settings()


@pytest.fixture
def workbook(tmp_path: Path) -> Path:
    return write_lender_file(tmp_path / "lenders.xlsx")


def _call_reports(*rssds: int, year: int = 2021) -> pl.DataFrame:
    return pl.DataFrame({"activity_year": [year] * len(rssds), "rssd_id": list(rssds)})


def test_the_lender_sheet_is_found_by_its_columns(tmp_path: Path) -> None:
    path = write_lender_file(tmp_path / "l.xlsx", sheet="beta4", notes=True)
    assert lender_sheet(path) == "beta4"
    assert lender_years(path) == [2021]
    assert read_lender_file(path)["YEAR"].to_list() == ["2021"] * len(LENDER_ROWS)


def test_a_workbook_without_the_lender_columns_is_refused(tmp_path: Path) -> None:
    path = write_lender_file(tmp_path / "l.xlsx", frame=pl.DataFrame({"YEAR": [2021]}))
    with pytest.raises(LenderFileError, match="no sheet has the lender columns"):
        lender_sheet(path)
    html = tmp_path / "error.xlsx"
    html.write_text("<html>error</html>")
    with pytest.raises(LenderFileError, match=r"not an \.xlsx"):
        lender_sheet(html)


def test_lenders_from_file(workbook: Path, segments: Segments) -> None:
    dim = lenders_from_file(read_lender_file(workbook), segments, _call_reports(100))
    rows = {r["lei"]: r for r in dim.iter_rows(named=True)}
    assert {lei: r["lender_type"] for lei, r in rows.items()} == {
        BANK_LEI: "bank",
        CU_LEI: "credit_union",
        IMC_LEI: "independent_mortgage_company",
        AFFILIATE_LEI: "bank_affiliate",
    }
    bank = rows[BANK_LEI]
    assert (bank["respondent_rssd"], bank["parent_rssd"], bank["top_holder_rssd"]) == (
        100,
        1000,
        1000,
    )
    assert (bank["agency_code"], bank["institution_type"], bank["name"]) == (
        1,
        10,
        "BIG BANK, N.A.",
    )
    imc = rows[IMC_LEI]
    assert (imc["respondent_rssd"], imc["parent_rssd"], imc["top_holder_rssd"]) == (
        None,
        None,
        None,
    )  # 0 means none
    assert [rows[lei]["in_call_reports"] for lei in (BANK_LEI, CU_LEI, IMC_LEI)] == [
        True,
        False,
        False,
    ]
    assert dim["activity_year"].dtype == pl.Int32


def test_a_call_report_filer_is_a_bank_whatever_its_code(
    workbook: Path, segments: Segments
) -> None:
    """The real file codes some small banks 30 (credit union); their RSSD files a Call Report."""
    dim = lenders_from_file(read_lender_file(workbook), segments, _call_reports(100, 555))
    row = dim.filter(pl.col("lei") == CU_LEI).row(0, named=True)
    assert (row["lender_type"], row["institution_type"]) == ("bank", 30)


def test_without_call_reports_the_code_decides(workbook: Path, segments: Segments) -> None:
    dim = lenders_from_file(read_lender_file(workbook), segments, None)
    assert dim["in_call_reports"].null_count() == dim.height
    assert dim.filter(pl.col("lei") == CU_LEI)["lender_type"].to_list() == ["credit_union"]
    # Call Reports loaded for another year only: still unknown for 2021, so no override.
    dim = lenders_from_file(read_lender_file(workbook), segments, _call_reports(555, year=2020))
    assert dim["in_call_reports"].null_count() == dim.height
    assert dim.filter(pl.col("lei") == CU_LEI)["lender_type"].to_list() == ["credit_union"]


def test_an_unlisted_code_is_unknown(tmp_path: Path, segments: Segments) -> None:
    rows = [(BANK_LEI, 1, 99, 100, 0, "ODD LENDER")]
    path = write_lender_file(tmp_path / "l.xlsx", lender_file_frame(rows))
    dim = lenders_from_file(read_lender_file(path), segments, None)
    assert dim["lender_type"].to_list() == ["unknown"]


def test_repeated_year_and_lei_is_refused(tmp_path: Path, segments: Segments) -> None:
    frame = pl.concat([lender_file_frame(), lender_file_frame(LENDER_ROWS[:1])])
    path = write_lender_file(tmp_path / "l.xlsx", frame)
    with pytest.raises(ValueError, match="repeated"):
        lenders_from_file(read_lender_file(path), segments, None)


def test_several_years_in_one_file(tmp_path: Path, segments: Segments) -> None:
    frame = pl.concat([lender_file_frame(year=2024), lender_file_frame(year=2025)])
    path = write_lender_file(tmp_path / "l.xlsx", frame)
    assert lender_years(path) == [2024, 2025]
    dim = lenders_from_file(read_lender_file(path), segments, _call_reports(100, year=2025))
    assert dim.group_by("activity_year").len().sort("activity_year").rows() == [
        (2024, 4),
        (2025, 4),
    ]
    bank = dim.filter(pl.col("lei") == BANK_LEI).sort("activity_year")
    assert bank["in_call_reports"].to_list() == [None, True]  # no 2024 Call Reports loaded


def test_institution_types_config(segments: Segments) -> None:
    table = segments.institution_type_table()
    assert table["institution_type"].is_unique().all()
    types = dict(zip(table["institution_type"], table["lender_type"], strict=True))
    assert {types[c] for c in (10, 13, 14, 20, 23)} == {"bank"}
    assert {types[c] for c in (11, 12, 21, 22, 41)} == {"bank_affiliate"}
    assert {types[c] for c in (30, 31, 32, 33)} == {"credit_union"}
    assert types[40] == "independent_mortgage_company"
    assert segments.call_report_filer_type == "bank"


def test_a_code_in_two_lender_types_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "segments.yaml"
    bad.write_text(
        "holder_segments:\n  a: {label: A, purchaser_types: [0]}\n"
        "lender_types:\n  bank: {10: commercial bank}\n  credit_union: {10: credit union}\n"
    )
    with pytest.raises(ValueError, match="institution type 10"):
        Segments.load(bad)


def _server(seen: list[str], body: bytes) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_downloads_once(settings: Settings, workbook: Path, segments: Segments) -> None:
    seen: list[str] = []
    client = _server(seen, workbook.read_bytes())
    outcome = fetch_lender_file(settings, Manifest.for_settings(settings), client=client)
    assert (outcome.status, outcome.detail) == ("downloaded", "covers 2021")
    assert seen == [URL]
    entry = Manifest.for_settings(settings).get(KEY)
    assert (entry.period, entry.url) == ("2021", URL)

    again = fetch_lender_file(settings, Manifest.for_settings(settings), client=client)
    assert again.status == "cached" and len(seen) == 1  # never downloaded twice
    dim = build_dim_hmda_lender(Manifest.for_settings(settings), segments)
    assert dim is not None and dim.height == len(LENDER_ROWS)


def test_fetch_rejects_a_page_that_is_not_a_workbook(settings: Settings) -> None:
    client = _server([], b"<html>Access denied</html>")
    outcome = fetch_lender_file(settings, Manifest.for_settings(settings), client=client)
    assert outcome.status == "failed" and "not return an .xlsx" in outcome.detail
    assert not lender_file_path(settings).exists()
    assert KEY not in Manifest.for_settings(settings)


def test_a_download_that_fails_the_check_is_kept(settings: Settings, tmp_path: Path) -> None:
    seen: list[str] = []
    odd = write_lender_file(tmp_path / "odd.xlsx", frame=pl.DataFrame({"YEAR": [2021]}))
    client = _server(seen, odd.read_bytes())
    outcome = fetch_lender_file(settings, Manifest.for_settings(settings), client=client)
    assert outcome.status == "failed" and "not downloaded again" in outcome.detail
    assert lender_file_path(settings).exists()
    assert "check" in Manifest.for_settings(settings).get(KEY).meta

    again = fetch_lender_file(settings, Manifest.for_settings(settings), client=client)
    assert again.status == "failed" and len(seen) == 1


def test_a_hand_placed_file_is_recorded(settings: Settings) -> None:
    placed = write_lender_file(settings.raw_dir / "philfed_lender" / "hmda-2018-present.xlsx")
    outcome = fetch_lender_file(settings, Manifest.for_settings(settings), client=None)
    assert (outcome.status, outcome.path) == ("recorded", placed)
    entry = Manifest.for_settings(settings).get(KEY)
    assert entry.meta == {"origin": "placed by hand"} and entry.period == "2021"


def test_a_hand_placed_file_that_fails_the_check_is_not_recorded(settings: Settings) -> None:
    placed = settings.raw_dir / "philfed_lender" / "lenders.xlsx"
    write_lender_file(placed, frame=pl.DataFrame({"YEAR": [2021]}))
    outcome = fetch_lender_file(settings, Manifest.for_settings(settings), client=None)
    assert outcome.status == "failed" and "replace the file" in outcome.detail
    assert KEY not in Manifest.for_settings(settings)


def test_no_lender_file_means_no_table(settings: Settings, segments: Segments) -> None:
    assert build_dim_hmda_lender(Manifest.for_settings(settings), segments) is None
