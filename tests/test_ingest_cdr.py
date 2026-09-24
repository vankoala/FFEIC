import zipfile
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from armreset.ingest.cdr import (
    CdrFormatError,
    MdrmSpec,
    index_members,
    parse_member,
    read_cdr_file,
    read_schedule,
    stage_quarter,
)
from tests.cdr_fixtures import BANKS, make_cdr_zip, tsv
from tests.conftest import REPO_ROOT


@pytest.fixture
def spec() -> MdrmSpec:
    return MdrmSpec.load(REPO_ROOT / "config" / "mdrm.yaml")


# Member names as they appear in the real 2018-2026 zips.
@pytest.mark.parametrize(
    ("name", "code", "part", "parts"),
    [
        ("FFIEC CDR Call Bulk POR 06302026.txt", "POR", 1, 1),
        ("FFIEC CDR Call Schedule RC 06302026.txt", "RC", 1, 1),
        ("FFIEC CDR Call Schedule RCCI 06302026.txt", "RCCI", 1, 1),
        ("FFIEC CDR Call Schedule RCCII 06302026.txt", "RCCII", 1, 1),
        ("FFIEC CDR Call Schedule RCB 06302026(1 of 2).txt", "RCB", 1, 2),
        ("FFIEC CDR Call Schedule RCRII 06302026(4 of 4).txt", "RCRII", 4, 4),
    ],
)
def test_parse_member(name: str, code: str, part: int, parts: int) -> None:
    member = parse_member(name)
    assert member is not None
    assert (member.code, member.stamp, member.part, member.parts) == (code, "06302026", part, parts)


@pytest.mark.parametrize(
    "name",
    ["Readme.txt", "FFIEC CDR Call Schedule RC 0630202.txt", "FFIEC CDR Call Schedule RC.txt"],
)
def test_parse_member_ignores_other_files(name: str) -> None:
    assert parse_member(name) is None


def test_index_members_orders_parts_and_rejects_gaps() -> None:
    names = [
        "FFIEC CDR Call Schedule RCB 06302026(2 of 2).txt",
        "FFIEC CDR Call Schedule RCB 06302026(1 of 2).txt",
        "Readme.txt",
    ]
    assert [m.part for m in index_members(names)["RCB"]] == [1, 2]
    with pytest.raises(CdrFormatError, match="incomplete"):
        index_members(["FFIEC CDR Call Schedule RCB 06302026(2 of 2).txt"])


def test_reader_drops_schedule_description_row_and_trailing_tab() -> None:
    raw = tsv(['"IDRSSD"', "RCON2170"], [["37", "85210"], ["242", ""]], ["", "TOTAL ASSETS"])
    df = read_cdr_file(raw)
    assert df.columns == ["IDRSSD", "RCON2170"]  # quotes stripped, no unnamed column
    assert df["IDRSSD"].to_list() == ["37", "242"]
    assert df["RCON2170"].to_list() == ["85210", None]  # blank = not reported


def test_reader_keeps_first_bank_when_there_is_no_description_row() -> None:
    raw = tsv(['"IDRSSD"', "Financial Institution Name"], [["37", "BANK"]], trailing_tab=False)
    assert read_cdr_file(raw)["IDRSSD"].to_list() == ["37"]


def test_reader_ignores_quote_characters_in_values() -> None:
    raw = tsv(['"IDRSSD"', "TEXT"], [["37", '"quoted start'], ["242", "plain"]], ["", "D"])
    assert read_cdr_file(raw)["TEXT"].to_list() == ['"quoted start', "plain"]


def test_reader_rejects_ragged_rows_and_duplicate_banks() -> None:
    ragged = b'"IDRSSD"\tA\r\n37\t1\textra\r\n'
    with pytest.raises(Exception, match=r"(?i)field"):
        read_cdr_file(ragged)
    with pytest.raises(CdrFormatError, match="duplicate"):
        read_cdr_file(tsv(['"IDRSSD"', "A"], [["37", "1"], ["37", "2"]], ["", "D"]))


def test_reader_decodes_windows_1252() -> None:
    raw = '"IDRSSD"\tName\r\n37\tBANCO PE\xd1A\r\n'.encode("cp1252")
    assert read_cdr_file(raw)["Name"].to_list() == ["BANCO PEÑA"]


def test_two_part_schedule_joins_on_idrssd(tmp_path: Path) -> None:
    path = make_cdr_zip(tmp_path)
    with zipfile.ZipFile(path) as zf:
        members = index_members(zf.namelist())["RCCI"]
        df = read_schedule(zf, members)
    assert len(members) == 2
    assert {"RCON5367", "RCONA564", "RCONA569"} <= set(df.columns)
    row = df.filter(pl.col("IDRSSD") == "100").row(0, named=True)
    assert (row["RCON5367"], row["RCONA564"], row["RCONA569"]) == ("500", "10", "290")
    assert df.height == len(BANKS)


def test_mdrm_spec_prefix_orders(spec: MdrmSpec) -> None:
    assert spec.prefixes_for(spec.total_assets) == ("RCFD", "RCON")
    assert spec.prefixes_for(spec.first_lien_total) == ("RCON",)
    assert all(spec.prefixes_for(code) == ("RCON",) for code in spec.buckets.values())
    # Staging keeps both prefixes of every code, for audit.
    assert {"RCFD5367", "RCON5367", "RCFDA564", "RCONA564"} <= set(spec.wanted_columns)
    assert list(spec.items)[:2] == ["total_assets", "first_lien_total"]


def test_stage_quarter_writes_identity_and_raw_columns(spec: MdrmSpec, tmp_path: Path) -> None:
    zip_path = make_cdr_zip(tmp_path / "raw")
    out = tmp_path / "stg.parquet"
    result = stage_quarter(zip_path, date(2026, 6, 30), spec, out, "abc123")
    df = pl.read_parquet(out)

    assert result.n_banks == df.height == len(BANKS)
    assert result.data_as_of == "2026-09-15T04:30:03"
    assert set(spec.wanted_columns) <= set(df.columns)
    assert "RCFDA564" in result.absent and df["RCFDA564"].null_count() == df.height
    assert result.non_numeric == {"RCON2170": 1}  # the CONF value
    big = df.filter(pl.col("rssd_id") == 100).row(0, named=True)
    assert (big["name"], big["form"], big["fdic_cert"]) == ("BIG BANK, N.A.", "031", "1001")
    assert (big["RCFD5367"], big["RCON5367"]) == ("600", "500")
    assert big["report_date"] == date(2026, 6, 30)
    assert big["source_sha256"] == "abc123"
    assert "RCONB575" not in df.columns  # RCCII is not read


def test_stage_quarter_rejects_mismatched_date_or_missing_schedule(
    spec: MdrmSpec, tmp_path: Path
) -> None:
    zip_path = make_cdr_zip(tmp_path)
    with pytest.raises(CdrFormatError, match="another date"):
        stage_quarter(zip_path, date(2026, 3, 31), spec, tmp_path / "x.parquet", "sha")
    trimmed = tmp_path / "trimmed.zip"
    with zipfile.ZipFile(zip_path) as src, zipfile.ZipFile(trimmed, "w") as dst:
        for name in src.namelist():
            if "Schedule RCN " not in name:
                dst.writestr(name, src.read(name))
    with pytest.raises(CdrFormatError, match="RCN"):
        stage_quarter(trimmed, date(2026, 6, 30), spec, tmp_path / "y.parquet", "sha")
