# Methodology

How much US residential mortgage balance faces an interest-rate reset in a given period, who
holds it, and how that is changing. This page states every assumption the tool makes. The
checks behind them are in `docs/verification.md`.

## Sources are shown side by side, never summed

| Source | What it measures | Grain |
|---|---|---|
| Call Reports (FFIEC 031/041/051) | Bank-held closed-end first-lien 1–4 family loans by remaining maturity (fixed rate) or next repricing date (floating rate) | Bank × quarter, 6 buckets |
| HMDA LAR | Originations with months until the first rate change (phases 3–4) | Loan × origination year |

The sources overlap by design:
- A bank-retained ARM appears in both HMDA and the Call Report.
- A GSE-sold ARM appears in both HMDA and agency pools.

So the tool never adds numbers across sources.

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

## Known limitations

### Call Reports
- **The buckets mix fixed-rate maturities with floating-rate repricing**, and they are
  measured from the report date.
- **The buckets cover domestic offices only.** Foreign-office first-lien loans, material
  only at a few 031 filers, are excluded.
- **Credit unions are absent from the Call Report layer.**

### Across sources
- **The sources overlap by design**, so the tool shows them side by side and never sums
  them.

The HMDA sections (first-reset calendar, holder segments, coverage matrix, cross-checks)
are added in phases 3–4.
