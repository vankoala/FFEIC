# Methodology

How much US residential mortgage balance faces an interest-rate reset in a given period, who
holds it, and how that is changing. This page states every assumption the tool makes. The
checks behind them are in `docs/verification.md`.

## Sources are shown side by side, never summed

| Source | What it measures | Grain |
|---|---|---|
| Call Reports (FFIEC 031/041/051) | Bank-held closed-end first-lien 1–4 family loans by remaining maturity (fixed rate) or next repricing date (floating rate) | Bank × quarter, 6 buckets |
| HMDA LAR | Originations with months until the first rate change | Loan × origination year |

The sources overlap by design:
- A bank-retained ARM appears in both HMDA and the Call Report.
- A GSE-sold ARM appears in both HMDA and agency pools.

So the tool never adds numbers across sources.

### No loan is counted twice

Within each source, the tool also makes sure the same loans never appear twice:
- **Call Report rankings use one quarter.** `v_cdr_bank_latest` holds banks that filed in
  the latest quarter. A bank that merged or failed drops out, because another filer now
  reports its loans.
- **HMDA drops purchased loans.** Only originations (action taken 1) are kept. A loan one
  lender originates and another buys appears only once, as the origination.
- **Each HMDA year comes from one source.** It's the nationwide file, or else the full set of
  state files, never both. A partial set of state files is refused.
- **Each lender-year comes from one source:** the Philadelphia Fed HMDA Lender File, with one
  row per lender and year. The CFPB's lender lists are only a cross-check.
- **Nothing is downloaded twice.** A file recorded in the manifest is never fetched again.
- **Each ARM counts once in the reset calendar.** Its first reset is split across at most two
  calendar years, with shares that add up to 1. In each scenario the shares of all ARMs add
  up to their number: 3,225,980 in 2018–2025 (`docs/verification.md`). The tests check it on
  every change.
- **Each scenario holds every loan once, so never add scenarios together.** Each row of the
  calendar names its scenario; pick one.
- **Subsequent resets, off by default, count resets rather than loans.** Turned on, a loan
  appears once per reset, so a total across reset years counts resets.

## Call Report repricing wall

### What the buckets contain

- **The data is Schedule RC-C Part I, Memorandum item 2.a:** closed-end loans secured by
  first liens on 1–4 family residential properties, in domestic offices.
- **Two kinds of loan share the buckets.** Fixed-rate loans are placed by remaining
  maturity. Adjustable-rate loans are placed by next repricing date.
- **So the short buckets mix three things:**
  - ARMs about to reprice, at their first roll or at a routine annual reset.
  - Seasoned fixed-rate loans near maturity, such as 10- and 15-year loans.
  - Balloon loans.
- **The tool calls these amounts "repricing or maturing", never "ARM resets".**
- **Buckets are measured from the report date.** On the 2026-06-30 report, "over 3 through
  12 months" means roughly October 2026 to June 2027. The tool never relabels buckets as
  calendar years. It shows them as, for example, "within 12 months of the report date".

### Items used

| Column | MDRM | Form line | Columns tried, in order |
|---|---|---|---|
| `total_assets` | 2170 | Schedule RC item 12 | RCFD (031, consolidated), then RCON (041/051) |
| `first_lien_total` | 5367 | RC-C Part I item 1.c.(2)(a), column B | RCON only |
| `b_le_3m` | A564 | RC-C Part I Memo 2.a.(1): three months or less | RCON only |
| `b_3_12m` | A565 | Memo 2.a.(2): over three months through 12 months | RCON only |
| `b_1_3y` | A566 | Memo 2.a.(3): over one year through three years | RCON only |
| `b_3_5y` | A567 | Memo 2.a.(4): over three years through five years | RCON only |
| `b_5_15y` | A568 | Memo 2.a.(5): over five years through 15 years | RCON only |
| `b_gt_15y` | A569 | Memo 2.a.(6): over 15 years | RCON only |
| `reported_nonaccrual` | C229 | RC-N item 1.c.(2)(a), column C (nonaccrual) | RCON only |

- **Every form reports the buckets for domestic offices only (RCON).** So the first-lien
  total they're compared with is the domestic figure too.
- **FFIEC 031 filers also report a consolidated first-lien total (RCFD5367)**, which
  includes foreign offices. For Citibank it's $19bn higher than the domestic figure.
- **Mixing the two bases would misstate every ratio.** So those items use RCON only.
- **Total assets comes from RCFD for 031 filers** (consolidated), and from RCON for 041/051
  filers.
- **Every row records its source columns.** The `prefix_source` column lists them, for
  example `RCFD2170,RCON5367,RCONA564-A569,RCONC229`.
