"""Download log (PLAN.md §9): one JSON file recording every fetched file.

Fetchers call :meth:`Manifest.is_cached` before downloading and :meth:`Manifest.record` once a
download has completed, so a file that is recorded and still on disk is never fetched twice.
A recorded file that has been deleted counts as not cached, and fetching it again replaces
the entry.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

    from armreset.settings import Settings

MANIFEST_VERSION = 1


@dataclass
class ManifestEntry:
    key: str  # stable logical id, e.g. "cdr:2026Q2" or "hmda:2021:nationwide"
    source: str  # cdr | hmda | hmda_panel | bbg
    path: str  # relative to the project root when the file is inside it
    sha256: str
    bytes: int
    fetched_at: str  # ISO 8601, UTC
    url: str | None = None
    period: str | None = None  # as-of period the file covers, e.g. "2026-06-30" or "2021"
    published: str | None = None  # the source's published date, where it gives one
    derived: list[str] = field(default_factory=list)  # outputs built from it, e.g. Parquet
    meta: dict[str, Any] = field(default_factory=dict)


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


class Manifest:
    def __init__(self, path: Path, root: Path) -> None:
        self.path = Path(path)
        self.root = Path(root)
        self._entries: dict[str, ManifestEntry] = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Manifest {self.path} is not valid JSON: {exc}") from exc
            for key, entry in data.get("entries", {}).items():
                self._entries[key] = ManifestEntry(**entry)

    @classmethod
    def for_settings(cls, settings: Settings) -> Manifest:
        return cls(settings.manifest_path, settings.root)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: str) -> bool:
        return key in self._entries

    def get(self, key: str) -> ManifestEntry | None:
        return self._entries.get(key)

    def entries(self, source: str | None = None) -> list[ManifestEntry]:
        return [e for _, e in sorted(self._entries.items()) if source is None or e.source == source]

    def abspath(self, stored: str) -> Path:
        path = Path(stored)
        return path if path.is_absolute() else self.root / path

    def is_cached(self, key: str) -> bool:
        """True when ``key`` is recorded and its file, or one of its derived outputs, exists."""
        entry = self._entries.get(key)
        if entry is None:
            return False
        return any(self.abspath(p).exists() for p in [entry.path, *entry.derived])

    def record(
        self,
        key: str,
        source: str,
        path: Path,
        *,
        url: str | None = None,
        period: str | None = None,
        published: str | None = None,
        derived: Iterable[Path] = (),
        meta: dict[str, Any] | None = None,
    ) -> ManifestEntry:
        """Hash ``path``, record it under ``key`` (replacing any earlier entry) and save."""
        path = Path(path)
        entry = ManifestEntry(
            key=key,
            source=source,
            path=self._stored(path),
            sha256=sha256_file(path),
            bytes=path.stat().st_size,
            fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
            url=url,
            period=period,
            published=published,
            derived=[self._stored(Path(d)) for d in derived],
            meta=dict(meta or {}),
        )
        self._entries[key] = entry
        self.save()
        return entry

    def add_derived(self, key: str, *paths: Path) -> ManifestEntry:
        entry = self._entries[key]
        for p in paths:
            if (stored := self._stored(Path(p))) not in entry.derived:
                entry.derived.append(stored)
        self.save()
        return entry

    def save(self) -> None:
        """Write the manifest atomically (temp file, then rename)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": MANIFEST_VERSION,
            "entries": {k: asdict(e) for k, e in sorted(self._entries.items())},
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def summary(self) -> list[dict[str, Any]]:
        """One row per source: file count, bytes, period range, last fetch, files missing."""
        rows: dict[str, dict[str, Any]] = {}
        for entry in self._entries.values():
            row = rows.setdefault(
                entry.source,
                {"source": entry.source, "files": 0, "bytes": 0, "periods": [], "missing": 0},
            )
            row["files"] += 1
            row["bytes"] += entry.bytes
            if entry.period:
                row["periods"].append(entry.period)
            row["last_fetched"] = max(row.get("last_fetched", ""), entry.fetched_at)
            row["missing"] += not self.is_cached(entry.key)
        for row in rows.values():
            periods = sorted(row.pop("periods"))
            row["first_period"] = periods[0] if periods else None
            row["last_period"] = periods[-1] if periods else None
        return [rows[s] for s in sorted(rows)]

    def _stored(self, path: Path) -> str:
        path = path.resolve()
        try:
            return path.relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return str(path)
