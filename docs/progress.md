# Progress

Where the build stands and what comes next. Claude Code keeps this page current (see
`CLAUDE.md`).

## Status

| Phase | Work (PLAN.md §12) | Status |
|---|---|---|
| 0 | Scaffold, config, settings, manifest, test harness | Done |
| 1 | Call Report fetch and ingest, latest quarter | Done |
| 2 | Call Report history 2018Q1–2026Q2, `dim_bank`, Call Report views | Done |
| 3 | HMDA 2021 and its lenders | Done |
| – | Lender source for every year: the Philadelphia Fed HMDA Lender File | Done (user decision) |
| 4 | HMDA all years, reset calendar model, coverage matrix | **Done; checkpoint waiting on the user** |
| 5 | Streamlit pages 1–4 and 6 | Next, after the review |
| 6 | SQL console, optional NL-to-SQL | |
| 7 | Bloomberg import contract (optional) | |

199 tests pass. All work is on the branch `claude/arm-reset-tool-build-0o47u0`, which is also
GitHub's default branch.

## Phase 4 checkpoint

The three outputs PLAN.md §12 asks for are in `data/qa_report.md` under "First-reset
calendar", which `armtool validate` writes:
- the calendar table by scenario, plus the base scenario by holder segment;
- the coverage matrix: reset year by months to first reset;
- the filled-rate counts, with the filled terms and the interest-only ARMs.

What phase 4 did:
1. **HMDA 2018–2025:** downloaded and checked, year by year (`docs/verification.md`,
   "Phase 4"). The 2021 layout holds every year, except that `1111` appears in
   `intro_rate_period` in 2018–2020; staging already reads it as exempt.
2. **The open coverage question:** answered. The Data Browser downloads hold the loans of the
   lenders that its filers list lacks.
3. **Reset timing:** `reset_year_weights` and its SQL split (§7.2 steps 1–2), with the unit,
   property and SQL-vs-Python tests of §11.
4. **Balance at reset:** `sched_factor`, survival `(1 − CPR)^(k/12)`, and the median fill for
   missing rates and terms, with counts. Reported rates above 20% and terms above 600 months
   are keying errors and are filled too.
5. **Tables:** `fact_reset_calendar`, `qa_reset_inputs`, `dim_scenario`, `dim_code_label`, and
   the view `v_reset_calendar` with labels, the CPR assumption and the current-year flag.
6. **Coverage matrix:** `v_reset_coverage`.
7. **Subsequent resets:** built and tested; off by default (`model.subsequent_resets`).

## Next: phase 5

After the review: the Streamlit pages 1–4 and 6 (PLAN.md §8), with a screenshot of each at
the checkpoint. The bank drill-down page carries the HMDA vs Call Report cross-check (§7.3).

## Open items

None.

## Waiting on the user

- **CPR scenarios.** `model.scenarios` in `config.yaml` (low 6%, base 10%, high 15%) are the
  plan's placeholders. The phase 4 outputs use them and say so. Give your own values, or keep
  these.
- **Two data-quality limits, set in phase 4.** A reported rate above 20% or a term above 600
  months counts as a keying error and gets the median. That covers 19 rates and 116 terms out
  of 3,225,980 ARMs. Say if you want other limits.

## Rebuild the data on a new machine

`data/` isn't in git. After `uv sync`, and with `contact_email` set in `config.local.yaml`:

```bash
uv run armtool fetch cdr        # 34 quarters, 2018Q1 to latest: about 12 minutes, 225 MB
uv run armtool fetch lenders    # Lender File, 15 MB
uv run armtool fetch panel      # CFPB panels 2018-2023 and filers lists 2024-2025 (cross-check)
uv run armtool fetch hmda       # 2018-2025, one year at a time: 25 GB of CSV, about 20 minutes
uv run armtool build && uv run armtool validate
```

Keep at least 15 GB free: each year's CSV needs up to 6 GB while it converts. The Parquet files
that stay take 2.7 GB.

