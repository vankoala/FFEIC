import io
import json
import zipfile
from pathlib import Path

import httpx
import polars as pl
import pytest

from armreset.fetch.panel import (
    FILERS_API,
    PANEL_YEARS,
    PanelError,
    check_panel,
    fetch_panels,
    panel_url,
)
from armreset.ingest.panel import (
    build_dim_hmda_lender,
    lenders_from_filers,
    lenders_from_panel,
    read_panel,
)
from armreset.manifest import Manifest
from armreset.segments import Segments
from armreset.settings import Settings, load_settings
from tests.conftest import REPO_ROOT
from tests.hmda_fixtures import PANEL_ROWS, panel_csv


@pytest.fixture
def segments() -> Segments:
    return Segments.load(REPO_ROOT / "config" / "segments.yaml")


@pytest.fixture
def settings(project: Path) -> Settings:
    return load_settings()


def _zip(text: str, name: str = "2021_public_panel.csv") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(name, text)
    return buffer.getvalue()


@pytest.fixture
def panel(tmp_path: Path):
    """Write rows as a zipped panel CSV and read it back with read_panel."""

    def make(rows) -> pl.DataFrame:
        path = tmp_path / "panel.zip"
        path.write_bytes(_zip(panel_csv(rows)))
        return read_panel(path)

    return make


def test_read_panel_renames_topholder_columns(panel, tmp_path: Path) -> None:
    df = panel(PANEL_ROWS)
    assert {"top_holder_rssd", "top_holder_name"} <= set(df.columns)
    assert "topholder_rssd" not in df.columns
    pipe = tmp_path / "2021_panel.txt"
    pipe.write_text(panel_csv(PANEL_ROWS).replace(",", "|"))
    assert read_panel(pipe)["lei"].to_list() == [r[0] for r in PANEL_ROWS]


# (lei, agency, rssd, name, other_lender_code), in Call Reports?, expected lender_type
RULE_CASES = [
    (("L1", "1", "100", "Big Bank", "0"), True, "bank"),
    (("L2", "9", "200", "Flagstar Bank", "0"), True, "bank"),  # CFPB-supervised bank
    (("L3", "3", "300", "Paducah Bank", "3"), True, "bank"),  # bank with an affiliate code
    (("L4", "5", "400", "Kitsap Credit Union", "0"), False, "credit_union"),
    (("L5", "9", "500", "Navy Federal Credit Union", "-1"), False, "credit_union"),
    (("L6", "9", "600", "THE GOLDEN 1", "0"), False, "credit_union"),  # CFPB depository, no CR
    (("L7", "2", "-1", "PrimeLending", "1"), False, "bank_affiliate"),
    (("L8", "7", "-1", "Rocket-ish Mortgage", "3"), False, "independent_mortgage_company"),
    (("L9", "7", "-1", "Smartfi Home Loans", "-1"), False, "independent_mortgage_company"),
    (("L10", "9", "-1", "Vista Point Mortgage", "-1"), False, "independent_mortgage_company"),
    (("L11", "3", "-1", "Citizens State Bank", "-1"), False, "bank"),  # no RSSD, bank regulator
]


def test_lender_types(segments: Segments, panel) -> None:
    rows = panel([case[0] for case in RULE_CASES])
    filers = pl.DataFrame(
        {
            "activity_year": [2021] * 3,
            "rssd_id": [int(c[0][2]) for c in RULE_CASES if c[1]],
        }
    )
    dim = lenders_from_panel(rows, 2021, segments, filers)
    got = dict(zip(dim["lei"], dim["lender_type"], strict=True))
    assert got == {case[0][0]: case[2] for case in RULE_CASES}
    assert dim.filter(pl.col("lei") == "L7")["respondent_rssd"].to_list() == [None]  # -1
    assert dim["in_call_reports"].to_list() == [case[1] for case in RULE_CASES]


def test_without_call_reports_the_link_rules_never_fire(segments: Segments, panel) -> None:
    dim = lenders_from_panel(panel([case[0] for case in RULE_CASES]), 2021, segments, None)
    got = dict(zip(dim["lei"], dim["lender_type"], strict=True))
    assert got["L2"] == "unknown"  # a CFPB bank isn't guessed to be a credit union
    assert got["L6"] == "unknown"
    assert got["L1"] == "bank"  # bank regulators still read as banks
    assert dim["in_call_reports"].null_count() == dim.height


def test_panel_for_another_year_is_refused(segments: Segments, panel) -> None:
    with pytest.raises(ValueError, match="activity years"):
        lenders_from_panel(panel(PANEL_ROWS), 2022, segments)


def test_filers_list_gives_names_only_rows(tmp_path: Path) -> None:
    path = tmp_path / "filers_2025.json"
    path.write_text(json.dumps([{"lei": "abc ", "name": "Some Lender", "count": 3}]))
    dim = lenders_from_filers(path, 2025)
    row = dim.row(0, named=True)
    assert (row["lei"], row["lender_type"], row["panel_available"]) == ("ABC", "unknown", False)
    assert row["respondent_rssd"] is None


