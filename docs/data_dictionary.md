# Data dictionary

Everything lives in `data/warehouse.duckdb`, built by `armtool build`. Amounts are in US
dollars unless a column says otherwise. The views listed in PLAN.md §6 arrive in phase 2.

## Tables

### `dim_bank`

Bank identity per quarter, from the Call Report POR file. One row per `(rssd_id, report_date)`.

| Column | Type | Notes |
|---|---|---|
| `rssd_id` | BIGINT | Federal Reserve RSSD ID (`IDRSSD` in the bulk files) |
| `report_date` | DATE | Quarter-end report date |
| `name` | VARCHAR | Financial institution name |
| `city`, `state` | VARCHAR | Head office location |
| `form` | VARCHAR | Filing form: `031`, `041` or `051` |
| `fdic_cert` | VARCHAR | FDIC certificate number (`0` when there is none) |
| `last_submission` | VARCHAR | When the filing was last updated at CDR (ISO timestamp) |

### `fact_cdr_repricing`

One row per `(rssd_id, report_date)`. Bucket amounts are first-lien balances **repricing or
maturing**, measured from the report date (see `docs/methodology.md`).

| Column | Type | Notes |
|---|---|---|
| `rssd_id`, `report_date` | BIGINT, DATE | Key; joins `dim_bank` |
| `total_assets` | DOUBLE | 2170; RCFD for 031 filers, RCON otherwise |
| `first_lien_total` | DOUBLE | RCON5367: closed-end first-lien 1–4 family loans, domestic offices |
| `b_le_3m` | DOUBLE | RCONA564: three months or less |
| `b_3_12m` | DOUBLE | RCONA565: over three months through 12 months |
| `b_1_3y` | DOUBLE | RCONA566: over one year through three years |
| `b_3_5y` | DOUBLE | RCONA567: over three years through five years |
| `b_5_15y` | DOUBLE | RCONA568: over five years through 15 years |
| `b_gt_15y` | DOUBLE | RCONA569: over 15 years |
| `sum_buckets` | DOUBLE | Sum of the reported buckets; null when none are reported |
| `implied_nonaccrual` | DOUBLE | `first_lien_total − sum_buckets` |
| `reported_nonaccrual` | DOUBLE | RCONC229: nonaccrual first-lien loans (RC-N item 1.c.(2)(a), column C) |
| `n_buckets_reported` | TINYINT | How many of the six buckets are non-null (0–6) |
| `prefix_source` | VARCHAR | Columns that supplied the values, e.g. `RCFD2170,RCON5367,RCONA564-A569,RCONC229` |

### `qa_cdr_flags`

One row per flag raised on a bank-quarter. The flag rules are in `docs/methodology.md`.

| Column | Type | Notes |
|---|---|---|
| `rssd_id`, `report_date` | BIGINT, DATE | The flagged bank-quarter |
| `flag` | VARCHAR | Flag name |
| `detail` | VARCHAR | The values behind the flag, e.g. `first_lien_total=…; sum_buckets=…` |

## Staging files

### `data/staging/cdr/stg_cdr_<YYYY-MM-DD>.parquet`

One file per quarter, written by the CDR ingest. It holds the POR identity columns above and
the raw text of every prefix variant of every MDRM item in `config/mdrm.yaml`, as filed. Those
values are in thousands of dollars, and the variants include ones the model doesn't use, such
as RCFD5367. A variant absent from the files is an all-null column. Other columns:
- `source_file`: the zip the file was staged from.
- `source_sha256`: that zip's hash, used to tell whether staging is current.
- `cdr_data_as_of`: CDR's processing timestamp, from the zip's `Readme.txt`.
