# ARM Reset Exposure Tool: Build Plan

Drop this file into an empty repo as `PLAN.md` and give the coding agent this prompt:

> Read PLAN.md in full before writing code. Build in the phase order in §12. At each checkpoint, stop and show me the listed outputs. Resolve every item marked VERIFY against real files and record what you found in `docs/verification.md`.

---

## 0. Rules for the coding agent

- Build in the phase order in §12 and stop at each **Checkpoint**.
- Items marked **VERIFY** are assumptions about external files or APIs that have not been checked against a live download. Check each one on the first real file, record the result in `docs/verification.md`, and adjust the code. Do not silently work around a mismatch.
- Tests assert logic and reconciliation, never market values. Do not hardcode expected totals or ARM shares.
- Treat the government servers with care: one request at a time, at least 5 seconds between requests, and a descriptive User-Agent. Cache everything and never re-download a file already recorded in the manifest.
- The tool **never adds numbers across sources**. Call Report buckets, the HMDA reset calendar and Bloomberg agency pools overlap:
  - A bank-retained ARM appears in both HMDA and the Call Report.
  - A GSE-sold ARM appears in both HMDA and agency pools.
  - Present sources side by side and explain the overlap.

---

## 1. Goal and scope

**Question the tool answers:** how much US residential mortgage balance faces an interest-rate reset in a given period, who holds it, and how that is changing.

| Source | What it measures | Grain | Main limitation |
|---|---|---|---|
| Call Reports (FFIEC 031/041/051) | Bank-held closed-end first-lien 1–4 family loans by remaining maturity (fixed rate) or next repricing date (floating rate) | Bank × quarter, 6 buckets | Mixes fixed and floating; buckets are relative to the report date |
| HMDA LAR | Originations with months until the first rate change | Loan × origination year | Origination amounts, not current balances; starts 2018; small filers exempt from the field |

**v1 scope:**
- Both FFIEC sources: fetch, ingest and model.
- Streamlit dashboard, SQL console and methodology page.

**v2 scope (optional):** import agency ARM pool data exported from Bloomberg or BQuant (§5.4).

**Out of scope:**
- Credit union call reports (NCUA 5300).
- Loan-level servicing data (ICE McDash).
- Non-agency RMBS.

**Runtime:** local Python on a laptop or home server, not BQuant, because the FFIEC and HMDA downloads need open internet access. BQuant notebooks can read the output Parquet files.

---

## 2. Architecture

```
        fetch (CLI)                  ingest                     model                      serve
┌───────────────────────┐   ┌──────────────────────┐   ┌───────────────────────┐   ┌──────────────────────┐
│ CDR bulk zips         │──▶│ stg_cdr (parquet)    │──▶│ fact_cdr_repricing    │──▶│ Streamlit app        │
│ HMDA LAR CSV (API)    │──▶│ stg_hmda (parquet)   │──▶│ fact_reset_calendar   │──▶│ SQL console (DuckDB) │
│ HMDA panel            │──▶│ dim_hmda_lender      │   │ dim_bank, views v_*   │   │ CSV / Parquet export │
│ (v2) BBG pool export  │──▶│ stg_bbg_pools        │   │                       │   │ BQuant notebook read │
└───────────────────────┘   └──────────────────────┘   └───────────────────────┘   └──────────────────────┘
        data/raw/                 data/staging/              data/warehouse.duckdb
```

---

## 3. Stack

- Python 3.11+, environment managed with `uv`
- `duckdb` for storage and SQL, `polars` + `pyarrow` for parsing
- `httpx` for streaming downloads, `tenacity` for retries
- `typer` (CLI), `pydantic-settings` + `pyyaml` (config)
- `streamlit` + `plotly` (UI)
- `pytest`, `ruff`
- Optional: `ffiec-data-collector`, a CDR bulk-download helper. It is a pre-release that scrapes an ASP.NET page, so pin the exact version.
- Optional: `openai` client for natural-language-to-SQL against any OpenAI-compatible endpoint, such as a local vLLM server.

---

## 4. Repo layout

