# Data dictionary

Everything lives in `data/warehouse.duckdb`, built by `armtool build`. Amounts are in US
dollars unless a column says otherwise. Columns ending in `_pct` or containing `_pct_` are
percentage points: 8.7 means 8.7%.

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

### `dim_hmda_lender`

One row per `(activity_year, lei)`, from the Philadelphia Fed HMDA Lender File (2018–2025).

| Column | Type | Notes |
|---|---|---|
| `activity_year`, `lei` | INTEGER, VARCHAR | Key |
| `name` | VARCHAR | Filer name as filed with HMDA, cut at 30 characters |
| `respondent_rssd`, `parent_rssd`, `top_holder_rssd` | BIGINT | NIC RSSD IDs of the lender, its direct parent and its regulatory high holder; null where the file has 0 |
| `agency_code` | BIGINT | 1 OCC, 2 FRB, 3 FDIC, 5 NCUA, 7 HUD, 9 CFPB |
| `institution_type` | BIGINT | The Fed's institution type code; labels in `dim_institution_type` |
| `lender_type` | VARCHAR | `bank`, `bank_affiliate`, `credit_union`, `independent_mortgage_company` or `unknown`. From `institution_type`, except that a Call Report filer is always `bank` |
| `in_call_reports` | BOOLEAN | The RSSD filed a Call Report that year; null when no Call Reports are loaded for the year |

### `dim_institution_type`

The Lender File's institution type codes and the `lender_type` each maps to, from
`config/segments.yaml`. Columns: `institution_type`, `institution_label`, `lender_type`.

### `dim_purchaser_segment`

HMDA `purchaser_type` → holder segment, from `config/segments.yaml`.

| Column | Notes |
|---|---|
| `purchaser_type` | HMDA code |
| `holder_segment` | `retained`, `gse`, `ginnie`, `private_securitization` or `other` |
| `holder_label` | Readable label |
| `overlaps_with` | Where else these loans show up (Call Report buckets, agency pools, ...) |

### `dim_scenario`

The CPR scenarios in `config.yaml` (`model.scenarios`), applied from `model.as_of` to each
reset. **Assumptions, not estimates.**

| Column | Notes |
|---|---|
| `scenario` | Scenario name, e.g. `base` |
| `cpr` | Assumed annual rate of prepayment and default after the as-of date, e.g. 0.10 |

### `dim_history_cpr`

The CPR each origination year is assumed to have shown from origination to `model.as_of`
(`model.history_cpr`), the same in every scenario. **Placeholders until measured.** A year
not listed takes its scenario's CPR.

| Column | Notes |
|---|---|
| `orig_year` | HMDA activity year |
| `cpr` | Assumed annual rate of prepayment and default up to the as-of date |

### `dim_code_label`

Readable labels for codes, from `code_labels` in `config/segments.yaml`. Columns: `field`
(`lender_type`, `conforming_loan_limit` or `occupancy_type`), `code` (as text), `label`.

### `fact_reset_calendar`

HMDA ARMs by the calendar year of their first rate reset (PLAN.md §7.2), for every scenario.
One row per scenario and combination of the grouping columns. Each scenario holds every ARM
once: **filter to one scenario, never add scenarios together.**

| Column | Type | Notes |
|---|---|---|
| `scenario` | VARCHAR | CPR scenario; joins `dim_scenario` |
| `reset_kind` | VARCHAR | `first`; `subsequent` rows exist only when `model.subsequent_resets` is on, and count resets, not loans |
| `reset_year` | INTEGER | Calendar year of the reset |
| `orig_year` | INTEGER | HMDA activity year: the origination year |
| `intro_m` | INTEGER | Months from origination to the first reset |
| `holder_segment` | VARCHAR | Holder at origination, from `purchaser_type` (`dim_purchaser_segment`); `unmapped` for unlisted codes |
| `lender_type` | VARCHAR | From `dim_hmda_lender` for the origination year; `unknown` when the lender isn't in it |
| `conforming` | VARCHAR | `conforming_loan_limit`: `C`, `NC`, `U` or `NA` |
| `occupancy_type` | INTEGER | HMDA code: 1 principal residence, 2 second residence, 3 investment |
| `state_code`, `lei` | VARCHAR | Property state and lender |
| `is_io` | BOOLEAN | Interest-only payments; assumed to last through the first reset |
| `rate_filled`, `term_filled` | BOOLEAN | The interest rate or loan term was missing or out of range, and a median stands in (`qa_reset_inputs`) |
| `w_loans` | DOUBLE | Weighted loan count: each loan's reset is split over at most two years, with shares adding up to 1 |
| `orig_amount` | DOUBLE | Original loan amount × share, USD |
| `bal_at_reset` | DOUBLE | Modeled balance at the reset × share, USD: amount × scheduled amortization × survival, at the origination year's history CPR up to `model.as_of` and the scenario's CPR after it |

