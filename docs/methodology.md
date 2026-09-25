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
- **Nothing is downloaded twice.** A file recorded in the manifest is never fetched again.

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

The HMDA Reporter Panel gives each lender's RSSD ID, regulator (`agency_code`) and
relationship to a depository (`other_lender_code`). Those codes alone misclassify:
- Large credit unions report to the CFPB, not NCUA.
- Code 3 is mostly independent mortgage companies.

So `lender_type` is set by rules in `config/segments.yaml`, first match wins:
1. **bank:** its RSSD files a Call Report that year. Banks and savings associations file
   Call Reports.
2. **credit_union:** regulator NCUA, or "credit union" in the name, or a CFPB-supervised
   depository that files no Call Report.
3. **bank_affiliate:** a mortgage subsidiary or affiliate of a depository (code 1, 2 or 5).
4. **independent_mortgage_company:** code 3, or regulator HUD, or a CFPB-supervised
   non-depository.
5. **bank:** regulator OCC, Fed or FDIC with no Call Report match that year.
6. **unknown:** anything else.

Link details:
- **Rules that need the Call Report link never fire for a year with no Call Reports
  loaded.** Those lenders stay `unknown` rather than being guessed.
- **`v_bank_hmda_link` gives each lender's match status:**
  - `matched`: the RSSD filed a Call Report that year.
  - `rssd_not_a_call_report_filer`: most credit unions and nonbanks.
  - `no_rssd`
  - `not_in_panel`: in the loan file but not the panel.
  - `no_panel_for_year`
- **Panel coverage:** the panel is published for 2018–2023 only. For 2024 on, the tool has
  names without RSSD IDs unless a file with the panel's columns is placed by hand.
  `docs/verification.md` measures the alternatives.

## Known limitations

### Call Reports
- **The buckets mix fixed-rate maturities with floating-rate repricing**, and they are
  measured from the report date.
- **The buckets cover domestic offices only.** Foreign-office first-lien loans, material
  only at a few 031 filers, are excluded.
- **Credit unions are absent from the Call Report layer.**

### HMDA
- **Covers originations only;** balances at reset are modeled (phase 4).
- **No origination month**, only the year.
- **Interest-only length is unknown.** A small number of loans have an exempt
  interest-only flag but a reported intro period; they count as not interest-only.
- **Pre-2018 cohorts are missing:** the intro-period field starts in 2018.
- **Small filers are exempt from the intro-period field** (`unknown`), and lenders below
  the HMDA reporting thresholds are absent.
- **"Retained in origination year" is not the same as "held today".**
- **No panel for 2024 on**, so no RSSD link for those years unless one is supplied.
- **Vintages differ.** The 2021 loan file is the three-year vintage while the panel is the
  Snapshot, so 42 late filers (0.3% of loans) aren't in the panel.

### Across sources
- **The sources overlap by design**, so the tool shows them side by side and never sums
  them.

The first-reset calendar, the coverage matrix and the cross-checks are added in phase 4.