```
arm-reset/
├── PLAN.md
├── pyproject.toml
├── config.yaml
├── config/
│   ├── mdrm.yaml              # Call Report items → MDRM codes
│   ├── segments.yaml          # purchaser_type → holder segment, lender type mapping
│   ├── saved_queries.sql      # queries listed in the SQL console
│   └── bbg_fields.yaml        # v2: Bloomberg field mnemonics (left blank until confirmed)
├── src/armreset/
│   ├── cli.py
│   ├── settings.py
│   ├── manifest.py            # download log: url, path, sha256, bytes, fetched_at
│   ├── fetch/   {cdr.py, hmda.py, panel.py}
│   ├── ingest/  {cdr.py, hmda.py, panel.py, bbg.py}
│   ├── model/   {repricing.py, reset_calendar.py, amortization.py}
│   ├── db.py                  # DuckDB connection + view definitions
│   └── app/
│       ├── Home.py
│       └── pages/ {1_Reset_calendar.py, 2_Bank_repricing.py, 3_Bank_drilldown.py,
│                   4_HMDA_explorer.py, 5_SQL_console.py, 6_Methodology.py}
├── docs/ {methodology.md, verification.md, data_dictionary.md}
├── notebooks/bquant_reader.ipynb   # reads outputs; v2 export stub
├── data/ {raw/, staging/}           # gitignored
└── tests/
```

---

## 5. Data sources and acquisition

### 5.1 Call Reports (FFIEC CDR bulk data)

**Source:**
- On https://cdr.ffiec.gov/public, go to Bulk Data and choose "Call Reports -- Single Period" in tab-delimited format.
- Bulk files post about 45 days after quarter-end. As of this plan, the latest quarter is 2026-06-30.

**Quarters:** 2018Q1 through the latest quarter by default; configurable.

**Download:**
- Try `ffiec-data-collector` first: `FFIECDownloader().download_cdr_single_period("YYYYMMDD", FileFormat.TSV)`.
- If it fails, print a clear message telling the user to download the zip manually into `data/raw/cdr/` and re-run. The fetcher must accept manually placed zips.

**Files used from each zip** (identify schedules by parsing the file name with a regex, not by loose globbing):
- **POR:** bank identity (name, city, state, filing form).
- **Schedule RC:** total assets.
- **Schedule RC-C Part I (code `RCCI`):** may be split into several files, e.g. `(1 of 2)`, `(2 of 2)`.

**Parsing, all VERIFY on the first zip:**
- Tab-delimited.
- Header row 1 holds MDRM codes and row 2 holds item descriptions (skip row 2).
- The key column is `IDRSSD`.
- Multi-part schedule files split columns, not rows, so join the parts on `IDRSSD`.
- A blank cell means not reported.
- Amounts are in thousands of dollars.
- The encoding may not be UTF-8.

```python
import re
import polars as pl
from pathlib import Path

SCHED_RE = re.compile(
    r"Schedule (?P<code>[A-Z0-9]+)(?: \((?P<part>\d+) of (?P<n>\d+)\))? (?P<date>\d{8})"
)

def read_cdr_file(path: Path) -> pl.DataFrame:
    df = pl.read_csv(
        path, separator="\t", skip_rows_after_header=1,   # row 2 = descriptions
        infer_schema_length=0, encoding="utf8-lossy", truncate_ragged_lines=True,
    )
    return df.rename({c: c.strip().strip('"') for c in df.columns})

def read_schedule(files: list[Path]) -> pl.DataFrame:
    parts = [read_cdr_file(p) for p in sorted(files)]
    out = parts[0]
    for p in parts[1:]:
        out = out.join(p, on="IDRSSD", how="full", coalesce=True)
    return out
```

**Items (`config/mdrm.yaml`):**

