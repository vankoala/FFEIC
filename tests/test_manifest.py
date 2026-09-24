import hashlib
import json
from pathlib import Path

import pytest

from armreset.manifest import MANIFEST_VERSION, Manifest


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def manifest(tmp_path: Path) -> Manifest:
    return Manifest(tmp_path / "data" / "raw" / "manifest.json", root=tmp_path)


def test_record_hashes_file_and_persists(manifest: Manifest, tmp_path: Path) -> None:
    f = _write(tmp_path / "data" / "raw" / "cdr" / "q.zip", b"hello")
    entry = manifest.record(
        "cdr:2026Q2", "cdr", f, url="https://example.test/q.zip", period="2026-06-30"
    )
    assert entry.sha256 == hashlib.sha256(b"hello").hexdigest()
    assert entry.bytes == 5
    assert entry.path == "data/raw/cdr/q.zip"  # stored relative to the project root
    assert entry.fetched_at.endswith("+00:00")

    reloaded = Manifest(manifest.path, root=tmp_path)
    assert reloaded.get("cdr:2026Q2") == entry
    assert "cdr:2026Q2" in reloaded
    assert len(reloaded) == 1


def test_is_cached_needs_file_or_derived_output(manifest: Manifest, tmp_path: Path) -> None:
    raw = _write(tmp_path / "data" / "raw" / "hmda" / "lar_2021.csv", b"activity_year\n2021\n")
    manifest.record("hmda:2021:nationwide", "hmda", raw, period="2021")
    assert manifest.is_cached("hmda:2021:nationwide")

    parquet = _write(tmp_path / "data" / "staging" / "hmda" / "2021.parquet", b"PAR1")
    manifest.add_derived("hmda:2021:nationwide", parquet)
    raw.unlink()  # raw CSV deleted after conversion: still cached via the Parquet
    assert manifest.is_cached("hmda:2021:nationwide")

    parquet.unlink()  # both gone: the next fetch must download again
    assert not manifest.is_cached("hmda:2021:nationwide")
    assert not manifest.is_cached("never-recorded")


def test_add_derived_is_idempotent(manifest: Manifest, tmp_path: Path) -> None:
    raw = _write(tmp_path / "a.csv", b"x")
    out = _write(tmp_path / "a.parquet", b"y")
    manifest.record("k", "hmda", raw)
    manifest.add_derived("k", out)
    manifest.add_derived("k", out)
    assert manifest.get("k").derived == ["a.parquet"]


def test_re_recording_replaces_the_entry(manifest: Manifest, tmp_path: Path) -> None:
    f = _write(tmp_path / "f.zip", b"v1")
    first = manifest.record("cdr:2026Q2", "cdr", f)
    _write(f, b"version 2")
    second = manifest.record("cdr:2026Q2", "cdr", f)
    assert len(manifest) == 1
    assert second.sha256 != first.sha256
    assert second.bytes == len(b"version 2")


def test_save_is_atomic_and_versioned(manifest: Manifest, tmp_path: Path) -> None:
    manifest.record("k", "cdr", _write(tmp_path / "f", b"x"))
    assert not list(manifest.path.parent.glob("*.tmp"))
    payload = json.loads(manifest.path.read_text())
    assert payload["version"] == MANIFEST_VERSION
    assert set(payload["entries"]) == {"k"}


def test_paths_outside_root_are_stored_absolute(tmp_path: Path) -> None:
    root = tmp_path / "project"
    outside = _write(tmp_path / "downloads" / "manual.zip", b"z")
    m = Manifest(root / "manifest.json", root=root)
    entry = m.record("cdr:manual", "cdr", outside)
    assert Path(entry.path).is_absolute()
    assert m.is_cached("cdr:manual")


def test_entries_and_summary_group_by_source(manifest: Manifest, tmp_path: Path) -> None:
    manifest.record(
        "cdr:2026Q1", "cdr", _write(tmp_path / "q1.zip", b"a" * 10), period="2026-03-31"
    )
    manifest.record(
        "cdr:2026Q2", "cdr", _write(tmp_path / "q2.zip", b"b" * 20), period="2026-06-30"
    )
    gone = _write(tmp_path / "lar.csv", b"c" * 5)
    manifest.record("hmda:2021", "hmda", gone, period="2021")
    gone.unlink()

    assert [e.key for e in manifest.entries("cdr")] == ["cdr:2026Q1", "cdr:2026Q2"]
    assert len(manifest.entries()) == 3

    summary = {row["source"]: row for row in manifest.summary()}
    assert list(summary) == ["cdr", "hmda"]
    assert summary["cdr"]["files"] == 2
    assert summary["cdr"]["bytes"] == 30
    assert (summary["cdr"]["first_period"], summary["cdr"]["last_period"]) == (
        "2026-03-31",
        "2026-06-30",
    )
    assert summary["cdr"]["missing"] == 0
    assert summary["hmda"]["missing"] == 1


def test_corrupt_manifest_is_a_clear_error(tmp_path: Path) -> None:
    path = _write(tmp_path / "manifest.json", b"{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        Manifest(path, root=tmp_path)