### `qa_reset_inputs`

What went into the reset calendar: one row per `activity_year`, `intro_bucket` (months to
first reset, bucketed as in the QA histogram) and `conforming`. These are the filled-rate
counts of PLAN.md §11.

| Column | Notes |
|---|---|
| `arm_loans`, `arm_amount` | ARMs and their original amount, USD |
| `rate_reported` | ARMs whose reported rate is used |
| `rate_out_of_range` | ARMs reporting a rate above 20%, treated as missing |
| `rate_filled` | ARMs whose rate is a median: missing or out of range |
| `rate_fill_source`, `rate_fill_value` | The group the median came from, e.g. `median: activity_year, intro_bucket, conforming`, and the rate used |
| `rate_missing` | ARMs left without a rate (no median anywhere); their balance is unknown |
| `median_reported_rate` | Median of the reported rates in the group |
| `term_filled`, `term_out_of_range`, `term_missing` | The same for loan terms; out of range means above 600 months |
| `io_loans`, `io_amount` | Interest-only ARMs and their original amount |

## Views

The SQL console lists these.

### `stg_hmda`

HMDA originations, read in place from `data/staging/hmda/activity_year=YYYY/*.parquet` (PLAN.md
§6 keeps them in Parquet). One row per loan: originated, first lien, closed-end, not a reverse
mortgage, 1–4 family dwelling.

| Column | Notes |
|---|---|
| `activity_year`, `lei`, `state_code`, `msa_md` | Year of action, lender, location |
| `conforming_loan_limit` | `C` conforming, `NC` jumbo, `U` undetermined |
| `dwelling_category` | Site-built or manufactured 1–4 family |
| `purchaser_type`, `loan_type`, `loan_purpose`, `occupancy_type` | HMDA codes |
| `loan_amount` | USD, midpoint of a $10k band |
| `interest_rate` | Note rate in percent; null for `NA` or `Exempt` |
| `loan_term_m` | Term in months |
| `intro_raw`, `intro_m` | `intro_rate_period` as filed, and as months |
| `is_io` | Interest-only payments reported |
| `rate_type` | `arm`, `fixed` or `unknown` (exempt) |
| `source_key` | Manifest key of the raw file the row came from |

### `v_hmda_orig_summary`

Loan counts and dollars by `activity_year` × `rate_type` × `holder_segment` × `conforming`
(`conforming_loan_limit`). Columns: `loans`, `amount` (USD).

### `v_bank_hmda_link`

One row per `(activity_year, lei)` in the Lender File or the loan file.

| Column | Notes |
|---|---|
| `activity_year`, `lei`, `name`, `lender_type`, `agency_code`, `institution_type`, `respondent_rssd` | From `dim_hmda_lender` |
| `match_status` | `matched`, `rssd_not_a_call_report_filer`, `no_rssd`, `not_in_lender_file` or `no_lender_file_for_year` |
| `call_report_name`, `call_report_date` | The matched filer's name and its latest report date that year |
| `loans`, `amount`, `arm_loans`, `arm_amount` | The lender's originations that year in `stg_hmda` |

### `v_reset_calendar`

`fact_reset_calendar` with readable labels, the scenario's CPR and the current-year flag. Same
rows as the fact table; filter to one scenario.