- **Units:** Call Report values are filed in thousands of dollars, and the tool stores
  dollars. A blank cell means "not reported" and stays null, never zero. A non-numeric
  value such as `CONF` (confidential) is also null, and it is flagged.

### Metrics

- **`within_12m`** is `b_le_3m + b_3_12m`: first-lien balances repricing or maturing within
  12 months of the report date.
- **`within_3y`** adds `b_1_3y`.
- **Each appears in dollars**, as a share of the bank's first-lien book, and as a share of
  its total assets.
- **Shares of total assets are approximate for 031 filers:** they compare domestic
  first-lien loans with consolidated assets.
- **Unreported buckets make the metric unknown, not zero.** If either short bucket is
  unreported, `within_12m` is null, and the industry share leaves that bank out of the
  numerator and the denominator alike.
- **Industry series** (`v_cdr_industry`) sum every filer in each quarter. The set of banks
  changes as banks merge, fail and open, so a change between quarters mixes changes in
  books with changes in who files.
- **Bank rankings** (`v_cdr_bank_latest`) use the latest quarter in the warehouse and
  include only banks that filed in it.
  - A bank whose last report is older is left out: it has merged, failed or changed charter,
    and another filer now holds its loans.
  - This reads PLAN.md's "latest quarter per bank" as the latest quarter, so the same loans
    aren't ranked twice.

### Reconciliation and QA flags

- **The form defines an identity:** the six buckets plus nonaccrual first-lien loans equal
  the first-lien total.
- **The tool computes `implied_nonaccrual`** as `first_lien_total − sum of buckets` and
  checks it against the bank's reported nonaccrual (RC-N, RCONC229).
- **Every bank checked so far reconciles to within $5k**, which is rounding: each figure is
  filed in whole thousands.

Flags go to `qa_cdr_flags`. Flagged rows stay in the data.

| Flag | When |
|---|---|
| `implied_nonaccrual_negative` | The buckets exceed the first-lien total by more than $5k |
| `implied_nonaccrual_negative_rounding` | The buckets exceed the first-lien total by $5k or less |
| `implied_nonaccrual_gt_25pct` | Implied nonaccrual is over 25% of the first-lien total |
| `nonaccrual_mismatch` | Implied and reported nonaccrual differ by more than $5k |
| `buckets_not_reported` | A first-lien total, but none of the six buckets |
| `buckets_partial` | Some of the six buckets are blank |
| `unparseable_value` | A used item holds something other than a number, e.g. `CONF` |

### Coverage

- **Banks and savings associations only.** These are the filers of FFIEC 031, 041 and 051.
- **Credit unions are absent from the Call Report layer.** They file NCUA 5300 reports,
  which are out of scope. They do appear in HMDA.
- **FFIEC 051 filers do report Memo 2.a.** In every quarter built so far, every bank
  reports all six buckets, covering 100% of first-lien balances. The QA report shows
  coverage for each quarter.

### Data vintage

- **Bulk files include amendments.** CDR rebuilds each quarter's bulk file as amended
  reports arrive.
- **Each quarter records when its data was fetched.** The tool keeps CDR's processing
  timestamp from the zip's `Readme.txt`, CDR's "Call Updated" date, and the download time.
- **Recorded quarters are not refetched.** To pick up later amendments, delete the
  quarter's zip, staging file and manifest entry, then fetch again.

## HMDA originations

### What is included

- **Data:** the CFPB Data Browser's loan-level file for each year. For 2021 the Data
  Browser serves the three-year dataset (frozen 2024-12-31, including resubmissions).
- **Kept:**
  - originated loans (action taken 1);
  - first liens;
  - closed-end loans (open-end lines of credit dropped);
  - not reverse mortgages;
  - 1–4 family dwellings, site-built or manufactured.
- **Where the filters run:** the API accepts at most two filters, so it filters on action
  taken and lien status, and staging applies the rest.
- **Loan amounts are the midpoint of a $10k band**, e.g. $305,000 for any amount from
  $300,000 up to $310,000.

### Fixed, ARM or unknown

`rate_type` comes from `intro_rate_period`, the months until the first rate change:
- **`arm`:** a positive number of months, shorter than the loan term.
- **`fixed`:** `NA`, zero, or an intro period at least as long as the term.
- **`unknown`:** `Exempt`. The lender is exempt from reporting the field (small filers under
  the 2018 partial exemptions).
  - These rows are counted and shown separately, never dropped silently.
  - In 2021 they're 1.8% of loans and 1.4% of dollars.

Two notes on ARMs:
- **Most first resets come at 5, 7 or 10 years.** In 2021, 120, 84 and 60 months together
  make up 85% of ARM loans. 180 months (3.4%) is the next largest cluster.
