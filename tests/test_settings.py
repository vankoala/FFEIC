from pathlib import Path

import pytest
from pydantic import ValidationError

from armreset.settings import PROJECT_URL, load_settings
from tests.conftest import REPO_ROOT


def test_repo_config_loads() -> None:
    s = load_settings(REPO_ROOT / "config.yaml")
    assert s.root == REPO_ROOT
    assert s.config_path == REPO_ROOT / "config.yaml"
    assert s.warehouse_path.is_relative_to(REPO_ROOT)
    assert min(s.hmda.years) >= 2018
    assert s.cdr.request_delay_s >= 5
    assert s.model.scenarios
    assert s.config_file("mdrm.yaml").is_file()


def test_relative_paths_resolve_against_config_dir(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # the working directory is not the project root
    s = load_settings()
    assert s.root == project
    assert s.raw_dir == project / "data" / "raw"
    assert s.staging_dir == project / "data" / "staging"
    assert s.warehouse_path == project / "data" / "warehouse.duckdb"
    assert s.manifest_path == project / "data" / "raw" / "manifest.json"
    assert s.config_file("segments.yaml") == project / "config" / "segments.yaml"


def test_absolute_paths_are_kept(patch_config, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere" / "wh.duckdb"
    patch_config({"paths": {"warehouse": str(elsewhere)}})
    assert load_settings().warehouse_path == elsewhere


def test_env_overrides_yaml_and_deep_merges(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    yaml_scenarios = load_settings().model.scenarios
    monkeypatch.setenv("ARMRESET_CONTACT_EMAIL", "analyst@example.com")
    monkeypatch.setenv("ARMRESET_HMDA__KEEP_RAW_CSV", "true")
    monkeypatch.setenv("ARMRESET_MODEL__SCENARIOS__BASE", "0.2")
    s = load_settings()
    assert s.contact_email == "analyst@example.com"
    assert s.hmda.keep_raw_csv is True
    assert s.model.scenarios["base"] == 0.2
    # The other scenarios still come from the YAML file.
    assert set(s.model.scenarios) == set(yaml_scenarios) | {"base"}


def test_explicit_path_beats_env_var(project: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "config.yaml").write_text("contact_email: someone@example.com\n")
    s = load_settings(other / "config.yaml")
    assert s.root == other
    assert s.contact_email == "someone@example.com"


def test_missing_config_is_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARMRESET_CONFIG", str(tmp_path / "nope.yaml"))
    with pytest.raises(FileNotFoundError, match=r"nope\.yaml"):
        load_settings()


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"cdr": {"request_delay_s": 1}}, "request_delay_s"),
        ({"cdr": {"start": "2018Q5"}}, "Not a quarter"),
        ({"hmda": {"years": [2017, 2018]}}, "predate 2018"),
        ({"model": {"scenarios": {"base": 1.5}}}, "CPR"),
        ({"model": {"calendar_years": [2032, 2026]}}, "after last year"),
        ({"model": {"subsequent_resets": {"frequency_months": 0}}}, "frequency_months"),
        ({"not_a_setting": 1}, "not_a_setting"),
    ],
)
def test_invalid_config_is_rejected(patch_config, patch: dict, match: str) -> None:
    patch_config(patch)
    with pytest.raises(ValidationError, match=match):
        load_settings()


def test_cdr_quarters_are_normalised(patch_config) -> None:
    patch_config({"cdr": {"start": "2019q3", "end": "Latest"}})
    s = load_settings()
    assert (s.cdr.start, s.cdr.end) == ("2019Q3", "latest")


def test_user_agent_is_descriptive_and_omits_placeholder(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = load_settings()
    assert not s.contact_configured
    assert s.user_agent.startswith("arm-reset-research/")
    assert PROJECT_URL in s.user_agent
    assert "SET_ME" not in s.user_agent
    monkeypatch.setenv("ARMRESET_CONTACT_EMAIL", "analyst@example.com")
    s = load_settings()
    assert s.contact_configured
    assert "contact: analyst@example.com" in s.user_agent


def test_llm_disabled_without_base_url(patch_config) -> None:
    assert not load_settings().llm.enabled
    patch_config({"llm": {"base_url": "http://localhost:8000/v1", "model": "some-model"}})
    assert load_settings().llm.enabled


def test_local_config_overrides_config_yaml(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (project / "config.local.yaml").write_text(
        "contact_email: analyst@example.com\nhmda:\n  keep_raw_csv: true\n"
    )
    s = load_settings()
    assert s.config_files == (project / "config.yaml", project / "config.local.yaml")
    assert s.contact_email == "analyst@example.com"
    assert s.hmda.keep_raw_csv is True
    assert s.hmda.years  # deep merge: the rest of hmda still comes from config.yaml
    monkeypatch.setenv("ARMRESET_CONTACT_EMAIL", "env@example.com")
    assert load_settings().contact_email == "env@example.com"  # environment beats both files


def test_local_config_is_optional(project: Path) -> None:
    assert load_settings().config_files == (project / "config.yaml",)