| Column | Notes |
|---|---|
| `scenario` | CPR scenario |
| `history_cpr_assumption` | The origination year's assumed CPR up to `model.as_of` (the scenario's when the year has none) |
| `forward_cpr_assumption` | The scenario's assumed CPR after `model.as_of` |
| `reset_kind`, `reset_year`, `orig_year`, `intro_m` | As in `fact_reset_calendar` |
| `is_current_year` | `reset_year` is the year of `model.as_of`; that bar includes resets that already happened earlier in the year |
| `holder_segment`, `holder_label` | Holder at origination and its label |
| `lender_type`, `lender_type_label` | Lender type and its label |
| `conforming`, `conforming_label` | e.g. `NC`, "Jumbo (nonconforming)" |
| `occupancy_type`, `occupancy_label` | e.g. 1, "Principal residence" |
| `state_code`, `lei`, `is_io`, `rate_filled`, `term_filled` | As in `fact_reset_calendar` |
| `w_loans`, `orig_amount`, `bal_at_reset` | As in `fact_reset_calendar` |

### `v_reset_coverage`

The coverage matrix (PLAN.md §7.2 step 5): one row per reset year and `intro_m` in the
calendar.

| Column | Notes |
|---|---|
| `reset_year`, `intro_m` | The cell |
| `first_cohort`, `last_cohort` | The origination years whose loans reset in that cell |
| `coverage` | The share of the cell's origination window that the loaded HMDA years hold, weighted as in the calendar (0 to 1) |
| `status` | `complete`, `partial` or `missing` |

### `v_cdr_industry`

One row per report date: every bank's Call Report figures summed. Bucket amounts are
repricing or maturing, measured from that report date.

| Column | Notes |
|---|---|
| `report_date` | Quarter-end report date |
| `n_banks` | Banks filing a Call Report that quarter |
| `n_banks_reporting_buckets` | Banks reporting at least one of the six buckets |
| `n_banks_all_buckets` | Banks reporting all six buckets |
| `total_assets`, `first_lien_total` | Sums over all filers |
| `b_le_3m` … `b_gt_15y` | Sum of each bucket |
| `sum_buckets`, `implied_nonaccrual`, `reported_nonaccrual` | Sums of the fact-table columns |
| `within_12m` | Sum of `b_le_3m + b_3_12m` |
| `within_3y` | Sum of `b_le_3m + b_3_12m + b_1_3y` |
| `within_12m_pct_first_lien` | `within_12m` as a percent of the first-lien total at banks reporting both short buckets |
| `within_3y_pct_first_lien` | The same for `within_3y` |
| `bucket_coverage_pct` | First-lien balances at banks reporting buckets, as a percent of all first-lien balances |

### `v_cdr_bank_latest`

One row per bank that filed in the **latest quarter in the warehouse**. A bank whose last
report is older is left out: it has merged, failed or changed charter, and its loans now sit
with another filer. Keeping it would count the same loans twice in a ranking.

| Column | Notes |
|---|---|
| `rssd_id`, `report_date` | Key |
| `name`, `city`, `state`, `form`, `fdic_cert` | From `dim_bank` |
| `total_assets`, `first_lien_total`, `b_le_3m` … `b_gt_15y` | From `fact_cdr_repricing` |
| `within_12m` | `b_le_3m + b_3_12m`; null if either bucket is unreported |
| `within_3y` | `within_12m + b_1_3y` |
| `within_12m_pct_first_lien`, `within_3y_pct_first_lien` | Percent of the bank's first-lien book |
| `within_12m_pct_assets`, `within_3y_pct_assets` | Percent of total assets (for 031 filers: domestic first-lien loans over consolidated assets) |
| `implied_nonaccrual`, `reported_nonaccrual` | From `fact_cdr_repricing` |
| `n_qa_flags`, `qa_flags` | Count and comma-separated names of the bank-quarter's QA flags |
| `prefix_source` | Columns the values came from |

## Staging files

### `data/staging/cdr/stg_cdr_<YYYY-MM-DD>.parquet`

One file per quarter, written by the CDR ingest. It holds the POR identity columns above and
the raw text of every prefix variant of every MDRM item in `config/mdrm.yaml`, as filed. Those
values are in thousands of dollars, and the variants include ones the model doesn't use, such
as RCFD5367. A variant absent from the files is an all-null column. Other columns:
- `source_file`: the zip the file was staged from.
- `source_sha256`: that zip's hash, used to tell whether staging is current.
- `cdr_data_as_of`: CDR's processing timestamp, from the zip's `Readme.txt`.