```yaml
# Schedule RC-C Part I, Memorandum item 2.a:
# closed-end loans secured by first liens on 1-4 family residential properties,
# fixed-rate by remaining maturity, floating-rate by next repricing date
repricing_buckets:
  b_le_3m:  A564   # three months or less
  b_3_12m:  A565   # over three months through 12 months
  b_1_3y:   A566   # over one year through three years
  b_3_5y:   A567   # over three years through five years
  b_5_15y:  A568   # over five years through 15 years
  b_gt_15y: A569   # over 15 years
first_lien_total: "5367"  # RC-C Part I item 1.c.(2)(a)
total_assets:     "2170"  # Schedule RC item 12
prefixes: [RCFD, RCON]    # prefer consolidated (FFIEC 031), then domestic (041/051)
```

VERIFY:
- The exact column names (RCFD vs RCON) for every code above.
- That the A567–A569 descriptions match the buckets listed.
- Whether FFIEC 051 filers report Memo item 2.a. If they don't, compute and display the share of industry first-lien balances covered.

```python
def resolve(df: pl.DataFrame, code: str, prefixes=("RCFD", "RCON")) -> tuple[pl.Expr, str | None]:
    """Coalesce RCFD→RCON for one MDRM item, convert thousands → dollars.
    Returns the expression and which prefixes were present (for the prefix_source audit column)."""
    cols = [f"{p}{code}" for p in prefixes if f"{p}{code}" in df.columns]
    if not cols:
        return pl.lit(None, dtype=pl.Float64), None
    expr = pl.coalesce([
        pl.col(c).str.strip_chars().replace("", None).cast(pl.Float64, strict=False)
        for c in cols
    ]) * 1_000
    return expr, ",".join(cols)
```

**QA per bank-quarter:**
- `sum_buckets = Σ b_*`
- `implied_nonaccrual = first_lien_total − sum_buckets`. The form requires the six buckets plus nonaccrual first-lien loans to equal the first-lien total, so this gap should be the nonaccrual balance.
- Write flags to `qa_cdr_flags` when `implied_nonaccrual < 0` or when it exceeds 25% of `first_lien_total`. Keep flagged rows.

### 5.2 HMDA loan-level data (Data Browser API)

**Years:** 2018–2025. The `intro_rate_period` field starts in 2018, and 2025 is the latest published year.

**Primary endpoint** (nationwide CSV, streamed; needs a year plus at least one data filter):

```
GET https://ffiec.cfpb.gov/v2/data-browser-api/view/nationwide/csv
    ?years=YYYY
    &actions_taken=1
    &lien_statuses=1
    &dwelling_categories=Single Family (1-4 Units):Site-Built,Single Family (1-4 Units):Manufactured
```

**Fallback:** loop `GET .../view/csv?states=XX&years=YYYY&...` over every state plus DC and PR. The non-nationwide endpoint requires a geographic filter.

**Handling:**
- Files run to several GB per year nationally, so stream to disk.
- Use `follow_redirects=True`, since the API redirects to object storage, and a long read timeout.
- Write to a `.part` file and rename it when the download completes.
- Check that the first line contains `activity_year`, to catch an HTML page returned instead of data.
- Convert to Parquet immediately. Delete the raw CSV unless `keep_raw_csv: true`.

```python
import time
import httpx
from pathlib import Path
from tenacity import retry, stop_after_attempt, wait_exponential

API = "https://ffiec.cfpb.gov/v2/data-browser-api/view"
FILTERS = {
    "actions_taken": "1",
    "lien_statuses": "1",
    "dwelling_categories": "Single Family (1-4 Units):Site-Built,"
                           "Single Family (1-4 Units):Manufactured",
}
HEADERS = {"User-Agent": "arm-reset-research/0.1 (contact: SET_IN_CONFIG)"}

@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=10, max=120))
def fetch_hmda(year: int, out: Path, state: str | None = None) -> Path:
    url = f"{API}/csv" if state else f"{API}/nationwide/csv"
    params = {"years": year, **FILTERS, **({"states": state} if state else {})}
    tmp = out.with_suffix(".part")
    with httpx.stream("GET", url, params=params, headers=HEADERS, follow_redirects=True,
                      timeout=httpx.Timeout(60, read=900)) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    with tmp.open("rb") as f:
        if b"activity_year" not in f.readline():
            raise ValueError(f"Not a HMDA CSV for {year} {state or 'nationwide'}")
    tmp.rename(out)
    time.sleep(5)
    return out
```

