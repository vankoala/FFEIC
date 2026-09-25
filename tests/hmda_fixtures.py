"""Synthetic HMDA LAR rows in the verified layout (docs/verification.md, items 5-8).

Only the columns staging reads are written. Values are text as in the real CSV: fixed-rate
loans carry "NA" in intro_rate_period, exempt filers carry "Exempt" there and "1111" in the
coded fields, some integers have leading zeros, and loan amounts are $10k-band midpoints.
"""

from __future__ import annotations

import csv
from pathlib import Path

COLUMNS = [
    "activity_year", "lei", "derived_msa-md", "state_code", "conforming_loan_limit",
    "derived_dwelling_category", "action_taken", "purchaser_type", "loan_type", "loan_purpose",
    "lien_status", "reverse_mortgage", "open-end_line_of_credit", "loan_amount", "interest_rate",
    "loan_term", "intro_rate_period", "interest_only_payment", "occupancy_type",
]  # fmt: skip
SITE_BUILT = "Single Family (1-4 Units):Site-Built"
MANUFACTURED = "Single Family (1-4 Units):Manufactured"
BANK_LEI = "BANK0000000000000001"
CU_LEI = "CREDITUNION000000002"
IMC_LEI = "MORTGAGECO0000000003"
UNPANELLED_LEI = "NOTINPANEL0000000004"


def row(**overrides: str) -> dict[str, str]:
    base = {
        "activity_year": "2021",
        "lei": BANK_LEI,
        "derived_msa-md": "35620",
        "state_code": "NY",
        "conforming_loan_limit": "C",
        "derived_dwelling_category": SITE_BUILT,
        "action_taken": "1",
        "purchaser_type": "0",
        "loan_type": "1",
        "loan_purpose": "1",
        "lien_status": "1",
        "reverse_mortgage": "2",
        "open-end_line_of_credit": "2",
        "loan_amount": "305000",
        "interest_rate": "3.0",
        "loan_term": "360",
        "intro_rate_period": "NA",
        "interest_only_payment": "2",
        "occupancy_type": "1",
    }
    return base | overrides


# (label, row, staged rate_type or None when staging must drop it)
CASES: list[tuple[str, dict[str, str], str | None]] = [
    ("fixed NA", row(), "fixed"),
    ("exempt", row(intro_rate_period="Exempt", loan_term="Exempt", interest_rate="Exempt",
                   interest_only_payment="1111", lei=CU_LEI), "unknown"),
    ("arm 60/360", row(intro_rate_period="60", interest_rate="2.5", purchaser_type="1",
                       lei=IMC_LEI), "arm"),
    ("intro equals term", row(intro_rate_period="360"), "fixed"),
    ("intro zero", row(intro_rate_period="0"), "fixed"),
    ("arm leading zeros", row(intro_rate_period="00120", loan_term="00360", purchaser_type="4",
                              conforming_loan_limit="NC", loan_amount="1505000"), "arm"),
    ("arm io one month", row(intro_rate_period="1", interest_only_payment="1",
                             purchaser_type="99", lei=UNPANELLED_LEI), "arm"),
    ("arm manufactured", row(intro_rate_period="84", derived_dwelling_category=MANUFACTURED,
                             interest_rate="NA", purchaser_type="2"), "arm"),
    ("open-end", row(**{"open-end_line_of_credit": "1", "intro_rate_period": "1"}), None),
    ("reverse", row(reverse_mortgage="1"), None),
    ("multifamily", row(derived_dwelling_category="Multifamily:Site-Built"), None),
    ("purchased loan", row(action_taken="6"), None),
    ("junior lien", row(lien_status="2"), None),
]  # fmt: skip


def write_lar_csv(path: Path, rows: list[dict[str, str]] | None = None) -> Path:
    rows = [case_row for _, case_row, _ in CASES] if rows is None else rows
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


PANEL_HEADER = (
    "activity_year,lei,tax_id,agency_code,id_2017,respondent_rssd,respondent_name,"
    "respondent_state,respondent_city,assets,other_lender_code,parent_rssd,parent_name,"
    "topholder_rssd,topholder_name"
)


def panel_csv(rows: list[tuple[str, str, str, str, str]]) -> str:
    """rows: (lei, agency_code, respondent_rssd, name, other_lender_code)."""
    lines = [PANEL_HEADER]
    for lei, agency, rssd, name, olc in rows:
        lines.append(f'2021,{lei},00-0000000,{agency},,{rssd},"{name}",NY,CITY,1000,{olc},-1,,-1,')
    return "\r\n".join(lines) + "\r\n"


PANEL_ROWS = [
    (BANK_LEI, "1", "100", "BIG BANK, N.A.", "0"),
    (CU_LEI, "5", "555", "Hometown Credit Union", "0"),
    (IMC_LEI, "7", "-1", "Mortgage Co LLC", "3"),
]