- **About 1.9% of 2021 ARMs reset after 1 month.** They're mostly interest-only purchase
  loans from private-banking lenders that adjust monthly from the start.

### Holder at origination

`purchaser_type` maps to a holder segment in `config/segments.yaml`:

| Segment | purchaser_type | Where else these loans show up |
|---|---|---|
| retained | 0 (not sold in the origination year) | Call Report buckets |
| gse | 1, 3 (Fannie Mae, Freddie Mac) | Agency pools (v2) |
| ginnie | 2 | Agency pools (v2) |
| private_securitization | 5 | Non-agency RMBS (out of scope) |
| other | 4, 6, 71, 72, 8, 9 | Unknown holder |

- **Farmer Mac (4) isn't in PLAN.md's table.** It isn't an agency MBS pool, so it's under
  `other`.
- **Unlisted codes go to `unmapped`**, and the QA report counts them.
- **"Retained" means not sold in the origination year.** The loan may have been sold later,
  so the segment is a proxy for the current holder.

### Lender type and the link to the Call Reports

**Source:** the Philadelphia Fed's HMDA Lender File (`armtool fetch lenders`).
- One workbook covers every HMDA filer from 2018 to 2025, one row per lender and year.
- It gives each lender's RSSD ID, its parent's and its high holder's, its regulator
  (`agency_code`), and an institution type (`institution_type`) from the Fed's National
  Information Center.
- It replaced the CFPB Reporter Panel, which stops at 2023. The panel's codes also needed a
  chain of rules to classify lenders.

**`lender_type`** maps the institution type in `config/segments.yaml`:

| lender_type | Institution types |
|---|---|
| bank | Commercial banks; savings banks, S&Ls, industrial and cooperative banks; US branches of foreign banks; failed banks and thrifts |
| bank_affiliate | Subsidiaries of banks, thrifts and their holding companies; independent mortgage banks affiliated with a depository |
| credit_union | Credit unions, their subsidiaries and service organizations |
| independent_mortgage_company | Independent mortgage banks |

- **One override:** a lender whose RSSD files a Call Report that year is a `bank`, whatever
  its type code. Only banks and savings associations file Call Reports.
  - This retypes 21 lender-years in 2018–2025, mostly small banks the file codes as credit
    unions.
- **A lender with no Call Report loaded for its year** keeps the type from its code.
- **Credit union subsidiaries and service organizations count as credit unions.** They are
  owned by credit unions, which file NCUA reports, not Call Reports.

**Link details:**
- **`v_bank_hmda_link` gives each lender's match status:**
  - `matched`: the RSSD filed a Call Report that year.
  - `rssd_not_a_call_report_filer`: most credit unions and nonbanks.
  - `no_rssd`
  - `not_in_lender_file`: in the loan file but not the Lender File for that year.
  - `no_lender_file_for_year`: the Lender File doesn't cover the year yet.
- **A bank can be unlinked for a year.** Examples: a bank absorbed early in the year, or a US
  branch of a foreign bank, which files FFIEC 002 instead. The QA report counts these.
- **In 2021, every lender typed `bank` is linked.** Linked lenders originated 81% of that
  year's ARM dollars.

## First-reset calendar

The calendar places every HMDA ARM in the calendar year of its first rate reset (PLAN.md
§7.2). `armtool build` writes it to `fact_reset_calendar`; `v_reset_calendar` adds readable
labels and `v_reset_coverage` says which reset years the data covers in full.

### Which loans

- **ARMs only:** `rate_type = 'arm'`, a positive intro period shorter than the loan term.
  3,225,980 loans in 2018–2025.
- **Exempt rows can't be placed,** because their intro period is unknown. They are 1.8% to
  3.9% of each year's loans; the QA report shows the share by year.

### When the first reset comes

- **Public HMDA gives the origination year, not the date.** Origination dates are assumed to
  be spread evenly over the year.
- **The first reset comes `intro_m` months after origination.** So a loan's reset falls in at
  most two calendar years. A 2021 loan with a 60-month intro resets in 2026; with a 6-month
  intro, half in 2021 and half in 2022.
- **The two shares add up to 1,** so each loan counts once.

### How much balance reaches it

`bal_at_reset = loan_amount × A(k) × S(k)`, where k is `intro_m`, the months to the reset:
- **A(k) is scheduled amortization:** the share of the original balance left after k
  level payments at the note rate over the loan term. At a zero rate it falls in a straight
  line.
- **S(k) is survival, `(1 − CPR)^(k/12)`.** CPR is an annual rate of prepayment and default
  together.
- **The CPR scenarios are assumptions, not estimates.** `model.scenarios` in config.yaml holds
  low 6%, base 10% and high 15%, the placeholders from PLAN.md §10. `dim_scenario` and
  `v_reset_calendar` (`cpr_assumption`) carry the rate behind every number.