**Staging SQL.** Read all columns as VARCHAR, because the fields mix numbers with `NA` and `Exempt`. Exclude purchased loans (action 6) to avoid double counting.

```sql
CREATE OR REPLACE TABLE stg_hmda AS
SELECT
  CAST(activity_year AS INTEGER)            AS activity_year,
  lei,
  state_code,
  "derived_msa-md"                          AS msa_md,
  conforming_loan_limit,                    -- C / NC / U / NA
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
  END                                       AS rate_type
FROM read_csv('data/raw/hmda/lar_*.csv', all_varchar = true, header = true)
WHERE action_taken = '1'
  AND lien_status = '1'
  AND "open-end_line_of_credit" <> '1'
  AND reverse_mortgage <> '1';
```

VERIFY:
- Fixed-rate loans report `NA` in `intro_rate_period`.
- Whether exempt rows show `Exempt` or `1111`.
- The `intro_m` distribution: log a histogram. Expect clusters near 12, 36, 60, 84 and 120.
- That public `loan_amount` is the midpoint of a $10k band.

Write `stg_hmda` to Parquet partitioned by `activity_year`.

### 5.3 HMDA panel (lender identity and RSSD link)

**Source:** the Panel file for each year on the Snapshot National Loan-Level Dataset page of ffiec.cfpb.gov.

**Fields used:**
- `activity_year`, `lei`, `respondent_name`
- `respondent_rssd`, `parent_rssd`, `top_holder_rssd`
- `agency_code`, `other_lender_code`, `assets`

**Download (VERIFY):**
- The Snapshot page is a JavaScript app, and third parties report that its static file paths don't serve data programmatically.
- If an automated download fails, tell the user to download the panel files manually into `data/raw/hmda_panel/`.
- Names-only fallback: `GET /v2/data-browser-api/view/filers?years=YYYY` returns LEI and name.

**Lender type (VERIFY):** keep the raw codes and map them in `config/segments.yaml`.
- `agency_code = 5` means a credit union.
- `other_lender_code`: 0 = depository institution; 1, 2, 3, 5 = various affiliates or mortgage banking subsidiaries; -1 = null.
- Confirm the exact labels against the panel data dictionary before finalizing the mapping.

**RSSD link:** join `respondent_rssd` to the Call Report `IDRSSD`. Report the unmatched LEIs; don't force matches.

### 5.4 (v2, optional) Bloomberg agency ARM pools

The tool ingests a file the user exports from BQuant or Excel into `data/raw/bbg/agency_arm_pools_YYYYMMDD.{parquet,csv}`, with this contract:

| column | type | notes |
|---|---|---|
| as_of | date | |
| pool_id / cusip | str | |
| agency | str | FNMA / FHLMC / GNMA |
| product | str | e.g. 5/1, 7/6 |
| current_face | float | USD |
| wac | float | percent |
| months_to_roll | float | to next reset; weighted average if pool-level |
| first_reset_pending | bool | true if the pool has not passed its initial roll |

Bloomberg field mnemonics must be confirmed on the terminal with FLDS. Keep `config/bbg_fields.yaml` blank and have the notebook stub raise a clear error until it is filled in. **Do not guess mnemonics.**

---

## 6. Data model (DuckDB)

**Tables**
- `dim_bank(rssd_id, report_date, name, city, state, form)`: from POR
- `fact_cdr_repricing(rssd_id, report_date, total_assets, first_lien_total, b_le_3m, b_3_12m, b_1_3y, b_3_5y, b_5_15y, b_gt_15y, sum_buckets, implied_nonaccrual, prefix_source)`: USD
- `qa_cdr_flags(rssd_id, report_date, flag, detail)`
- `stg_hmda`: Parquet, partitioned by `activity_year`
- `dim_hmda_lender(lei, activity_year, name, respondent_rssd, parent_rssd, top_holder_rssd, agency_code, other_lender_code, lender_type)`
- `fact_reset_calendar(scenario, reset_year, orig_year, intro_m, holder_segment, lender_type, conforming, occupancy_type, state_code, lei, w_loans, orig_amount, bal_at_reset)`: `w_loans` is the weighted loan count
- `stg_bbg_pools` (v2)

