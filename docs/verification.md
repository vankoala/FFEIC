# Verification log

Each VERIFY item in `PLAN.md` gets checked against a real file or a live endpoint. The result
goes here: what was checked, when, against what, what was found, and what changed in the
code. The Methodology page summarizes these results.

Status key:
- **open**: not checked yet.
- **confirmed**: the plan's assumption held.
- **adjusted**: it didn't hold, and the code now follows the file.

## Summary

| # | Item (PLAN.md §13 unless noted) | Checked in | Status |
|---|---|---|---|
| 1 | CDR zip names, schedule regex, header and description rows, multi-part split, encoding | phase 1 | open |
| 2 | RCFD vs RCON columns for 5367, 2170 and A564–A569 by form type | phase 1 | open |
| 3 | Whether FFIEC 051 filers report RC-C Memo item 2.a | phase 1 | open |
| 4 | `ffiec-data-collector` against the current CDR page | phases 0–1 | version pinned; live test open |
| 5 | HMDA nationwide CSV: size, run time, redirects, multi-value `dwelling_categories` | phase 3 | open |
| 6 | `intro_rate_period` for fixed-rate (`NA`) and exempt (`Exempt` vs `1111`) rows | phase 3 | open |
| 7 | Panel download path; `agency_code` / `other_lender_code` → `lender_type` | phase 3 | open; names-only fallback confirmed |
| 8 | Public `loan_amount` is the $10k-band midpoint | phase 3 | open |
| §5.1 | A567–A569 descriptions match the bucket labels | phase 1 | open |
| §5.2 | `intro_m` histogram clusters near 12, 36, 60, 84 and 120 | phase 3 | open |
| §11 | One large bank's six buckets match its Call Report PDF | phase 1 | open |

## Phase 0 (2026-09-24)

### Endpoint reachability

One request per host, 5 seconds apart, with a descriptive User-Agent:

| Request | Result |
|---|---|
| `HEAD https://cdr.ffiec.gov/public/PWS/DownloadBulkData.aspx` | 200 |
| `HEAD https://ffiec.cfpb.gov/v2/data-browser-api/view/filers?years=2021` | **405** |
| `GET` on the same URL | 200, JSON, 400 KB |

- The Data Browser API rejects HEAD, so the fetchers must never probe it with HEAD before a
  GET.
- The filers response has the shape `{"institutions": [{"lei", "name", "count", "period"}, ...]}`.
  That confirms the names-only fallback for the panel (§5.3, item 7). Whether the full
  panel file downloads is still open.

### Item 4: `ffiec-data-collector` version

- PyPI lists exactly one release: `2.0.0rc2` (uploaded 2025-08-11). It requires
  Python ≥ 3.10 and depends on `requests` and `python-dateutil`.
- It is pinned as `ffiec-data-collector==2.0.0rc2` in `pyproject.toml`, and `uv.lock`
  records its hash. It installs and imports.
- Whether it still works against the live CDR page gets tested on the first real
  download in phase 1.

### Gaps in the plan found while writing the config

- **purchaser_type 4 (Farmer Mac) is missing from the §7.2 holder-segment table.**
  - Farmer Mac is not an agency MBS pool, so `config/segments.yaml` maps code 4 to `other`
    (unknown holder) rather than dropping it.
  - Any code missing from the table maps to `unmapped`, and the QA report counts those rows.
  - The code list gets confirmed in phase 3 against the LAR data dictionary and the 2021
    file.
- **Credit unions in HMDA may not all use agency code 5.**
  - §5.3 and §14 identify credit unions by `agency_code = 5` (NCUA).
  - Credit unions with over $10bn in assets may report under the CFPB (agency code 9)
    instead. If so, `agency_code = 5` undercounts credit unions.
  - This gets checked in phase 3 under item 7, before the lender-type mapping is final.