## Reference results

From the builds of 2026-09-25. The Call Reports and HMDA 2021 were built first in the web
session and again on the user's machine, with identical results. A rebuild should match these.
Call Report counts can move a little, because CDR amends its bulk files, and an HMDA year
changes when the Data Browser publishes a newer dataset for it. Rule those out before looking
for a bug.

- **Call Reports:** 34 quarters (2018Q1–2026Q2), 166,554 bank-quarters and 7,285 QA flag
  rows.
  - Every bank-quarter reconciles to within $5k.
  - The share of first-lien balances within 12 months of the report date was 11.3% in
    2018Q1 and 8.7% in 2026Q2.
- **HMDA 2021** (three-year dataset, frozen):
  - 14,154,790 rows downloaded (5.48 GB of CSV);
  - 13,772,373 rows staged;
  - ARMs are 2.85% of loans and 6.66% of dollars;
  - exempt rows are 1.80% and 1.38%.
- **HMDA 2018–2025:** 64,200,870 rows downloaded, 61,373,119 staged and 3,225,980 ARMs.
  2018–2022 come from the three-year datasets, 2023–2024 from the one-year datasets and 2025
  from the snapshot.
- **Lender File** (2026-07-15 release): 39,525 lender-years for 2018–2025, and 15 rows in
  `dim_institution_type`.
- **The 2021 link:** 1,955 lenders match a Call Report filer, and all of them are typed
  `bank`. They originated 81.3% of 2021 ARM dollars.
- **Spot check:** for JPMorgan Chase Bank (RSSD 852218, 2026-06-30), all nine items match its
  filed Call Report.
- **Reset calendar, base scenario (10% CPR):** $83.4bn reaches its first reset in 2026, $80.6bn
  in 2027 and $105.9bn in 2032. 3,664 ARMs have a filled rate and 1,747 a filled term.

## Decisions

| Date | Decision |
|---|---|
| 2026-09-24 | The user supplied the contact email for the User-Agent. It is kept only in `config.local.yaml`. |
| 2026-09-25 | "No repeated data" is the user's standing rule; see `CLAUDE.md`. |
| 2026-09-25 | Lender source: the Philadelphia Fed HMDA Lender File, chosen from three measured options (`docs/verification.md`). |
| 2026-09-25 | A lender whose RSSD files a Call Report that year is typed `bank`, whatever its Lender File code. This changes 21 lender-years in 2018–2025. |
| 2026-09-25 | The project moved from a Claude Code on the web session to a local Claude Code session on the user's machine (WSL). |
| 2026-09-25 | The user chose to keep commits authored as `Claude <noreply@anthropic.com>`, set in this repo's git config, as in the web session. The repo is public. |
| 2026-09-25 | Reported rates above 20% and terms above 600 months are keying errors, filled like missing values. Claude's call, for the user's review. |

## Log

Newest first.

- **2026-09-25:** Phase 4 checkpoint: this page and the README's build status.
- **2026-09-25 (47ed799):** Phase 4: HMDA 2018–2025, the first-reset calendar, the coverage
  matrix and the filled-rate counts.
- **2026-09-25:** Rebuilt `data/` on the user's machine. Every reference result matched.
- **2026-09-25 (793268a):** Handoff to local Claude Code: added `CLAUDE.md` and this page.
- **2026-09-25 (b01a7db):** The Lender File became the lender source, with `fetch lenders`
  and the QA coverage tables.
- **2026-09-25 (bc1fbaa):** Measured the three lender options for 2024–2025. Panel zips
  with macOS metadata now read correctly.
- **2026-09-25 (97db17f):** Phase 3, HMDA 2021 and the 2021 panel.
- **2026-09-25 (c407eb8, a484345):** Phase 2, Call Report history and views.
- **2026-09-24 (9f4d898):** Phase 1, Call Report fetch, ingest and the repricing model for
  the latest quarter.
- **2026-09-24 (0aef4ae):** Phase 0, scaffold.