**Views** (documented in `docs/data_dictionary.md` and listed in the SQL console sidebar)
- `v_cdr_industry`: quarterly sums of each bucket, `first_lien_total`, `n_banks`, and `n_banks_reporting_buckets`
- `v_cdr_bank_latest`: latest quarter per bank, with:
  - `name`
  - `within_12m = b_le_3m + b_3_12m`
  - `within_3y`
  - `within_12m_pct_first_lien`
  - `within_12m_pct_assets`
- `v_hmda_orig_summary`: year × `rate_type` × `holder_segment` × `conforming`, with count and amount
- `v_reset_calendar`: `fact_reset_calendar` with readable segment labels
- `v_bank_hmda_link`: LEI ↔ RSSD bridge with match status

---

## 7. Analytical logic

This section is also the source text for `docs/methodology.md`. Every assumption here must appear on the Methodology page.

### 7.1 Call Report repricing wall

**What the buckets contain:**
- Bank-held closed-end first-lien 1–4 family loans.
- Fixed-rate loans are placed by remaining maturity; adjustable-rate loans by next repricing date.

**Consequences:**
- The short buckets hold three things together:
  - ARMs about to reprice, whether at their first roll or at a routine annual reset.
  - Seasoned fixed-rate loans near maturity (10- and 15-year loans, for example).
  - Balloon loans.
- Label these amounts "repricing or maturing," never "ARM resets."
- Buckets are measured from the report date. On the 2026-06-30 report, "over 3 through 12 months" means roughly Oct 2026–Jun 2027.
- Never relabel buckets as calendar years. Display them as "within 12 months of the report date."

**Metrics:**
- `within_12m` and `within_3y` in dollars.
- Each as a share of the bank's first-lien book and of total assets.
- Industry time series across quarters and a top-bank ranking.

**Coverage:** banks and savings associations only; no credit unions.

### 7.2 HMDA first-reset calendar

**Step 1: select ARMs.** Keep `rate_type = 'arm'`. Report `unknown` (exempt) rows separately; never drop them silently.

**Step 2: timing.**
- Public HMDA gives the origination year but not the date, so assume origination dates are spread evenly across the year.
- The first reset comes `intro_m` months after origination.
- That spreads each loan's reset over at most two calendar years.

```python
import math

def reset_year_weights(orig_year: int, intro_m: int) -> dict[int, float]:
    """Origination date ~ Uniform(orig_year). First reset = origination + intro_m months.
    Returns {calendar_year: weight}; weights sum to 1."""
    start = orig_year + intro_m / 12.0   # reset time if originated Jan 1
    end = start + 1.0                    # reset time if originated Dec 31
    out, y = {}, math.floor(start)
    while y < end:
        overlap = min(end, y + 1) - max(start, y)
        if overlap > 1e-9:
            out[y] = overlap
        y += 1
    return out

# reset_year_weights(2021, 60) -> {2026: 1.0}
# reset_year_weights(2021, 6)  -> {2021: 0.5, 2022: 0.5}
```

Same logic in SQL for the full table:

```sql
WITH arm AS (
  SELECT *, activity_year + intro_m / 12.0 AS t0
  FROM stg_hmda WHERE rate_type = 'arm'
),
split AS (
  SELECT *, CAST(floor(t0) AS INTEGER)     AS reset_year, (floor(t0) + 1 - t0)     AS w FROM arm
  UNION ALL
  SELECT *, CAST(floor(t0) AS INTEGER) + 1 AS reset_year, 1 - (floor(t0) + 1 - t0) AS w FROM arm
)
SELECT * FROM split WHERE w > 1e-9;
```

**Step 3: balance reaching the reset.**

`bal_at_reset = loan_amount × A(intro_m) × S(intro_m)`

