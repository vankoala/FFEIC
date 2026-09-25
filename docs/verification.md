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
| 1 | CDR zip names, schedule regex, header and description rows, multi-part split, encoding | phases 1–2 | **adjusted**: regex, POR header, trailing tab; holds for all 34 quarters |
| 2 | RCFD vs RCON columns for 5367, 2170 and A564–A569 by form type | phases 1–2 | **adjusted**: 5367 and the buckets use RCON only; same columns in all 34 quarters |
| 3 | Whether FFIEC 051 filers report RC-C Memo item 2.a | phases 1–2 | **confirmed**: all do, in all 34 quarters; coverage 100% |
| 4 | `ffiec-data-collector` against the current CDR page | phases 0–1 | **confirmed**, with our session swapped in |
| 5 | HMDA nationwide CSV: size, run time, redirects, multi-value `dwelling_categories` | phase 3 | open |
| 6 | `intro_rate_period` for fixed-rate (`NA`) and exempt (`Exempt` vs `1111`) rows | phase 3 | open |
| 7 | Panel download path; `agency_code` / `other_lender_code` → `lender_type` | phase 3 | open; names-only fallback confirmed |
| 8 | Public `loan_amount` is the $10k-band midpoint | phase 3 | open |
| §5.1 | A567–A569 descriptions match the bucket labels | phase 1 | **confirmed** on the form; the bulk label for A568 is wrong |
| §5.1 | The buckets plus nonaccrual equal the first-lien total | phases 1–2 | **confirmed** for all 166,554 bank-quarters; checked on every build |
| §5.2 | `intro_m` histogram clusters near 12, 36, 60, 84 and 120 | phase 3 | open |
| §11 | One large bank's six buckets match its Call Report PDF | phase 1 | **passed**: JPMorgan Chase Bank, 2026-06-30 |

## Phase 2 (2026-09-25): Call Report history, 2018Q1–2026Q2

### Download

- **27 quarters downloaded:** 2019Q3–2026Q1.
- **110 requests in 9 min 21 s:** two to list CDR's periods, then four per quarter, each at
  least 5 s apart.
- **The 7 quarters already in the manifest were skipped**, with no requests.
- **Zip sizes:** 5.2–7.9 MB each, 34 zips in all.
- **CDR's "Call Updated" date was 2026-09-15 throughout.**

### Layout, re-checked on every zip

| Check | 34 quarters |
|---|---|
| Every data file matches the corrected member-name regex | yes; 48–51 files per zip |
| RC-C Part I (`RCCI`) in one file | yes, every quarter |
| RC-C Part I columns (5367 and A564–A569, both prefixes) the same as 2026Q2 | yes |
| Non-ASCII bytes | none |
| Columns with data among the 18 staged variants | the same 11 in every quarter: RCFD2170, RCON2170, RCFD5367, RCON5367, RCONA564–A569, RCONC229 |

- **RC-N (past-due and nonaccrual) is split into parts in 2018Q1–2023Q4**, and whole from
  2024Q1. The plan expected splits in RC-C Part I. They occur instead in RC-N, the schedule
  that supplies reported nonaccrual.
- **The split changes nothing.** The parts are joined on IDRSSD, and every bank-quarter
  still reconciles (below).
- **Other schedules are split in every quarter:** RCB, RCL, RCO, RCQ, RCRII and RCT.
- **CDR rebuilds older bulk files as amendments arrive.** The Readme timestamps show it:
  2018Q1–2020Q4 files were last rebuilt between 2024-04-15 and 2026-08-15, and 2021Q1 on
  2026-01-15. From 2021Q2 on, all were rebuilt on 2026-09-15.

### Reconciliation across the history

- **Every bank-quarter reconciles.** All 166,554 bank-quarters reconcile implied nonaccrual
  to reported nonaccrual (RCONC229) within $5k, 118,736 of them exactly. The industry totals
  over all quarters are $636.706bn implied vs $636.703bn reported.
- **Flags over the whole history:**
  - 7,119 rounding-level negative gaps (−$5k to $0).
  - 166 bank-quarters with implied nonaccrual above 25% of first-lien.
  - Nothing else: no mismatches, no negative gaps beyond rounding, no missing or partial
    buckets, and no non-numeric values.
- **Filers by form:** 2,735 FFIEC 031 bank-quarters (all using RCFD2170 for total assets),
  42,716 FFIEC 041 and 121,103 FFIEC 051.
