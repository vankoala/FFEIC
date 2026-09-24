from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's ARMRESET_* overrides out of every test."""
    for key in list(os.environ):
        if key.startswith("ARMRESET_"):
            monkeypatch.delenv(key)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway project root holding copies of config.yaml and config/.

    ARMRESET_CONFIG points at it, so tests never read or write the real data/ directory.
    """
    root = tmp_path / "project"
    root.mkdir()
    shutil.copy(REPO_ROOT / "config.yaml", root / "config.yaml")
    shutil.copytree(REPO_ROOT / "config", root / "config")
    monkeypatch.setenv("ARMRESET_CONFIG", str(root / "config.yaml"))
    return root


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@pytest.fixture
def patch_config(project: Path) -> Callable[[dict[str, Any]], Path]:
    """Deep-merge a dict into the project's config.yaml and return the file's path."""

    def apply(patch: dict[str, Any]) -> Path:
        path = project / "config.yaml"
        merged = _deep_merge(yaml.safe_load(path.read_text()), patch)
        path.write_text(yaml.safe_dump(merged, sort_keys=False))
        return path

    return apply