- **A(k), scheduled amortization factor** after k months at note rate r over term n:
  - Standard case: `A = ((1+i)^n − (1+i)^k) / ((1+i)^n − 1)` with `i = r/1200`.
  - Zero rate: `A = 1 − k/n`.
  - Interest-only loans: `A = 1`. HMDA does not report the IO period length, so assume IO lasts at least until the first reset and label this.
- **S(k), survival** from prepayment and default: `(1 − CPR)^(k/12)`, where CPR comes from the scenario set in config. These are **assumptions, not estimates**; label them that way everywhere.
- **Missing `interest_rate`:** fill with the median for the same (`activity_year`, `intro_m` bucket, `conforming_loan_limit`). Count the filled rows.
- Because the time from origination to first reset is exactly `intro_m` for every loan, k does not depend on the origination date.

```python
def sched_factor(rate_pct: float, term_m: int, k_m: int, io: bool = False) -> float:
    if io or k_m <= 0:
        return 1.0
    if k_m >= term_m:
        return 0.0
    i = rate_pct / 1200
    if i == 0:
        return 1 - k_m / term_m
    g = 1 + i
    return (g**term_m - g**k_m) / (g**term_m - 1)
```

**Step 4: holder segment** from `purchaser_type`, set in `config/segments.yaml`:

| purchaser_type | segment | where else it shows up |
|---|---|---|
| 0 | Retained (not sold in origination year) | Overlaps Call Report buckets |
| 1, 3 | Sold to Fannie / Freddie | Overlaps agency pools (v2) |
| 2 | Ginnie Mae | Overlaps agency pools (v2) |
| 5 | Private securitization | Non-agency RMBS (out of scope) |
| 6, 71, 72, 8, 9 | Sold to another institution / affiliate / other | Unknown holder |

A loan retained in its origination year may have been sold later. The segment is a proxy.

**Step 5: coverage matrix,** shown under every calendar chart.
- Reset year Y is complete for intro period m years only if the origination cohort `Y − m` falls between 2018 and the latest HMDA year (2025).
- Compute this per (reset year × intro period) and shade the missing cells.
- Examples:
  - 10-year ARMs resetting in 2026 or 2027 come from 2016–17 originations, which HMDA does not cover.
  - 3-year ARMs resetting in 2029 or later come from originations not yet published.
- Also show by year:
  - The share of loans and dollars with `rate_type = 'unknown'` (exempt filers).
  - A note that lenders below HMDA reporting thresholds are absent.
- The current calendar year's bar includes resets that already happened earlier in the year; flag it.

**Step 6: subsequent resets** (toggle, off by default).
- After the first reset, ARMs reset every 6 or 12 months. HMDA doesn't record the frequency, so use `frequency_months` from config (default 12).
- Roll surviving balances forward.
- Label the output clearly as modeled.

### 7.3 Cross-checks (diagnostics, never sums)

- **HMDA vs Call Report per bank:**
  - For banks linked by RSSD, compare HMDA retained-ARM balances expected to reset within the Call Report windows against that bank's `within_12m` and `within_3y`.
  - HMDA should generally come in *below* the Call Report figures. The Call Report also holds fixed loans near maturity, purchased loans and pre-2018 ARMs.
  - Show this as a scatter with outliers listed. It is not a reconciliation.
- **Where to look next:** the retained vs sold split tells the user where the rest of the exposure sits. GSE and Ginnie loans are in agency pools (v2); retained loans are in the Call Reports.

---

## 8. App (Streamlit, multi-page)

**Design direction:**
- Dense analyst dashboard with sentence-case labels.
- Neutral palette with one accent color reserved for "resets inside the selected window."
- No decorative chrome.
- Units: $bn (one decimal) on charts, $mm in bank tables.
- **Every chart gets a caption with source, as-of date, and a one-line caveat.**

**1. Reset calendar (home)**
- Stacked bars, $bn by first-reset year (current year through 2032), stacked by holder segment.
- Controls:
  - CPR scenario
  - Original amount vs balance at reset
  - Conforming vs jumbo, occupancy, lender type, state
- Coverage matrix below the chart.
- CSV download.