- **Coverage:** every 051 filer reports all six buckets in every quarter, so bucket coverage
  is 100% of first-lien balances throughout.

## Phase 1 (2026-09-24): Call Report bulk files

### Files checked

| Quarter | Zip | Bytes | sha256 (first 16) | CDR data as of (Readme.txt) |
|---|---|---|---|---|
| 2026Q2 | `FFIEC CDR Call Bulk All Schedules 06302026.zip` | 5,980,988 | `0a2d59ce16b5a5b7` | 2026-09-15T04:30:03 |
| 2018Q1–2019Q2 | same naming, one zip per quarter | 7.3–7.9 MB each | see `data/raw/manifest.json` | 2025-05-15 to 2026-08-15 |

All seven zips came from "Call Reports -- Single Period", tab-delimited. The CDR page showed
"Call Updated: 9/15/2026" when they were downloaded, and the manifest records that as each
file's published date. The older quarters' Readme dates show that CDR regenerates old bulk
files as amendments come in.

### Item 1: file layout (adjusted)

| Plan assumption | What the files show | Code change |
|---|---|---|
| Regex `Schedule CODE (n of N) DATE` | POR is `FFIEC CDR Call Bulk POR 06302026.txt`. Schedules are `FFIEC CDR Call Schedule RCCI 06302026.txt`. The part suffix follows the date with no space: `... Schedule RCB 06302026(1 of 2).txt`. With the plan's regex, parts would go unrecognized. | New `MEMBER_RE`; `index_members()` also checks that no part is missing. |
| Row 2 holds descriptions; skip it | True for the schedule files, where row 2's IDRSSD is blank. **POR has no description row**, so skipping row 2 unconditionally would drop the first bank. | The description row is detected (blank IDRSSD), not assumed. |
| Key column `IDRSSD` | Yes. The header is quoted (`"IDRSSD"`); the other headers aren't. | Quotes stripped from header names. |
| Multi-part files split columns | Confirmed on RCB: both parts have 4,297 rows and the same IDRSSD set, and share no columns besides IDRSSD. RC-C Part I (`RCCI`) is a single file in all seven quarters. | Parts are joined on IDRSSD. A column repeated across parts raises an error. |
| (not in plan) | **Every schedule line ends with a tab**, header and data alike. This creates an unnamed column, which would collide when joining parts. | The unnamed column is dropped. |
| Blank = not reported | Confirmed. Some cells hold `CONF` (confidential), but in RC-C Part I only `RCONLG24`/`RCONLG25`, not the items used. | Any non-numeric value in a used column gets an `unparseable_value` flag, never a silent null. |
| Amounts in thousands | Confirmed. JPMorgan's RCFD2170 is 4,091,315,000 in both the bulk file and its own filed report: $4.09 trillion. | `resolve()` multiplies by 1,000. |
| Encoding may not be UTF-8 | Every member of all seven zips is pure ASCII, with CRLF line endings. | Strict UTF-8, with a logged Windows-1252 fallback rather than `utf8-lossy`. |
| (not in plan) | No ragged rows in any file. Fields starting with `"` occur only in free-text schedules (RCM, RIE, NARR). | CSV quoting is off. Ragged rows raise instead of being truncated. |

Column alignment check: RC item 12 (total assets, 2170) equals item 29 (total liabilities
and capital, 3300) for all 4,297 banks in 2026Q2.

### Item 2: RCFD vs RCON (adjusted)

Non-blank values by form, 2026Q2:

| Item | RCFD column | RCON column |
|---|---|---|
| 2170 total assets | 031: 80 of 80; 041/051: none | 041: 934 of 934; 051: 3,283 of 3,283 |
| 5367 first-lien 1–4 family | 031: 50 of 80 | all 4,297 |
| A564–A569 Memo 2.a buckets | column absent | all 4,297 |
| C229 nonaccrual first-lien | column absent | all 4,297 |

The 2018Q1 and 2019Q2 files contain the same columns.

- **Memo 2.a is reported for domestic offices only (RCON), on every form.** The form's
  caption says so: "Closed-end loans secured by first liens on 1-4 family residential
  properties in domestic offices (reported in Schedule RC-C, Part I, item 1.c.(2)(a),
  column B)". Column B is RCON5367.
