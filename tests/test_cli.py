from pathlib import Path

import pytest
from typer.testing import CliRunner

from armreset import __version__
from armreset.cli import app
from armreset.manifest import Manifest
from armreset.periods import Quarter
from armreset.settings import load_settings

runner = CliRunner()


def test_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("fetch", "build", "validate", "status", "app"):
        assert command in result.output


def test_fetch_help_lists_every_source() -> None:
    result = runner.invoke(app, ["fetch", "--help"])
    assert result.exit_code == 0
    for source in ("cdr", "hmda", "panel"):
        assert source in result.output


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_status_on_empty_project(project: Path) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "No downloads yet" in result.output
    assert "not set" in result.output  # contact_email is still the placeholder


def test_status_summarises_manifest(project: Path) -> None:
    zipped = project / "data" / "raw" / "cdr" / "q.zip"
    zipped.parent.mkdir(parents=True)
    zipped.write_bytes(b"x" * 2048)
    Manifest.for_settings(load_settings()).record("cdr:2026Q2", "cdr", zipped, period="2026-06-30")

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "cdr" in result.output
    assert "2026-06-30" in result.output
    assert "2.0 KB" in result.output


def test_config_option_selects_file(tmp_path: Path) -> None:
    config = tmp_path / "alt" / "config.yaml"
    config.parent.mkdir()
    config.write_text("contact_email: analyst@example.com\n")
    result = runner.invoke(app, ["--config", str(config), "status"])
    assert result.exit_code == 0, result.output
    assert "analyst@example.com" in result.output


def test_missing_config_exits_with_usage_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ARMRESET_CONFIG", str(tmp_path / "missing.yaml"))
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 2
    assert "missing.yaml" in result.output


# Remove each case as its phase implements the command.
@pytest.mark.parametrize(
    "args",
    [["fetch", "hmda"], ["fetch", "panel"], ["app"]],
)
def test_unimplemented_commands_fail_loudly(project: Path, args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert "not implemented yet" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["fetch", "cdr", "--start", "2018Q9"],
        ["fetch", "cdr", "--start", "2026Q2", "--end", "2018Q1"],
        ["fetch", "hmda", "--years", "2016-2018"],
        ["fetch", "panel", "--years", "twenty"],
    ],
)
def test_bad_periods_are_usage_errors(project: Path, args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 2


# --- phase 1: fetch cdr, build, validate, status, spotcheck -------------------------------


@pytest.fixture
def fake_cdr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Route `armtool fetch cdr` to a fake CDR that lists 2026Q1-Q2."""
    import armreset.fetch.cdr as cdr
    from tests.test_fetch_cdr import FakeSource

    source = FakeSource(tmp_path / "work", [Quarter(2026, 1), Quarter(2026, 2)])
    monkeypatch.setattr(cdr, "CollectorSource", lambda *args, **kwargs: source)
    return source


def test_fetch_cdr_latest(project: Path, fake_cdr) -> None:
    result = runner.invoke(app, ["fetch", "cdr", "--start", "latest", "--end", "latest"])
    assert result.exit_code == 0, result.output
    assert "2026Q2 (newest period listed by CDR)" in result.output
    assert "downloaded" in result.output
    assert fake_cdr.downloads == [Quarter(2026, 2)]
    again = runner.invoke(app, ["fetch", "cdr", "--start", "2026Q2", "--end", "2026Q2"])
    assert "cached" in again.output
    assert fake_cdr.downloads == [Quarter(2026, 2)]


def test_fetch_cdr_failure_prints_manual_steps(project: Path, fake_cdr) -> None:
    from armreset.fetch.cdr import CdrFetchError

    fake_cdr.fail_with = CdrFetchError("page changed")
    result = runner.invoke(app, ["fetch", "cdr", "--start", "2026Q2", "--end", "2026Q2"])
    assert result.exit_code == 1
    assert "Download these quarters by hand: 2026Q2 (06/30/2026)" in result.output


def test_build_validate_status(project: Path, fake_cdr) -> None:
    assert runner.invoke(app, ["build"]).exit_code == 1  # nothing fetched yet
    runner.invoke(app, ["fetch", "cdr", "--start", "2026Q2", "--end", "2026Q2"])
    result = runner.invoke(app, ["build"])
    assert result.exit_code == 0, result.output
    assert "Staged 2026-06-30" in result.output and "fact_cdr_repricing" in result.output
    result = runner.invoke(app, ["validate"])
    assert result.exit_code == 0, result.output
    assert (project / "data" / "qa_report.md").exists()
    result = runner.invoke(app, ["status"])
    assert "fact_cdr_repricing" in result.output and "Warehouse" in result.output


def test_spotcheck(
    project: Path, fake_cdr, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import armreset.fetch.facsimile as facsimile
    from tests.test_pipeline import _sdf

    runner.invoke(app, ["fetch", "cdr", "--start", "2026Q2", "--end", "2026Q2"])
    runner.invoke(app, ["build"])
    sdf = {"sdf": _sdf(tmp_path), "pdf": tmp_path / "bank.pdf"}
    monkeypatch.setattr(facsimile, "fetch_facsimile", lambda *args, **kwargs: sdf)
    result = runner.invoke(app, ["spotcheck", "100"])
    assert result.exit_code == 0, result.output
    assert "BIG BANK, N.A." in result.output and "NO" not in result.output

    sdf["sdf"] = _sdf(tmp_path, RCONA568="101")
    assert runner.invoke(app, ["spotcheck", "100"]).exit_code == 1
    missing = runner.invoke(app, ["spotcheck", "600"])  # FDIC cert 0
    assert missing.exit_code == 1 and "no FDIC certificate" in missing.output