- **Interest-only ARMs are assumed to stay interest-only through the first reset,** so A(k)
  is 1 for them. HMDA doesn't report the interest-only period. They are 29% of ARM dollars in
  2018–2025, from 20.6% to 40.5% by year, so this assumption moves the totals.
- **`loan_amount` is the midpoint of a $10k band,** as HMDA publishes it.

### Missing and implausible rates and terms

- **A missing interest rate takes the median** of the ARMs with a rate in the same origination
  year, intro_m bucket and conforming status (PLAN.md §7.2). If that group has no rate, the
  median for the year and bucket is used, then the year's.
- **A missing loan term is filled the same way.** The filled term is one that some loan in the
  group actually has.
- **A reported rate above 20%, or a term above 600 months, counts as missing.** No ARM in
  2018–2025 reports a rate between 17.5% and 29.25%. The 19 above include 362,500 and 5,125,
  which are 3.625% and 5.125% typed without the decimal point. 100 of the 116 terms over 600
  months are 999.
- **Every fill is counted.** 3,664 ARMs (0.1%) have a filled rate and 1,747 a filled term.
  `qa_reset_inputs` counts them by year, intro_m bucket and conforming status, and
  `v_reset_calendar` flags them in `rate_filled` and `term_filled`.

### Holder and lender

- **`holder_segment`** is the holder at origination, from `purchaser_type` (see "Holder at
  origination" above).
- **`lender_type`** comes from the Lender File for the origination year. A lender missing
  from the file would be `unknown`, but in 2018–2025 every lender with loans is in it.

### Coverage

- **A reset year is complete for an intro period only when every origination year behind it
  is loaded.** Loans that reset in year Y after m months were originated in Y − m/12 or the
  year before. An intro period that isn't a whole number of years draws on two origination
  years.
- **`v_reset_coverage` gives the loaded share** for every reset year and intro period, with
  the origination years it needs. The QA report shows the matrix for 2026–2032.
- **With 2018–2025 loaded:**
  - 7-year ARMs (84 months) are complete for every reset year from 2025 to 2032.
  - 5-year ARMs are complete through 2030, and 3-year ARMs through 2028. Later resets come
    from originations not yet published.
  - 10-year ARMs resetting in 2026 or 2027 come from 2016–2017 originations, which HMDA
    doesn't have, and 15-year ARMs resetting before 2033 all do.
- **Lenders below HMDA's reporting thresholds don't file,** so their loans are absent from
  every year.
- **The current year's bar includes resets that already happened** earlier in the year.
  `is_current_year` flags the year of `model.as_of` (2026).

### Subsequent resets (off by default)

- **After its first reset, an ARM resets every 6 or 12 months.** HMDA doesn't record the
  frequency, so `model.subsequent_resets.frequency_months` sets it (12).
- **Turned on, the calendar adds a row for each later reset** (`reset_kind = 'subsequent'`),
  up to the last calendar year. The balance keeps amortizing at the note rate and surviving at
  the scenario's CPR. Interest-only loans start amortizing over the rest of their term at the
  first reset.
- **These are modeled resets, not loans.** The rate after the first reset is unknown, so the
  note rate stands in for it.

## Known limitations

### Call Reports
- **The buckets mix fixed-rate maturities with floating-rate repricing**, and they are
  measured from the report date.
- **The buckets cover domestic offices only.** Foreign-office first-lien loans, material
  only at a few 031 filers, are excluded.
- **Credit unions are absent from the Call Report layer.**

### HMDA
- **Covers originations only;** balances at reset are modeled.
- **CPR is an assumption.** The scenarios are placeholders until you set your own.
- **No origination month**, only the year.
- **Interest-only length is unknown.** A small number of loans have an exempt
  interest-only flag but a reported intro period; they count as not interest-only.
- **Pre-2018 cohorts are missing:** the intro-period field starts in 2018.
- **Small filers are exempt from the intro-period field** (`unknown`), and lenders below
  the HMDA reporting thresholds are absent.
- **"Retained in origination year" is not the same as "held today".**
- **Lender names are cut at 30 characters**, as the Lender File stores them (e.g.
  "LIBERTYVILLE BANK & TRUST COMP"). Matched banks also show their Call Report name.
- **The Lender File is updated each July** when a year is added, at the same address. A
  recorded copy is never downloaded again; to pick up a new release, delete the file and its
  manifest entry, then run `armtool fetch lenders`.

### Across sources
- **The sources overlap by design**, so the tool shows them side by side and never sums
  them.

The HMDA vs Call Report cross-check (PLAN.md §7.3) comes with the bank drill-down page in
phase 5.