**2. Bank repricing**
- Industry stacked area of the six buckets by quarter ($bn), plus a line for the within-12-months share of the first-lien book.
- Latest-quarter table of top banks, sortable.
- Filters: asset-size band, state, minimum first-lien book.

**3. Bank drill-down**
- Search by name or RSSD.
- Bucket history and implied nonaccrual.
- QA flags.
- Linked HMDA LEIs with ARM originations by year and intro-period mix.
- The HMDA vs Call Report diagnostic.

**4. HMDA explorer**
- ARM share of originations by year (count and dollars).
- Intro-period mix and jumbo vs conforming split.
- Top ARM originators by year.
- Exempt share.

**5. SQL console**
- Read-only connection: `duckdb.connect(path, read_only=True)`.
- Sidebar lists views and columns plus the saved queries from `config/saved_queries.sql`.
- Results grid, CSV download, default row limit 10,000.
- **Optional natural-language-to-SQL:**
  - A text box sends the question and view schemas to an OpenAI-compatible endpoint set in config.
  - The generated SQL goes into the editor and runs only when the user clicks Run.
  - Hidden unless `llm.base_url` is set. Never auto-execute.

**6. Methodology**
- Renders `docs/methodology.md`.

**Performance:**
- `armtool build` precomputes all fact tables, and the app reads only views and aggregates.
- Cache with `st.cache_data` keyed on the warehouse file's modified time.

**Seed `config/saved_queries.sql` with at least:**

```sql
-- name: Top 20 banks by first-lien balances repricing or maturing within 12 months (latest quarter)
SELECT name, state,
       within_12m / 1e6        AS within_12m_mm,
       within_12m_pct_first_lien,
       within_12m_pct_assets
FROM v_cdr_bank_latest
ORDER BY within_12m DESC
LIMIT 20;

-- name: Industry repricing wall over time
SELECT report_date,
       (b_le_3m + b_3_12m) / 1e9 AS within_12m_bn,
       (b_1_3y + b_3_5y)   / 1e9 AS y1_5_bn,
       first_lien_total    / 1e9 AS first_lien_bn
FROM v_cdr_industry
ORDER BY report_date;

-- name: First resets by year, retained jumbo ARMs, base scenario
SELECT reset_year,
       SUM(bal_at_reset) / 1e9 AS bal_bn,
       SUM(w_loans)            AS weighted_loans
FROM v_reset_calendar
WHERE scenario = 'base' AND holder_segment = 'retained' AND conforming = 'NC'
GROUP BY 1 ORDER BY 1;
```

---

## 9. CLI

```
armtool fetch cdr    --start 2018Q1 --end latest
armtool fetch hmda   --years 2018-2025 [--by-state]
armtool fetch panel  --years 2018-2025
armtool build        # ingest → staging → models → views
armtool validate     # writes data/qa_report.md
armtool status       # what's downloaded, as-of dates, manifest summary
armtool app          # streamlit run
```

- Every command is idempotent.
- `manifest.json` records url, path, sha256, bytes, fetched_at, and the source's published date where available.

---

## 10. `config.yaml`

```yaml
contact_email: SET_ME            # used in the User-Agent
paths:
  raw: data/raw
  staging: data/staging
  warehouse: data/warehouse.duckdb
cdr:
  start: 2018Q1
  end: latest
  request_delay_s: 5
hmda:
  years: [2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025]
  by_state: false
  keep_raw_csv: false
model:
  as_of: 2026-06-30
  calendar_years: [2026, 2032]
  scenarios:                     # annual CPR incl. defaults. Placeholder assumptions: set your own.
    low: 0.06
    base: 0.10
    high: 0.15
  subsequent_resets:
    enabled: false
    frequency_months: 12
  io_assumption: io_through_first_reset
llm:
  base_url: null                 # e.g. a local OpenAI-compatible vLLM endpoint
  model: null
  api_key_env: LLM_API_KEY
```

---

## 11. Tests and acceptance

**Unit tests**