- **RCFD5367 is column A (consolidated), and it includes foreign offices.**
  - For 8 of the 50 031 filers that report it, RCFD5367 differs from RCON5367: Citibank
    (+$19.2bn), JPMorgan Chase (+$3.1bn), First Hawaiian (+$158mm), Banco Popular de
    Puerto Rico (+$135mm), Bank of Hawaii (+$105mm), FirstBank Puerto Rico (+$79mm),
    Cathay (+$6mm) and East West (+$0.7mm).
  - Across the 50 banks: RCFD $1,713.9bn vs RCON $1,691.1bn.
- **The plan's RCFD→RCON order would compare a consolidated first-lien total with domestic
  buckets.** That inflates implied nonaccrual (by $19bn at Citibank) and makes the QA flags
  meaningless.
- **Change:** `config/mdrm.yaml` gains `prefix_overrides`.
  - 5367, A564–A569 and C229 use RCON only.
  - Total assets keeps RCFD then RCON.
  - Staging keeps both prefixes, so the choice stays auditable.
  - `prefix_source` records the columns used for each bank-quarter, e.g.
    `RCFD2170,RCON5367,RCONA564-A569,RCONC229`.

### Item 3: FFIEC 051 filers and Memo 2.a (confirmed)

All 3,283 FFIEC 051 filers report all six buckets in 2026Q2. In every quarter built so far
(2018Q1–2019Q2 and 2026Q2), every bank reports all six, so the buckets cover 100% of
first-lien balances. The QA report still computes coverage every quarter, in case this
changes.

### Item 4: `ffiec-data-collector` 2.0.0rc2 (confirmed, with changes)

- **It works against the live page.** Seven quarters downloaded.
- **Out of the box it breaks PLAN.md §0** in three ways:
  - it sends 4–6 requests per download with no pause between them;
  - it spoofs a Chrome User-Agent;
  - it sets no timeouts.
- **Change:** `CollectorSource` swaps in our `PoliteSession`, which fixes all three:
  - at least `cdr.request_delay_s` (5 s) between one request ending and the next starting;
  - our descriptive User-Agent;
  - (60 s, 900 s) timeouts.
- **Observed on the 2026Q2 download:** six requests at 20:58:12, :17, :22, :27, :32 and :37
  UTC, 27 s in total.
- **Retries:** only connection errors, timeouts and HTTP 5xx are retried, at most twice,
  with backoff. A page-structure error goes straight to the manual-download message.
- **Hand-placed zips:** the fetcher still accepts zips placed by hand in `data/raw/cdr/`.
- **Listed periods:** CDR lists them newest first. On 2026-09-24 the newest was 06/30/2026,
  which matches the plan and the 45-day posting heuristic.

### §5.1: bucket descriptions (confirmed on the form; bulk label for A568 is wrong)

| MDRM | Bulk-file description (row 2) | Caption on the filed report (SDF and PDF) | Line |
|---|---|---|---|
| RCONA564 | CLSD-END LNS SECD 1ST LIENS 3 MOS LE | Three months or less | M.2.a.(1) |
| RCONA565 | CLSD-END LNS SECD 1ST LIENS OV3-12 M | Over three months through 12 months | M.2.a.(2) |
| RCONA566 | CLSD-END LNS SECD 1ST LIENS OV 1-3 Y | Over one year through three years | M.2.a.(3) |
| RCONA567 | CLSD-END LNS SECD 1ST LIENS OV 3-5 Y | Over three years through five years | M.2.a.(4) |
| RCONA568 | CLSD-END LNS SECD 1ST LIENS **OVR 15 Y** | **Over five years through 15 years** | M.2.a.(5) |
| RCONA569 | LOANS SECD BY RE MAT OVER 15 YEARS | Over 15 years | M.2.a.(6) |

- The plan's mapping is right. The bulk file's short label for A568 is a mislabel.
- The data agrees: the six buckets plus nonaccrual add up to the first-lien total (next
  section), so A568 and A569 don't overlap.

### §5.1: buckets plus nonaccrual equal the first-lien total (confirmed and strengthened)

- **The identity, as the form states it:** "(Sum of Memorandum items 2.a.(1) through
  2.a.(6) plus total nonaccrual closed-end loans secured by first liens on 1-4 family
  residential properties in domestic offices included in Schedule RC-N, item 1.c.(2)(a),
  column C, must equal total closed-end loans secured by first liens on 1-4 family
  residential properties in domestic offices (reported in Schedule RC-C, Part I, item
  1.c.(2)(a), column B).)"
