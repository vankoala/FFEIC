from pathlib import Path

import pytest
from typer.testing import CliRunner

from armreset import __version__
from armreset.cli import app
from armreset.manifest import Manifest
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
    [["fetch", "cdr"], ["fetch", "hmda"], ["fetch", "panel"], ["build"], ["validate"], ["app"]],
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
