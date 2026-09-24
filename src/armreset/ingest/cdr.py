"""CDR bulk zip -> staging Parquet (PLAN.md §5.1).

One Parquet file per quarter, ``data/staging/cdr/stg_cdr_<YYYY-MM-DD>.parquet``. Each file holds
bank identity from the POR file plus the raw text of every MDRM column the model uses, as
filed (thousands of dollars; blank means not reported). The parsing rules follow what the
real files showed; see docs/verification.md, items 1-3.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import polars as pl
import yaml

log = logging.getLogger(__name__)

# Real names: "FFIEC CDR Call Bulk POR 06302026.txt", "FFIEC CDR Call Schedule RCCI 06302026.txt"
# and "FFIEC CDR Call Schedule RCB 06302026(1 of 2).txt". The part suffix follows the date.
MEMBER_RE = re.compile(
    r"FFIEC CDR Call (?:Bulk (?P<por>POR)|Schedule (?P<code>[A-Z0-9]+)) (?P<stamp>\d{8})"
    r"(?:\((?P<part>\d+) of (?P<parts>\d+)\))?\.txt"
)
# Readme.txt: "... processed by the FFIEC Central Data Repository (CDR) as of 2026-09-15T04:30:03"
README_AS_OF_RE = re.compile(r"as of\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")

POR_COLUMNS = {
    "IDRSSD": "rssd_id",
    "FDIC Certificate Number": "fdic_cert",
    "Financial Institution Name": "name",
    "Financial Institution City": "city",
    "Financial Institution State": "state",
    "Financial Institution Filing Type": "form",
    "Last Date/Time Submission Updated On": "last_submission",
}


class CdrFormatError(ValueError):
    """A bulk file doesn't match the layout the parser was verified against."""


@dataclass(frozen=True)
class MdrmSpec:
    """``config/mdrm.yaml``: which MDRM codes feed which columns, and the prefix order for each."""

    buckets: dict[str, str]
    first_lien_total: str
    total_assets: str
    nonaccrual_first_lien: str
    prefixes: tuple[str, ...]
    overrides: dict[str, tuple[str, ...]]
    schedules: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> MdrmSpec:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(
            buckets={name: str(code) for name, code in raw["repricing_buckets"].items()},
            first_lien_total=str(raw["first_lien_total"]),
            total_assets=str(raw["total_assets"]),
            nonaccrual_first_lien=str(raw["nonaccrual_first_lien"]),
            prefixes=tuple(raw["prefixes"]),
            overrides={str(k): tuple(v) for k, v in (raw.get("prefix_overrides") or {}).items()},
            schedules=tuple(raw["schedules"]),
        )

    def prefixes_for(self, code: str) -> tuple[str, ...]:
        return self.overrides.get(code, self.prefixes)

    @property
    def items(self) -> dict[str, str]:
        """Fact-table column -> MDRM code, in fact-table order."""
        return {
            "total_assets": self.total_assets,
            "first_lien_total": self.first_lien_total,
            **self.buckets,
            "reported_nonaccrual": self.nonaccrual_first_lien,
        }

    @property
    def wanted_columns(self) -> list[str]:
        """Every prefix variant of every code. Staging keeps RCFD and RCON side by side even
        where the model uses only one, so the choice stays auditable."""
        all_prefixes = [*self.prefixes, *(p for ps in self.overrides.values() for p in ps)]
        prefixes = list(dict.fromkeys(all_prefixes))
        return [p + code for code in self.items.values() for p in prefixes]


@dataclass(frozen=True)
class Member:
    name: str
    code: str  # "POR", "RC", "RCCI", ...
    stamp: str  # MMDDYYYY
    part: int = 1
    parts: int = 1


def parse_member(name: str) -> Member | None:
    m = MEMBER_RE.fullmatch(Path(name).name)
    if m is None:
        return None
    return Member(
        name, m["por"] or m["code"], m["stamp"], int(m["part"] or 1), int(m["parts"] or 1)
    )


def index_members(names: Iterable[str]) -> dict[str, list[Member]]:
    """Group a zip's data files by schedule code, parts in order; checks no part is missing."""
    groups: dict[str, list[Member]] = {}
    for name in names:
        if (member := parse_member(name)) is not None:
            groups.setdefault(member.code, []).append(member)
    for code, parts in groups.items():
        parts.sort(key=lambda m: m.part)
        expected = list(range(1, parts[0].parts + 1))
        if [m.part for m in parts] != expected or {m.parts for m in parts} != {parts[0].parts}:
            found = ", ".join(f"{m.part} of {m.parts}" for m in parts)
            raise CdrFormatError(f"Schedule {code} has an incomplete set of parts: {found}")
    return groups


def _utf8(raw: bytes, name: str) -> bytes:
    """Every file checked so far is pure ASCII. Bytes that aren't valid UTF-8 are decoded as
    Windows-1252, with a warning, rather than being lossily replaced."""
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        log.warning("%s is not valid UTF-8; decoding it as Windows-1252", name)
        return raw.decode("cp1252").encode("utf-8")
    return raw


