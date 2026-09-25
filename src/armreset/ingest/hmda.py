"""HMDA LAR -> ``stg_hmda`` Parquet, partitioned by ``activity_year`` (PLAN.md §5.2).

Staging follows the plan's SQL, with one change from VERIFY item 5. The API takes at most two
filter criteria, so the dwelling filter (1-4 family, site-built and manufactured) moved from
the download to the WHERE clause here.

Each year is staged from exactly one source, the nationwide file if there is one and the
state files otherwise, so no loan is counted twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from armreset.fetch.hmda import DWELLING_CATEGORIES, SOURCE, STATES
from armreset.manifest import Manifest, ManifestEntry
from armreset.settings import Settings

log = logging.getLogger(__name__)

# PLAN.md §5.2 staging SQL. {source} is a read_parquet(...) over the year's raw file(s).
STAGE_SQL = """
SELECT
  CAST(activity_year AS INTEGER)            AS activity_year,
  lei,
  state_code,
  "derived_msa-md"                          AS msa_md,
  conforming_loan_limit,                    -- C / NC / U / NA
  derived_dwelling_category                 AS dwelling_category,
  TRY_CAST(purchaser_type AS INTEGER)       AS purchaser_type,
  TRY_CAST(loan_type      AS INTEGER)       AS loan_type,
  TRY_CAST(loan_purpose   AS INTEGER)       AS loan_purpose,
  TRY_CAST(occupancy_type AS INTEGER)       AS occupancy_type,
  TRY_CAST(loan_amount    AS DOUBLE)        AS loan_amount,
  TRY_CAST(interest_rate  AS DOUBLE)        AS interest_rate,
  TRY_CAST(loan_term      AS INTEGER)       AS loan_term_m,
  intro_rate_period                         AS intro_raw,
  TRY_CAST(intro_rate_period AS INTEGER)    AS intro_m,
  interest_only_payment = '1'               AS is_io,
  CASE
    WHEN intro_rate_period IN ('Exempt', '1111')                           THEN 'unknown'
    WHEN TRY_CAST(intro_rate_period AS INTEGER) IS NULL                    THEN 'fixed'
    WHEN TRY_CAST(intro_rate_period AS INTEGER) > 0
     AND (TRY_CAST(loan_term AS INTEGER) IS NULL
          OR TRY_CAST(intro_rate_period AS INTEGER) < TRY_CAST(loan_term AS INTEGER))
                                                                           THEN 'arm'
    ELSE 'fixed'
  END                                       AS rate_type,
  '{source_key}'                            AS source_key
FROM {source}
WHERE action_taken = '1'
  AND lien_status = '1'
  AND "open-end_line_of_credit" <> '1'
  AND reverse_mortgage <> '1'
  AND derived_dwelling_category IN ({dwellings})
"""


class HmdaStagingError(RuntimeError):
    pass


def stg_dir(settings: Settings) -> Path:
    return settings.staging_dir / "hmda"


def year_dir(settings: Settings, year: int) -> Path:
    return stg_dir(settings) / f"activity_year={year}"


def sources_for_year(manifest: Manifest, year: int) -> list[ManifestEntry]:
    """The raw files that make up one year: the nationwide file, or else the state files."""
    entries = [
        e for e in manifest.entries(SOURCE) if e.period == str(year) and manifest.is_cached(e.key)
    ]
    nationwide = [e for e in entries if e.key.endswith(":nationwide")]
    states = [e for e in entries if not e.key.endswith(":nationwide")]
    if nationwide:
        if states:
            log.warning(
                "%d: using the nationwide file and ignoring %d state files, which hold the "
                "same loans",
                year,
                len(states),
            )
        return nationwide
    if states and (missing := set(STATES) - {e.meta.get("state") for e in states}):
        raise HmdaStagingError(
            f"{year}: state files are missing for {sorted(missing)}; staging a partial year "
            "would undercount. Fetch them with `armtool fetch hmda --by-state`."
        )
    return states


def _parquet_of(manifest: Manifest, entry: ManifestEntry) -> Path:
    parquets = [manifest.abspath(d) for d in entry.derived if d.endswith(".parquet")]
    if not parquets or not parquets[0].exists():
        raise HmdaStagingError(f"{entry.key}: its Parquet file is missing; re-run fetch hmda")
    return parquets[0]


@dataclass
class StagedYear:
    year: int
    rows: int
    sources: list[str] = field(default_factory=list)
    reused: bool = False


def _marker(settings: Settings, year: int) -> Path:
    return year_dir(settings, year) / "_sources.txt"


def stage_year(
    settings: Settings, manifest: Manifest, year: int, force: bool = False
) -> StagedYear | None:
    """Write ``stg_hmda`` for one year unless it is already staged from the same raw files."""
    entries = sources_for_year(manifest, year)
    if not entries:
        return None
    signature = "\n".join(f"{e.key} {e.sha256}" for e in entries) + f"\n{STAGE_SQL}"
    marker = _marker(settings, year)
    out_dir = year_dir(settings, year)
    if not force and marker.exists() and marker.read_text(encoding="utf-8") == signature:
        rows = _count(out_dir)
        return StagedYear(year, rows, [e.key for e in entries], reused=True)

    tmp_dir = out_dir.with_name(out_dir.name + ".tmp")
    if tmp_dir.exists():
        for f in tmp_dir.iterdir():
            f.unlink()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dwellings = ", ".join(f"'{d}'" for d in DWELLING_CATEGORIES)
    con = duckdb.connect()
    try:
        for i, entry in enumerate(entries):
            source = _sql_path(_parquet_of(manifest, entry))
            sql = STAGE_SQL.format(
                source=f"read_parquet('{source}')", source_key=entry.key, dwellings=dwellings
            )
            target = _sql_path(tmp_dir / f"part-{i:03d}.parquet")
            con.execute(f"COPY ({sql}) TO '{target}' (FORMAT parquet, COMPRESSION zstd)")
    finally:
        con.close()
    if out_dir.exists():
        for f in out_dir.iterdir():
            f.unlink()
        out_dir.rmdir()
    tmp_dir.rename(out_dir)
    marker.write_text(signature, encoding="utf-8")
    return StagedYear(year, _count(out_dir), [e.key for e in entries])


def _count(directory: Path) -> int:
    con = duckdb.connect()
    try:
        glob = _sql_path(directory / "*.parquet")
        return con.execute(f"SELECT count(*) FROM read_parquet('{glob}')").fetchone()[0]
    finally:
        con.close()


def _sql_path(path: Path) -> str:
    return path.as_posix().replace("'", "''")


def staged_years(settings: Settings) -> list[int]:
    root = stg_dir(settings)
    if not root.exists():
        return []
    return sorted(
        int(d.name.split("=", 1)[1])
        for d in root.iterdir()
        if d.is_dir() and d.name.startswith("activity_year=") and not d.name.endswith(".tmp")
    )
