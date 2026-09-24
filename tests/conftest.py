from __future__ import annotations

import os
import shutil
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test away from the real data/ directory.

    Drops a developer's ARMRESET_* overrides, runs the test from a temp directory, and points
    ARMRESET_CONFIG at a file that doesn't exist, so a test that loads settings without the
    ``project`` fixture fails loudly instead of reading the repo's config and data.
    """
    for key in list(os.environ):
        if key.startswith("ARMRESET_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ARMRESET_CONFIG", str(tmp_path / "no-project-configured.yaml"))


class NetworkBlocked(BaseException):
    """Raised on any connection attempt. A BaseException, so no retry loop or ``except
    Exception`` can swallow it and the test fails at once."""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise NetworkBlocked(f"tests must not open network connections: {args[1:]!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


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
