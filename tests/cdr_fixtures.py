"""Synthetic CDR bulk zips in the verified layout (docs/verification.md, items 1-3).

POR has one header row; schedule files add a description row whose IDRSSD is blank and end
every line with a tab; lines end in CRLF; the "IDRSSD" header is quoted. RC-C Part I is split
into two parts here (the real 2018-2026 files keep it whole) so the part join gets exercised.
Values are in thousands of dollars, as filed. The numbers are made up to trigger each QA rule.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

POR_HEADER = [
    '"IDRSSD"',
    "FDIC Certificate Number",
    "OCC Charter Number",
    "OTS Docket Number",
    "Primary ABA Routing Number",
    "Financial Institution Name",
    "Financial Institution Address",
    "Financial Institution City",
    "Financial Institution State",
    "Financial Institution Zip Code",
    "Financial Institution Filing Type",
    "Last Date/Time Submission Updated On",
]
BUCKETS = ["RCONA564", "RCONA565", "RCONA566", "RCONA567", "RCONA568", "RCONA569"]
SCHEDULES = {
    "RC": [["RCFD2170", "RCON2170", "RCON3300"]],
    "RCCI": [["RCFD5367", "RCON5367", *BUCKETS[:3]], BUCKETS[3:]],  # two parts
    "RCCII": [["RCONB575"]],  # a neighbour whose name starts with RCCI
    "RCN": [["RCONC229"]],
}


def _buckets(*values: str) -> dict[str, str]:
    return dict(zip(BUCKETS, values, strict=True))


BANKS: dict[str, dict] = {
    # 031 filer: consolidated RCFD5367 differs from domestic RCON5367; buckets reconcile to RCON.
    "100": {
        "name": "BIG BANK, N.A.",
        "form": "031",
        "cert": "1001",
        "values": {
            "RCFD2170": "1000",
            "RCFD5367": "600",
            "RCON5367": "500",
            **_buckets("10", "20", "30", "40", "100", "290"),
            "RCONC229": "10",
        },
    },
    # Buckets exceed the total by $1k: rounding.
    "200": {
        "name": "ROUNDING BANK",
        "form": "041",
        "cert": "1002",
        "values": {
            "RCON2170": "500",
            "RCON5367": "100",
            **_buckets("10", "10", "10", "10", "30", "31"),
            "RCONC229": "0",
        },
    },
    # Reports no buckets.
    "300": {
        "name": "NO BUCKETS BANK",
        "form": "051",
        "cert": "1003",
        "values": {"RCON2170": "50", "RCON5367": "40", "RCONC229": "0"},
    },
    # Implied nonaccrual is 40% of first-lien, and it matches the reported nonaccrual.
    "400": {
        "name": "HIGH NONACCRUAL BANK",
        "form": "051",
        "cert": "1004",
        "values": {
            "RCON2170": "200",
            "RCON5367": "100",
            **_buckets("10", "10", "10", "10", "10", "10"),
            "RCONC229": "40",
        },
    },
    # Implied nonaccrual 20 vs reported 5.
    "500": {
        "name": "MISMATCH BANK",
        "form": "041",
        "cert": "1005",
        "values": {
            "RCON2170": "300",
            "RCON5367": "100",
            **_buckets("10", "10", "10", "10", "20", "20"),
            "RCONC229": "5",
        },
    },
    # One bucket blank, and total assets marked confidential.
    "600": {
        "name": "PARTIAL BANK",
        "form": "041",
        "cert": "0",
        "values": {
            "RCON2170": "CONF",
            "RCON5367": "100",
            **_buckets("10", "10", "", "30", "20", "30"),
            "RCONC229": "0",
        },
    },
    # Buckets exceed the total by $10k: beyond rounding.
    "700": {
        "name": "OVER BANK",
        "form": "041",
        "cert": "1007",
        "values": {
            "RCON2170": "300",
            "RCON5367": "100",
            **_buckets("20", "20", "20", "20", "15", "15"),
            "RCONC229": "0",
        },
    },
}


def tsv(
    header: list[str],
    rows: list[list[str]],
    descriptions: list[str] | None = None,
    trailing_tab: bool = True,
) -> bytes:
    tail = "\t" if trailing_tab else ""
    lines = ["\t".join(header) + tail]
    if descriptions is not None:
        lines.append("\t".join(descriptions) + tail)
    lines += ["\t".join(row) + tail for row in rows]
    return ("\r\n".join(lines) + "\r\n").encode()


def schedule_file(columns: list[str], banks: dict[str, dict]) -> bytes:
    rows = [[rssd, *(b["values"].get(c, "") for c in columns)] for rssd, b in banks.items()]
    return tsv(['"IDRSSD"', *columns], rows, ["", *(f"DESC OF {c}" for c in columns)])


def por_file(banks: dict[str, dict]) -> bytes:
    rows = [
        [
            rssd,
            b["cert"],
            "0",
            "0",
            "1",
            b["name"],
            "1 MAIN ST ",
            "CITY",
            "NY",
            "10001",
            b["form"],
            "2026-07-30T10:00:00",
        ]
        for rssd, b in banks.items()
    ]
    return tsv(POR_HEADER, rows, trailing_tab=False)


def make_cdr_zip(
    directory: Path,
    stamp: str = "06302026",
    banks: dict[str, dict] | None = None,
    readme_as_of: str | None = "2026-09-15T04:30:03",
) -> Path:
    """Write "FFIEC CDR Call Bulk All Schedules <stamp>.zip" into ``directory``."""
    banks = BANKS if banks is None else banks
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"FFIEC CDR Call Bulk All Schedules {stamp}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"FFIEC CDR Call Bulk POR {stamp}.txt", por_file(banks))
        for code, parts in SCHEDULES.items():
            for i, columns in enumerate(parts, start=1):
                suffix = f"({i} of {len(parts)})" if len(parts) > 1 else ""
                zf.writestr(
                    f"FFIEC CDR Call Schedule {code} {stamp}{suffix}.txt",
                    schedule_file(columns, banks),
                )
        if readme_as_of:
            zf.writestr(
                "Readme.txt",
                "This file contains data from Call Reports received and processed by \r\n"
                f"the FFIEC Central Data Repository (CDR) as of {readme_as_of} \r\n",
            )
    return path
