"""Settings: ``config.yaml`` plus ``ARMRESET_*`` environment overrides (PLAN.md §10).

Precedence, highest first: ``ARMRESET_*`` environment variables, then ``config.local.yaml``
next to the config file (gitignored, for personal values such as ``contact_email``), then
``config.yaml``.

Relative paths in the config resolve against the directory holding the config file, so the
CLI, tests and the Streamlit app agree on where data lives whatever the working directory.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from armreset import __version__
from armreset.periods import Quarter

CONFIG_ENV_VAR = "ARMRESET_CONFIG"
CONFIG_FILENAME = "config.yaml"
LOCAL_CONFIG_FILENAME = "config.local.yaml"
UNSET_CONTACT = "SET_ME"
PROJECT_URL = "https://github.com/vankoala/FFEIC"
MIN_REQUEST_DELAY_S = 5.0  # PLAN.md §0: at least 5 seconds between requests
FIRST_HMDA_YEAR = 2018  # intro_rate_period first appears in the 2018 LAR

# The YAML files for the Settings() call in progress, lowest priority first; see load_settings().
_yaml_files: ContextVar[tuple[Path, ...]] = ContextVar("_yaml_files", default=())


class Paths(BaseModel):
    raw: Path = Path("data/raw")
    staging: Path = Path("data/staging")
    warehouse: Path = Path("data/warehouse.duckdb")


class CdrConfig(BaseModel):
    start: str = "2018Q1"
    end: str = "latest"
    request_delay_s: float = Field(MIN_REQUEST_DELAY_S, ge=MIN_REQUEST_DELAY_S)

    @field_validator("start", "end")
    @classmethod
    def _quarter_or_latest(cls, v: str) -> str:
        if v.strip().lower() == "latest":
            return "latest"
        return Quarter.parse(v).label


class HmdaConfig(BaseModel):
    years: list[int] = Field(default_factory=lambda: list(range(FIRST_HMDA_YEAR, 2026)))
    by_state: bool = False
    keep_raw_csv: bool = False

    @field_validator("years")
    @classmethod
    def _years_have_intro_rate_period(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("list at least one year")
        if early := sorted(y for y in v if y < FIRST_HMDA_YEAR):
            raise ValueError(
                f"{early} predate {FIRST_HMDA_YEAR}, the first LAR with intro_rate_period"
            )
        return sorted(set(v))


class SubsequentResets(BaseModel):
    enabled: bool = False
    frequency_months: int = Field(12, gt=0)


class ModelConfig(BaseModel):
    as_of: date = date(2026, 6, 30)
    calendar_years: tuple[int, int] = (2026, 2032)
    # Annual CPR including defaults. Placeholder assumptions, not estimates.
    scenarios: dict[str, float] = Field(
        default_factory=lambda: {"low": 0.06, "base": 0.10, "high": 0.15}
    )
    subsequent_resets: SubsequentResets = Field(default_factory=SubsequentResets)
    io_assumption: Literal["io_through_first_reset"] = "io_through_first_reset"

    @field_validator("calendar_years")
    @classmethod
    def _ordered(cls, v: tuple[int, int]) -> tuple[int, int]:
        if v[0] > v[1]:
            raise ValueError(f"first year {v[0]} is after last year {v[1]}")
        return v

    @field_validator("scenarios")
    @classmethod
    def _cpr_bounds(cls, v: dict[str, float]) -> dict[str, float]:
        if not v:
            raise ValueError("define at least one CPR scenario")
        if bad := {k: cpr for k, cpr in v.items() if not 0 <= cpr < 1}:
            raise ValueError(f"CPR must be an annual rate in [0, 1): {bad}")
        return v


class LlmConfig(BaseModel):
    base_url: str | None = None
    model: str | None = None
    api_key_env: str = "LLM_API_KEY"

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARMRESET_", env_nested_delimiter="__", extra="forbid"
    )

    contact_email: str = UNSET_CONTACT
    paths: Paths = Field(default_factory=Paths)
    cdr: CdrConfig = Field(default_factory=CdrConfig)
    hmda: HmdaConfig = Field(default_factory=HmdaConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)

    _config_path: Path | None = PrivateAttr(default=None)
    _config_files: tuple[Path, ...] = PrivateAttr(default=())
    _root: Path = PrivateAttr(default_factory=Path.cwd)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Highest priority first: explicit kwargs, then environment, then the YAML files
        # (config.local.yaml deep-merged over config.yaml).
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if yaml_files := _yaml_files.get():
            sources.append(
                YamlConfigSettingsSource(settings_cls, yaml_file=list(yaml_files), deep_merge=True)
            )
        return tuple(sources)

    @property
    def config_path(self) -> Path | None:
        return self._config_path

    @property
    def config_files(self) -> tuple[Path, ...]:
        """The YAML files that were read, lowest priority first."""
        return self._config_files

    @property
    def root(self) -> Path:
        """Project root: the directory holding config.yaml."""
        return self._root

    def resolve(self, path: Path) -> Path:
        path = path.expanduser()
        return path if path.is_absolute() else self._root / path

    @property
    def raw_dir(self) -> Path:
        return self.resolve(self.paths.raw)

    @property
    def staging_dir(self) -> Path:
        return self.resolve(self.paths.staging)

    @property
    def warehouse_path(self) -> Path:
        return self.resolve(self.paths.warehouse)

    @property
    def manifest_path(self) -> Path:
        return self.raw_dir / "manifest.json"

    def config_file(self, name: str) -> Path:
        """A file under ``config/`` next to config.yaml (mdrm.yaml, segments.yaml, ...)."""
        return self._root / "config" / name

    @property
    def contact_configured(self) -> bool:
        return self.contact_email.strip() not in ("", UNSET_CONTACT)

    @property
    def user_agent(self) -> str:
        contact = self.contact_email.strip() if self.contact_configured else "not configured"
        return f"arm-reset-research/{__version__} (+{PROJECT_URL}; contact: {contact})"


def find_config(explicit: Path | None = None) -> Path:
    """Locate config.yaml: ``explicit``, then ``$ARMRESET_CONFIG``, then ``./config.yaml``,
    then the checkout this package is installed from (editable installs)."""
    if explicit is not None:
        candidates = [Path(explicit)]
    elif env := os.environ.get(CONFIG_ENV_VAR):
        candidates = [Path(env)]
    else:
        candidates = [
            Path.cwd() / CONFIG_FILENAME,
            Path(__file__).resolve().parents[2] / CONFIG_FILENAME,
        ]
    for candidate in candidates:
        if candidate.expanduser().is_file():
            return candidate.expanduser().resolve()
    tried = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(
        f"No config file found (tried {tried}). Pass --config or set {CONFIG_ENV_VAR}."
    )


def load_settings(config: Path | None = None) -> Settings:
    """Load settings from ``config`` (or :func:`find_config`), the ``config.local.yaml`` next
    to it if there is one, and ``ARMRESET_*`` environment overrides."""
    path = find_config(config)
    local = path.with_name(LOCAL_CONFIG_FILENAME)
    files = (path, local) if local.is_file() and local != path else (path,)
    token = _yaml_files.set(files)
    try:
        settings = Settings()
    finally:
        _yaml_files.reset(token)
    settings._config_path = path
    settings._config_files = files
    settings._root = path.parent
    return settings