*Reset weights:*
- `(2021, 60) → {2026: 1.0}`
- `(2021, 6) → {2021: .5, 2022: .5}`
- `(2021, 84) → {2028: 1.0}`
- Weights sum to 1 for random inputs (property test).
- The SQL split matches the Python function on a sample.

*`sched_factor`:*
- k=0 → 1; k=n → 0.
- Zero rate is linear.
- IO → 1.

*`rate_type`:*
- `NA` → fixed
- `Exempt` → unknown
- `60` with term 360 → arm
- `360` with term 360 → fixed
- `0` → fixed

*`resolve`:*
- RCFD is preferred over RCON.
- Blank → null.
- ×1000 scaling.

*CDR reader:*
- Fixture TSV with the two-row header.
- Fixture of a two-part schedule joined on IDRSSD.

**QA report** (logged for review, not asserted)

*Per quarter:*
- Bank count and banks reporting buckets.
- Industry bucket totals.
- Flag counts.

*Per HMDA year:*
- Row count.
- ARM share (count and dollars).
- Exempt share and `intro_m` histogram.
- Filled-rate count and RSSD match rate.

**Acceptance**
- Fresh clone → `uv sync` → fetch 2 quarters and 1 HMDA year → `armtool build` → `armtool app` runs end to end.
- Every chart shows source, as-of date and a caveat.
- The Methodology page covers every assumption in §7 and every VERIFY resolution.
- Spot check: one large bank's six buckets match its Call Report PDF from CDR Institution Reports. Record the check in `docs/verification.md`.

---

## 12. Build phases and checkpoints

| Phase | Work | Checkpoint: show me |
|---|---|---|
| 0 | Scaffold, config, settings, manifest, test harness | `armtool --help`; tests pass |
| 1 | CDR fetch and ingest for the latest quarter only | Resolved columns with prefix source; industry bucket totals ($bn); bank count; QA flag counts; one-bank spot check |
| 2 | CDR history 2018Q1 → latest; `dim_bank`; CDR views | `v_cdr_industry` table; banks-reporting-buckets by quarter |
| 3 | HMDA one year (2021) and panel for 2021 | Download size and time; row count; `rate_type` split; `intro_m` histogram; exempt share; 10 sample ARM rows; RSSD match rate |
| 4 | HMDA all years; reset calendar model; coverage matrix | Calendar table by scenario; coverage matrix; filled-rate counts |
| 5 | Streamlit pages 1–4 and 6 | Screenshots of each page |
| 6 | SQL console; optional NL-to-SQL | Saved queries running; generated SQL shown before execution |
| 7 (opt.) | BBG import contract, `bquant_reader.ipynb` | Stub fails clearly until `bbg_fields.yaml` is filled |

---

## 13. VERIFY checklist

**CDR files**
1. Zip file names, the schedule-code regex, header and description rows, how multi-part files split, and the encoding.
2. RCFD vs RCON columns for 5367, 2170 and A564–A569 by form type.
3. Whether FFIEC 051 filers report RC-C Memo item 2.a.
4. Whether `ffiec-data-collector` still works against the current CDR page (pin the version).

**HMDA files**

5. Nationwide CSV: size, run time, redirect behavior, and whether the multi-value `dwelling_categories` filter is accepted.
6. `intro_rate_period` values for fixed-rate loans (`NA`) and exempt filers (`Exempt` vs `1111`).
7. The panel download path and the `agency_code` / `other_lender_code` → `lender_type` mapping.
8. That public `loan_amount` is the $10k-band midpoint.

---

## 14. Known limitations (show on the Methodology page)

**Call Reports**
- Buckets mix fixed-rate maturities with floating-rate repricing, and are measured from the report date.
- Credit unions are absent from the Call Report layer. They do appear in HMDA, as agency code 5.

**HMDA**
- Covers originations only; balances are modeled.
- CPR is an assumption.
- No origination month.
- IO length is unknown.
- Pre-2018 cohorts are missing.
- Small filers are exempt from the intro-rate field.
- "Retained in origination year" ≠ held today.

**Across sources**
- Sources overlap by design, so the tool shows them side by side and never sums them.