def test_segments_config(segments: Segments) -> None:
    table = segments.purchaser_table()
    assert table["purchaser_type"].is_unique().all()
    assert table.filter(pl.col("purchaser_type") == 4)["holder_segment"].to_list() == ["other"]
    assert table.filter(pl.col("purchaser_type") == 0)["holder_segment"].to_list() == ["retained"]


def test_segments_reject_a_code_in_two_segments(tmp_path: Path) -> None:
    bad = tmp_path / "segments.yaml"
    bad.write_text(
        "holder_segments:\n  a: {label: A, purchaser_types: [0]}\n"
        "  b: {label: B, purchaser_types: [0]}\nlender_type_rules: []\n"
    )
    with pytest.raises(ValueError, match="purchaser_type 0"):
        Segments.load(bad)


def _files_server(seen: list[str], panel: bytes) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url).startswith(FILERS_API):
            return httpx.Response(200, json={"institutions": [{"lei": "X1", "name": "N"}]})
        return httpx.Response(200, content=panel)

    return httpx.MockTransport(handler)


def test_fetch_panels_downloads_or_falls_back_to_names(
    settings: Settings, segments: Segments
) -> None:
    assert 2021 in PANEL_YEARS and 2025 not in PANEL_YEARS
    seen: list[str] = []
    client = httpx.Client(transport=_files_server(seen, _zip(panel_csv(PANEL_ROWS))))
    manifest = Manifest.for_settings(settings)
    outcomes = fetch_panels(settings, manifest, [2021, 2025], client=client)
    assert [o.status for o in outcomes] == ["downloaded", "names only"]
    assert seen[0] == panel_url(2021)
    assert "no LEI-RSSD link" in outcomes[1].detail
    dim = build_dim_hmda_lender(Manifest.for_settings(settings), segments)
    assert dim.group_by("activity_year").len().sort("activity_year").rows() == [
        (2021, 3),
        (2025, 1),
    ]

    again = fetch_panels(settings, Manifest.for_settings(settings), [2021, 2025], client=client)
    assert [o.status for o in again] == ["cached", "names only"]
    assert len(seen) == 2  # nothing fetched twice


def test_fetch_panels_records_a_hand_placed_file(settings: Settings) -> None:
    placed = settings.raw_dir / "hmda_panel" / "2024_lender_panel.csv"
    placed.parent.mkdir(parents=True)
    placed.write_text(panel_csv(PANEL_ROWS).replace("2021,", "2024,"))
    [outcome] = fetch_panels(settings, Manifest.for_settings(settings), [2024], client=None)
    assert outcome.status == "recorded"


def test_fetch_panels_rejects_a_non_zip(settings: Settings) -> None:
    client = httpx.Client(transport=_files_server([], b"<html>error</html>"))
    [outcome] = fetch_panels(settings, Manifest.for_settings(settings), [2021], client=client)
    assert outcome.status == "failed" and "zip" in outcome.detail
    assert not (settings.raw_dir / "hmda_panel" / "2021_public_panel_csv.zip").exists()
    assert "hmda_panel:2021" not in Manifest.for_settings(settings)


def test_panel_zip_with_macos_metadata(tmp_path: Path) -> None:
    """The real 2022 zip carries an AppleDouble file next to the CSV."""
    path = tmp_path / "2022_public_panel_csv.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("2022_public_panel_csv.csv", panel_csv(PANEL_ROWS))
        zf.writestr("__MACOSX/._2022_public_panel_csv.csv", b"\x00\x05\x16\x07Mac OS X")
    assert "lei" in check_panel(path)
    assert read_panel(path).height == len(PANEL_ROWS)


def test_panel_zip_with_two_csvs_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "panel.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("a.csv", panel_csv(PANEL_ROWS))
        zf.writestr("b.csv", panel_csv(PANEL_ROWS))
    with pytest.raises(PanelError, match="expected one CSV"):
        check_panel(path)


def test_a_download_that_fails_the_check_is_kept(settings: Settings) -> None:
    seen: list[str] = []
    client = httpx.Client(transport=_files_server(seen, _zip("a,b\n1,2\n")))
    [outcome] = fetch_panels(settings, Manifest.for_settings(settings), [2021], client=client)
    assert outcome.status == "failed" and "lacks panel columns" in outcome.detail
    assert outcome.path is not None and outcome.path.exists()
    assert "hmda_panel:2021" in Manifest.for_settings(settings)

    [again] = fetch_panels(settings, Manifest.for_settings(settings), [2021], client=client)
    assert again.status == "failed"  # still checked, and still not downloaded again
    assert len(seen) == 1


def test_a_hand_placed_file_that_fails_the_check_is_not_recorded(settings: Settings) -> None:
    placed = settings.raw_dir / "hmda_panel" / "2024_lender_panel.csv"
    placed.parent.mkdir(parents=True)
    placed.write_text("a,b\n1,2\n")
    [outcome] = fetch_panels(settings, Manifest.for_settings(settings), [2024], client=None)
    assert outcome.status == "failed" and "replace the file" in outcome.detail
    assert "hmda_panel:2024" not in Manifest.for_settings(settings)
