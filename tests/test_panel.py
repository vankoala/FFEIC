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
from armreset.ingest.panel import cfpb_lender_lists, read_panel
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


def test_segments_config(segments: Segments) -> None:
    table = segments.purchaser_table()
    assert table["purchaser_type"].is_unique().all()
    assert table.filter(pl.col("purchaser_type") == 4)["holder_segment"].to_list() == ["other"]
    assert table.filter(pl.col("purchaser_type") == 0)["holder_segment"].to_list() == ["retained"]


def test_segments_reject_a_code_in_two_segments(tmp_path: Path) -> None:
    bad = tmp_path / "segments.yaml"
    bad.write_text(
        "holder_segments:\n  a: {label: A, purchaser_types: [0]}\n"
        "  b: {label: B, purchaser_types: [0]}\nlender_types: {}\n"
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


def test_fetch_panels_downloads_or_falls_back_to_names(settings: Settings) -> None:
    assert 2021 in PANEL_YEARS and 2025 not in PANEL_YEARS
    seen: list[str] = []
    client = httpx.Client(transport=_files_server(seen, _zip(panel_csv(PANEL_ROWS))))
    manifest = Manifest.for_settings(settings)
    outcomes = fetch_panels(settings, manifest, [2021, 2025], client=client)
    assert [o.status for o in outcomes] == ["downloaded", "names only"]
    assert seen[0] == panel_url(2021)
    assert "check the Lender File's coverage" in outcomes[1].detail
    lists = cfpb_lender_lists(Manifest.for_settings(settings))
    assert lists.group_by("activity_year", "cfpb_list").len().sort("activity_year").rows() == [
        (2021, "panel", 3),
        (2025, "filers list", 1),
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


def test_cfpb_lists_prefer_the_panel_and_null_its_minus_one(settings: Settings) -> None:
    manifest = Manifest.for_settings(settings)
    panel = settings.raw_dir / "hmda_panel" / "2021_public_panel_csv.zip"
    panel.parent.mkdir(parents=True)
    panel.write_bytes(_zip(panel_csv(PANEL_ROWS)))
    manifest.record("hmda_panel:2021", "hmda_panel", panel, period="2021")
    filers = settings.raw_dir / "hmda_panel" / "filers_2021.json"
    filers.write_text(json.dumps([{"lei": "other", "name": "Other", "count": 1}]))
    manifest.record("hmda_filers:2021", "hmda_filers", filers, period="2021")
    lists = cfpb_lender_lists(Manifest.for_settings(settings))
    assert lists["cfpb_list"].unique().to_list() == ["panel"]  # the 2021 filers list is ignored
    rssd = dict(zip(lists["lei"], lists["respondent_rssd"], strict=True))
    assert rssd == {r[0]: (None if r[2] == "-1" else int(r[2])) for r in PANEL_ROWS}
    assert cfpb_lender_lists(Manifest(settings.raw_dir / "none.json", settings.root)) is None
