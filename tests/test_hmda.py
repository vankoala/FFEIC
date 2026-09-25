from pathlib import Path

import duckdb
import httpx
import polars as pl
import pytest

from armreset.fetch.hmda import (
    FILTERS,
    STATES,
    HmdaFetcher,
    HmdaFetchError,
    csv_to_parquet,
    manifest_key,
    request_url,
    stream_csv,
)
from armreset.http import Throttle
from armreset.ingest.hmda import (
    HmdaStagingError,
    sources_for_year,
    stage_year,
    year_dir,
)
from armreset.manifest import Manifest
from armreset.settings import Settings, load_settings
from tests.hmda_fixtures import CASES, write_lar_csv

NO_WAIT = Throttle(0)


@pytest.fixture
def settings(project: Path) -> Settings:
    return load_settings()


def _lar_bytes(tmp_path: Path) -> bytes:
    return write_lar_csv(tmp_path / "src.csv").read_bytes()


def _api(content: bytes, seen: list[str]) -> httpx.MockTransport:
    """The Data Browser API: redirect to the file server, which serves the CSV."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "ffiec.cfpb.gov":
            return httpx.Response(301, headers={"Location": "https://files.ffiec.cfpb.gov/q.csv"})
        return httpx.Response(200, content=content)

    return httpx.MockTransport(handler)


def test_request_url_uses_at_most_two_filters() -> None:
    url, params = request_url(2021)
    assert url.endswith("/view/nationwide/csv")
    assert set(FILTERS) == {"actions_taken", "lien_statuses"}  # the API's two-filter limit
    assert params == {"years": "2021", **FILTERS}
    url, params = request_url(2021, "VT")
    assert url.endswith("/view/csv") and params["states"] == "VT"
    assert len(STATES) == 52  # 50 states, DC and PR


def test_stream_csv_follows_the_redirect(tmp_path: Path) -> None:
    seen: list[str] = []
    client = httpx.Client(transport=_api(_lar_bytes(tmp_path), seen), follow_redirects=True)
    out = tmp_path / "lar.csv"
    got = stream_csv(client, NO_WAIT, *request_url(2021), out)
    assert out.read_bytes().startswith(b"activity_year")
    assert got.final_url == "https://files.ffiec.cfpb.gov/q.csv"
    assert len(got.redirects) == 1 and "nationwide/csv" in got.redirects[0]
    assert not out.with_name("lar.csv.part").exists()


def test_stream_csv_refuses_a_page_that_is_not_lar(tmp_path: Path) -> None:
    client = httpx.Client(transport=_api(b"<html>maintenance</html>", []), follow_redirects=True)
    out = tmp_path / "lar.csv"
    with pytest.raises(HmdaFetchError, match="Not an HMDA LAR CSV"):
        stream_csv(client, NO_WAIT, *request_url(2021), out)
    assert not out.exists() and not out.with_name("lar.csv.part").exists()


def test_fetcher_converts_records_and_caches(settings: Settings, tmp_path: Path) -> None:
    seen: list[str] = []
    client = httpx.Client(transport=_api(_lar_bytes(tmp_path), seen), follow_redirects=True)
    manifest = Manifest.for_settings(settings)
    [outcome] = HmdaFetcher(settings, manifest, client, NO_WAIT).fetch([2021])
    assert outcome.status == "downloaded"
    assert outcome.rows == len(CASES)
    assert outcome.parquet.exists()
    assert not outcome.parquet.with_suffix(".csv").exists()  # keep_raw_csv is false
    entry = manifest.get(manifest_key(2021))
    assert entry.meta["rows"] == len(CASES) and entry.meta["filters"] == FILTERS
    assert entry.derived == ["data/raw/hmda/lar_2021.parquet"]
    assert manifest.is_cached(entry.key)  # via the Parquet, although the CSV is gone

    [again] = HmdaFetcher(settings, Manifest.for_settings(settings), client, NO_WAIT).fetch([2021])
    assert again.status == "cached"
    assert len(seen) == 2  # the redirect and the file, once


def test_fetcher_keeps_the_csv_when_asked(project: Path, patch_config, tmp_path: Path) -> None:
    patch_config({"hmda": {"keep_raw_csv": True}})
    settings = load_settings()
    client = httpx.Client(transport=_api(_lar_bytes(tmp_path), []), follow_redirects=True)
    [outcome] = HmdaFetcher(settings, Manifest.for_settings(settings), client, NO_WAIT).fetch(
        [2021]
    )
    assert outcome.parquet.with_suffix(".csv").exists()


def test_fetcher_reports_failure(settings: Settings) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400)))
    outcomes = HmdaFetcher(settings, Manifest.for_settings(settings), client, NO_WAIT).fetch(
        [2021, 2022]
    )
    assert [o.status for o in outcomes] == ["failed"]  # stops at the first failure
    assert "400" in outcomes[0].detail


def _record(settings: Settings, key: str, rows=None, state: str | None = None) -> None:
    """Put a LAR Parquet on disk and in the manifest, as the fetcher would."""
    manifest = Manifest.for_settings(settings)
    stem = key.replace(":", "_")
    csv = write_lar_csv(settings.raw_dir / "hmda" / f"{stem}.csv", rows)
    parquet = csv.with_suffix(".parquet")
    csv_to_parquet(csv, parquet)
    manifest.record(key, "hmda", csv, period="2021", meta={"state": state})
    manifest.add_derived(key, parquet)


def _staged(settings: Settings) -> pl.DataFrame:
    return pl.read_parquet(year_dir(settings, 2021) / "*.parquet")


def test_staging_applies_the_plan_rules(settings: Settings) -> None:
    _record(settings, "hmda:2021:nationwide")
    staged = stage_year(settings, Manifest.for_settings(settings), 2021)
    kept = [(label, rate) for label, _, rate in CASES if rate is not None]
    assert staged.rows == len(kept)
    df = _staged(settings)
    # PLAN.md §11: NA -> fixed, Exempt -> unknown, 60/360 -> arm, 360/360 -> fixed, 0 -> fixed
    assert sorted(df["rate_type"].to_list()) == sorted(rate for _, rate in kept)
    arm = df.filter(pl.col("rate_type") == "arm")
    assert sorted(arm["intro_m"].to_list()) == [1, 60, 84, 120]  # "00120" parses as 120
    assert arm.filter(pl.col("intro_m") == 1)["is_io"].to_list() == [True]
    assert arm.filter(pl.col("intro_m") == 84)["interest_rate"].to_list() == [None]  # "NA"
    exempt = df.filter(pl.col("rate_type") == "unknown").row(0, named=True)
    assert (exempt["intro_raw"], exempt["is_io"]) == ("Exempt", False)
    assert set(df["dwelling_category"]) == {
        "Single Family (1-4 Units):Site-Built",
        "Single Family (1-4 Units):Manufactured",
    }


def test_each_year_uses_one_source_so_no_loan_is_counted_twice(settings: Settings) -> None:
    _record(settings, "hmda:2021:nationwide")
    _record(settings, "hmda:2021:VT", state="VT")
    manifest = Manifest.for_settings(settings)
    assert [e.key for e in sources_for_year(manifest, 2021)] == ["hmda:2021:nationwide"]
    single = stage_year(settings, manifest, 2021)
    assert single.sources == ["hmda:2021:nationwide"]


def test_a_partial_set_of_state_files_is_refused(settings: Settings) -> None:
    _record(settings, "hmda:2021:VT", state="VT")
    with pytest.raises(HmdaStagingError, match="missing"):
        sources_for_year(Manifest.for_settings(settings), 2021)


def test_all_state_files_stage_together(settings: Settings) -> None:
    for state in STATES:
        _record(settings, f"hmda:2021:{state}", rows=[CASES[0][1]], state=state)
    staged = stage_year(settings, Manifest.for_settings(settings), 2021)
    assert staged.rows == len(STATES)
    assert len(staged.sources) == len(STATES)


def test_staging_is_idempotent(settings: Settings) -> None:
    _record(settings, "hmda:2021:nationwide")
    manifest = Manifest.for_settings(settings)
    first = stage_year(settings, manifest, 2021)
    second = stage_year(settings, manifest, 2021)
    assert (first.reused, second.reused) == (False, True)
    assert second.rows == first.rows
    _record(settings, "hmda:2021:nationwide", rows=[CASES[0][1]])  # new raw file
    third = stage_year(settings, Manifest.for_settings(settings), 2021)
    assert (third.reused, third.rows) == (False, 1)


def test_csv_to_parquet_keeps_every_column_as_text(tmp_path: Path) -> None:
    csv = write_lar_csv(tmp_path / "x.csv")
    rows = csv_to_parquet(csv, tmp_path / "x.parquet")
    assert rows == len(CASES)
    schema = duckdb.sql(f"DESCRIBE SELECT * FROM '{tmp_path / 'x.parquet'}'").fetchall()
    assert {col[1] for col in schema} == {"VARCHAR"}
    loan_term = duckdb.sql(
        f"SELECT loan_term FROM '{tmp_path / 'x.parquet'}' WHERE loan_term LIKE '00%'"
    ).fetchall()
    assert loan_term == [("00360",)]  # text, leading zeros intact