- **The nonaccrual item is RCONC229.** The SDF, the bank's report as delimited text, shows
  it on RC-N line 1c2a, "Secured by first liens".
- **Every bank reconciles.** The implied gap (RCON5367 minus the six buckets) equals
  RCONC229 to within $5k:
  - 2026Q2: all 4,297 banks, 2,988 of them exactly. Industry $17.95bn implied vs $17.95bn
    reported.
  - 2018Q1: all 5,657 banks, 4,231 of them exactly. $21.02bn implied vs $21.02bn reported.
- **Every negative gap is rounding.** In 2026Q2 they are between −$1k and −$4k: 185, 13, 6
  and 2 banks at −1, −2, −3 and −4 thousand. Each figure is filed in whole thousands.
- **Changes:**
  - `reported_nonaccrual` (RCONC229) joins `fact_cdr_repricing`.
  - `nonaccrual_mismatch` flags \|implied − reported\| > $5k.
  - Negative gaps are split into `implied_nonaccrual_negative_rounding` (−$5k to $0) and
    `implied_nonaccrual_negative` (beyond −$5k).
  - The plan's 25% flag stays. In 2026Q2 it catches 6 banks, all with small first-lien books
    ($4k to $28mm). In five, the high share matches the bank's reported nonaccrual. The
    sixth, Cleo State Bank, is a $4k book with all buckets at zero.

### §11: one-bank spot check (passed)

JPMorgan Chase Bank, N.A. (RSSD 852218, FDIC cert 628, FFIEC 031), report date 2026-06-30.
The filed report came from CDR as PDF (74 pages, 661,148 bytes) and SDF (224,854 bytes):

| Column | MDRM | Line | Filed ($mm) | Warehouse ($mm) |
|---|---|---|---|---|
| total_assets | RCFD2170 | RC-R II 11 | 4,091,315.0 | 4,091,315.0 |
| first_lien_total | RCON5367 | RC-C I 1.c.(2)(a) | 306,034.0 | 306,034.0 |
| b_le_3m | RCONA564 | RC-C I M.2.a.(1) | 3,624.0 | 3,624.0 |
| b_3_12m | RCONA565 | RC-C I M.2.a.(2) | 15,666.0 | 15,666.0 |
| b_1_3y | RCONA566 | RC-C I M.2.a.(3) | 25,733.0 | 25,733.0 |
| b_3_5y | RCONA567 | RC-C I M.2.a.(4) | 36,782.0 | 36,782.0 |
| b_5_15y | RCONA568 | RC-C I M.2.a.(5) | 64,112.0 | 64,112.0 |
| b_gt_15y | RCONA569 | RC-C I M.2.a.(6) | 156,565.0 | 156,565.0 |
| reported_nonaccrual | RCONC229 | RC-N 1.c.(2)(a) col C | 3,552.0 | 3,552.0 |

- **All nine match exactly.** Page 25 of the PDF shows the six Memo 2.a lines with the same
  values.
- **Why this bank:** JPMorgan also reports RCFD5367 of $309,102mm. The check confirms that
  the model uses RCON5367, the basis the buckets reconcile to.
- **To reproduce:** `armtool spotcheck 852218`. It fetches the facsimile, cached after the
  first time, and compares the SDF with the warehouse.

### CDR facsimile endpoint

- `ViewFacsimileDirect.aspx?ds=call&idType=fdiccert&id=<cert>&date=<MMDDYYYY>` returns a
  4.7 KB HTML page, not the file.
- The page's "Download PDF" and "Download SDF" buttons post its ASP.NET form state back to
  `ViewPDFFacsimile.aspx`.
- One GET plus one POST per format, 5 s apart.

### Notes

- **A test downloaded six quarters from CDR.** While `fetch cdr` was being wired up, an old
  test that expected the command to be unimplemented ran the real fetcher (2018Q1–latest)
  in a pytest temp directory. It downloaded 2018Q1–2019Q2 (about 26 requests) before it
  was stopped.
  - The requests went through the throttled session, at least 5 s apart and with the
    descriptive User-Agent.
  - The six zips passed their integrity checks. They were moved into `data/raw/cdr/` with
    their original manifest entries, sha256 re-checked, rather than downloaded again.
  - The test suite now blocks network access: any socket connect raises.
  - Tests also run from a temp directory, with no config unless a test sets one up.
- **The 2026Q2 facsimiles were first fetched by a one-off script** that used the same
  throttled client. They were then recorded in the manifest.

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
  records its hash.

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
