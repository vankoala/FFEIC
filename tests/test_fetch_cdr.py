from datetime import date
from pathlib import Path

import pytest
import requests
from tenacity import retry, retry_if_exception, stop_after_attempt

import armreset.fetch.cdr as cdr
from armreset.fetch.cdr import (
    BULK_PAGE_URL,
    CdrFetcher,
    CdrFetchError,
    CollectorSource,
    Downloaded,
    check_zip,
    find_zip,
    manifest_key,
    manual_instructions,
)
from armreset.http import PoliteSession, Throttle
from armreset.manifest import Manifest
from armreset.periods import Quarter
from armreset.settings import Settings, load_settings
from tests.cdr_fixtures import make_cdr_zip

Q1, Q2 = Quarter(2026, 1), Quarter(2026, 2)


class FakeSource:
    """Stands in for CDR: lists quarters and 'downloads' synthetic zips into a work dir."""

    def __init__(self, work_dir: Path, listed: list[Quarter], fail_with: Exception | None = None):
        self.work_dir = work_dir
        self.listed = listed
        self.fail_with = fail_with
        self.downloads: list[Quarter] = []

    def available_quarters(self) -> list[Quarter]:
        return self.listed

    def download(self, quarter: Quarter) -> Downloaded:
        self.downloads.append(quarter)
        if self.fail_with is not None:
            raise self.fail_with
        path = make_cdr_zip(self.work_dir, stamp=cdr.cdr_stamp(quarter))
        return Downloaded(path, date(2026, 9, 15))


class NoRequests:
    def available_quarters(self) -> list[Quarter]:
        raise AssertionError("expected no request to CDR")

    def download(self, quarter: Quarter) -> Downloaded:
        raise AssertionError("expected no request to CDR")


@pytest.fixture
def settings(project: Path) -> Settings:
    return load_settings()


def _fetcher(settings: Settings, source) -> CdrFetcher:
    return CdrFetcher(settings, Manifest.for_settings(settings), source=source)


def test_downloads_missing_quarters_and_records_them(settings: Settings, tmp_path: Path) -> None:
    source = FakeSource(tmp_path / "work", [Q1, Q2])
    outcomes = _fetcher(settings, source).fetch([Q1, Q2])

    assert [o.status for o in outcomes] == ["downloaded", "downloaded"]
    assert source.downloads == [Q1, Q2]
    assert all(o.path.parent == settings.raw_dir / "cdr" for o in outcomes)
    assert not list((tmp_path / "work").glob("*.zip"))  # moved out of the work dir
    entry = Manifest.for_settings(settings).get(manifest_key(Q2))
    assert (entry.url, entry.period, entry.published) == (BULK_PAGE_URL, "2026-06-30", "2026-09-15")
    assert entry.meta["format"] == "Tab Delimited"


def test_second_run_makes_no_requests(settings: Settings, tmp_path: Path) -> None:
    _fetcher(settings, FakeSource(tmp_path, [Q2])).fetch([Q2])
    outcomes = _fetcher(settings, NoRequests()).fetch([Q2])
    assert [o.status for o in outcomes] == ["cached"]


def test_hand_placed_zip_is_recorded_without_requests(settings: Settings) -> None:
    make_cdr_zip(settings.raw_dir / "cdr", stamp="06302026")
    outcomes = _fetcher(settings, NoRequests()).fetch([Q2])
    assert [o.status for o in outcomes] == ["recorded"]
    entry = Manifest.for_settings(settings).get(manifest_key(Q2))
    assert entry.url is None
    assert entry.meta["origin"] == "placed by hand"


def test_damaged_or_duplicate_hand_placed_zips_are_errors(settings: Settings) -> None:
    directory = settings.raw_dir / "cdr"
    directory.mkdir(parents=True)
    bad = directory / "FFIEC CDR Call Bulk All Schedules 06302026.zip"
    bad.write_bytes(b"<html>not a zip</html>")
    with pytest.raises(CdrFetchError, match="not a valid zip"):
        _fetcher(settings, NoRequests()).fetch([Q2])
    (directory / "copy of 06302026.zip").write_bytes(b"x")
    with pytest.raises(CdrFetchError, match="More than one zip"):
        find_zip(directory, Q2)


def test_quarter_cdr_does_not_list_is_not_requested(settings: Settings, tmp_path: Path) -> None:
    source = FakeSource(tmp_path, [Q1])
    outcomes = _fetcher(settings, source).fetch([Q2])
    assert [o.status for o in outcomes] == ["not listed"]
    assert source.downloads == []


def test_failure_stops_further_downloads(settings: Settings, tmp_path: Path) -> None:
    source = FakeSource(tmp_path, [Q1, Q2], fail_with=CdrFetchError("page changed"))
    outcomes = _fetcher(settings, source).fetch([Q1, Q2])
    assert [o.status for o in outcomes] == ["failed", "skipped"]
    assert source.downloads == [Q1]  # no second attempt: the error isn't transient
    assert "page changed" in outcomes[0].detail


def test_transient_errors_are_retried(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same policy as production, minus the waits.
    no_wait = retry(
        retry=retry_if_exception(cdr._transient), stop=stop_after_attempt(3), reraise=True
    )
    monkeypatch.setattr(cdr, "_with_retries", no_wait)
    source = FakeSource(tmp_path, [Q2])
    calls = {"n": 0}
    real_download = source.download

    def flaky(quarter: Quarter) -> Downloaded:
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.ConnectionError("reset by peer")
        return real_download(quarter)

    source.download = flaky
    outcomes = _fetcher(settings, source).fetch([Q2])
    assert [o.status for o in outcomes] == ["downloaded"]
    assert calls["n"] == 2


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (requests.ConnectionError(), True),
        (requests.Timeout(), True),
        (requests.HTTPError(response=type("R", (), {"status_code": 503})()), True),
        (requests.HTTPError(response=type("R", (), {"status_code": 404})()), False),
        (ValueError("Could not extract ViewState from page"), False),
    ],
)
def test_transient_classification(exc: Exception, expected: bool) -> None:
    assert cdr._transient(exc) is expected


def test_latest_quarter_uses_cdr_listing_or_falls_back(settings: Settings, tmp_path: Path) -> None:
    assert _fetcher(settings, FakeSource(tmp_path, [Q1, Q2])).latest_quarter()[0] == Q2

    class Unreachable(NoRequests):
        def available_quarters(self) -> list[Quarter]:
            raise requests.ConnectionError("down")

    quarter, how = _fetcher(settings, Unreachable()).latest_quarter()
    assert "posting lag" in how
    assert isinstance(quarter, Quarter)


def test_check_zip_requires_the_quarters_files(tmp_path: Path) -> None:
    path = make_cdr_zip(tmp_path, stamp="06302026")
    assert "Readme.txt" in check_zip(path, Q2)
    with pytest.raises(CdrFetchError, match="03312026"):
        check_zip(path, Q1)


def test_collector_source_uses_our_session(tmp_path: Path) -> None:
    source = CollectorSource(Throttle(5), "arm-reset-research/test", tmp_path)
    session = source._dl.session
    assert isinstance(session, PoliteSession)
    assert session.headers["User-Agent"] == "arm-reset-research/test"
    assert "Accept" in session.headers  # the library's other headers are kept


def test_manual_instructions_name_quarters_and_directory(tmp_path: Path) -> None:
    text = manual_instructions([Q1, Q2], tmp_path)
    assert "2026Q1 (03/31/2026)" in text and "2026Q2 (06/30/2026)" in text
    assert str(tmp_path) in text and "Tab Delimited" in text