def read_cdr_file(raw: bytes, name: str = "<bytes>") -> pl.DataFrame:
    """Parse one bulk file into an all-text DataFrame keyed by ``IDRSSD``.

    Tab-delimited with no quoting; the "IDRSSD" header is quoted and the quotes are stripped.
    Schedule files carry a second header row of item descriptions whose IDRSSD is blank;
    POR files don't, so the row is detected rather than assumed. Every schedule line ends in a
    tab, which yields an unnamed column that is dropped. Ragged rows raise.
    """
    df = pl.read_csv(
        io.BytesIO(_utf8(raw, name)),
        separator="\t",
        infer_schema_length=0,
        quote_char=None,
        truncate_ragged_lines=False,
    )
    df = df.rename({c: c.strip().strip('"') for c in df.columns})
    df = df.drop([c for c in df.columns if c == ""])
    if "IDRSSD" not in df.columns:
        raise CdrFormatError(f"{name}: no IDRSSD column")
    if df.height and not (df["IDRSSD"][0] or "").strip():
        df = df.slice(1)  # the description row
    df = df.with_columns(pl.col("IDRSSD").str.strip_chars())
    if df["IDRSSD"].is_null().any() or (df["IDRSSD"] == "").any():
        raise CdrFormatError(f"{name}: rows without an IDRSSD")
    if df["IDRSSD"].is_duplicated().any():
        raise CdrFormatError(f"{name}: duplicate IDRSSD values")
    return df


def read_schedule(zf: zipfile.ZipFile, members: list[Member]) -> pl.DataFrame:
    """Read a schedule and join its parts on IDRSSD. Parts split columns, not rows."""
    out = read_cdr_file(zf.read(members[0].name), members[0].name)
    for member in members[1:]:
        part = read_cdr_file(zf.read(member.name), member.name)
        if repeated := (set(part.columns) & set(out.columns)) - {"IDRSSD"}:
            raise CdrFormatError(
                f"{member.name} repeats columns from earlier parts: {sorted(repeated)}"
            )
        if set(part["IDRSSD"]) != set(out["IDRSSD"]):
            log.warning("%s covers a different set of banks from part 1", member.name)
        out = out.join(part, on="IDRSSD", how="full", coalesce=True)
    return out


@dataclass
class StageResult:
    report_date: date
    path: Path
    n_banks: int
    present: list[str]  # wanted MDRM columns found in the files
    absent: list[str]  # wanted MDRM columns not in any schedule (staged as nulls)
    non_numeric: dict[str, int]  # column -> non-blank values that aren't numbers (e.g. CONF)
    data_as_of: str | None  # CDR processing timestamp from Readme.txt


def stage_quarter(
    zip_path: Path, report_date: date, spec: MdrmSpec, out_path: Path, source_sha256: str
) -> StageResult:
    """Extract one quarter's bank identity and wanted MDRM columns into a staging Parquet."""
    stamp = report_date.strftime("%m%d%Y")
    with zipfile.ZipFile(zip_path) as zf:
        members = index_members(zf.namelist())
        if wrong := sorted(m.name for ms in members.values() for m in ms if m.stamp != stamp):
            raise CdrFormatError(f"{zip_path.name}: files for another date: {wrong[:3]}")
        if missing := [c for c in ("POR", *spec.schedules) if c not in members]:
            raise CdrFormatError(f"{zip_path.name}: no file for {missing}")
        por = read_schedule(zf, members["POR"])
        schedules = {code: read_schedule(zf, members[code]) for code in spec.schedules}
        readme = zf.read("Readme.txt").decode("latin-1") if "Readme.txt" in zf.namelist() else ""

    if missing_por := [c for c in POR_COLUMNS if c not in por.columns]:
        raise CdrFormatError(f"{zip_path.name}: POR file lacks {missing_por}")
    frame = por.select(
        [pl.col(src).str.strip_chars().alias(dst) for src, dst in POR_COLUMNS.items()]
    )

    present: list[str] = []
    taken: set[str] = set()
    for code, df in schedules.items():
        cols = [c for c in spec.wanted_columns if c in df.columns and c not in taken]
        taken.update(cols)
        present.extend(cols)
        if extra := set(df["IDRSSD"]) - set(frame["rssd_id"]):
            log.warning("%s: %d banks in schedule %s are not in POR", stamp, len(extra), code)
        frame = frame.join(
            df.select(pl.col("IDRSSD").alias("rssd_id"), *cols), on="rssd_id", how="left"
        )
    absent = [c for c in spec.wanted_columns if c not in taken]
    as_of = README_AS_OF_RE.search(readme)
    frame = frame.with_columns([pl.lit(None, dtype=pl.Utf8).alias(c) for c in absent]).select(
        pl.col("rssd_id").cast(pl.Int64),
        pl.lit(report_date).alias("report_date"),
        *[c for c in POR_COLUMNS.values() if c != "rssd_id"],
        *spec.wanted_columns,
        pl.lit(zip_path.name).alias("source_file"),
        pl.lit(source_sha256).alias("source_sha256"),
        pl.lit(as_of[1] if as_of else None, dtype=pl.Utf8).alias("cdr_data_as_of"),
    )

    non_numeric_counts = {
        c: n for c in present if (n := frame.select(non_numeric(c).sum()).item()) > 0
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    frame.write_parquet(tmp)
    tmp.replace(out_path)
    return StageResult(
        report_date,
        out_path,
        frame.height,
        present,
        absent,
        non_numeric_counts,
        as_of[1] if as_of else None,
    )


def non_numeric(col: str) -> pl.Expr:
    """True where a raw MDRM value is filled in but isn't a number (e.g. ``CONF``)."""
    text = pl.col(col).str.strip_chars()
    return text.is_not_null() & (text != "") & text.cast(pl.Float64, strict=False).is_null()
