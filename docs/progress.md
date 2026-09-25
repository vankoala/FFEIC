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
| 4 | HMDA all years, reset calendar model, coverage matrix | **Next** |
| 5 | Streamlit pages 1–4 and 6 | |
| 6 | SQL console, optional NL-to-SQL | |
| 7 | Bloomberg import contract (optional) | |

176 tests pass. All work is on the branch `claude/arm-reset-tool-build-0o47u0`, which is also
GitHub's default branch.

## Next: phase 4

The checkpoint shows the calendar table by scenario, the coverage matrix and the filled-rate
counts (PLAN.md §12). The model is in PLAN.md §7.2, and the tests it needs are in §11.

1. **HMDA 2018–2025.** Run `armtool fetch hmda`: one year at a time, each CSV converted to
   Parquet and then deleted.
   - For each year, record the size, time, rows and vintage in `docs/verification.md`. For
     2021, the Data Browser served the frozen three-year dataset. For 2024, the filer counts
     matched the Lender File's current LAR counts.
   - Confirm the verified layout holds every year: the column names, the `intro_rate_period`
     values (`NA`, `Exempt`), the $10k-band loan amounts, and the two-filter limit.
2. **Answer the open coverage question** under "Open items" below.
3. **Reset timing:** `reset_year_weights` and its SQL split (§7.2 steps 1–2), with the unit
   and property tests from §11.
4. **Balance at reset:** `sched_factor`, survival `(1 − CPR)^(k/12)`, and the interest-rate
   fill (median by year, intro bucket and conforming status), counting the filled rows.
   - Label the CPR scenarios as assumptions everywhere.
   - Label the interest-only rule, which assumes interest-only lasts through the first
     reset.
5. **Tables:** `fact_reset_calendar` and `v_reset_calendar` (PLAN.md §6). Take the holder
   segment from `config/segments.yaml` and `lender_type` from `dim_hmda_lender`.
6. **Coverage matrix** (§7.2 step 5):
   - reset year × intro period, with missing cohorts marked;
   - the exempt share by year;
   - the reporting-threshold note;
   - a flag on the current year's bar.
7. **Subsequent resets:** the toggle stays off by default (step 6), using
   `model.subsequent_resets` in `config.yaml`.
8. **Checkpoint:** show the three outputs and stop.

## Open items

- **Lenders the Data Browser may lack.** For 2024 and 2025, the Lender File lists lenders
  that the Data Browser filers lists don't:
  - 2024: 18 lenders with 23,134 records, none of them in the Lender File's snapshot count;
  - 2025: 122 lenders with 61,959 records (0.5%).

  Check whether their loans are in the Data Browser downloads, and record the answer.

## Waiting on the user

- **CPR scenarios.** In `config.yaml`, `model.scenarios` (low 6%, base 10%, high 15%) are
  placeholders marked "set your own". The phase 4 output uses them and says so. Ask the user
  for their values at the phase 4 checkpoint.

## Rebuild the data on a new machine

`data/` isn't in git. After `uv sync`, and with `contact_email` set in `config.local.yaml`:

```bash
uv run armtool fetch cdr        # 34 quarters, 2018Q1 to latest: about 12 minutes, 225 MB
uv run armtool fetch lenders    # Lender File, 15 MB
uv run armtool fetch panel      # CFPB panels 2018-2023 and filers lists 2024-2025 (cross-check)
uv run armtool fetch hmda --years 2021   # 5.5 GB CSV -> 0.6 GB Parquet, a few minutes
uv run armtool build && uv run armtool validate
```

Phase 4 then fetches the other HMDA years. Keep at least 15 GB free: each year's CSV needs up
to 6 GB while it converts, and the Parquet files stay.

## Reference results

These are from the first build, on 2026-09-25. A rebuild should match them. Call Report
counts can move a little, because CDR amends its bulk files; rule that out before looking for
a bug.

- **Call Reports:** 34 quarters (2018Q1–2026Q2), 166,554 bank-quarters and 7,285 QA flag
  rows.
  - Every bank-quarter reconciles to within $5k.
  - The share of first-lien balances within 12 months of the report date was 11.3% in
    2018Q1 and 8.7% in 2026Q2.
- **HMDA 2021** (three-year dataset, frozen):
  - 14,154,790 rows downloaded (5.48 GB of CSV in 208 seconds);
  - 13,772,373 rows staged;
  - ARMs are 2.85% of loans and 6.66% of dollars;
  - exempt rows are 1.80% and 1.38%.
- **Lender File** (2026-07-15 release): 39,525 lender-years for 2018–2025, and 15 rows in
  `dim_institution_type`.
- **The 2021 link:** 1,955 lenders match a Call Report filer, and all of them are typed
  `bank`. They originated 81.3% of 2021 ARM dollars.
- **Spot check:** for JPMorgan Chase Bank (RSSD 852218, 2026-06-30), all nine items match its
  filed Call Report.

## Decisions

| Date | Decision |
|---|---|
| 2026-09-24 | The user supplied the contact email for the User-Agent. It is kept only in `config.local.yaml`. |
| 2026-09-25 | "No repeated data" is the user's standing rule; see `CLAUDE.md`. |
| 2026-09-25 | Lender source: the Philadelphia Fed HMDA Lender File, chosen from three measured options (`docs/verification.md`). |
| 2026-09-25 | A lender whose RSSD files a Call Report that year is typed `bank`, whatever its Lender File code. This changes 21 lender-years in 2018–2025. |
| 2026-09-25 | The project moved from a Claude Code on the web session to a local Claude Code session on the user's machine (WSL). |

## Log

Newest first.

- **2026-09-25:** Handoff to local Claude Code: added `CLAUDE.md` and this page.
- **2026-09-25 (b01a7db):** The Lender File became the lender source, with `fetch lenders`
  and the QA coverage tables.
- **2026-09-25 (bc1fbaa):** Measured the three lender options for 2024–2025. Panel zips
  with macOS metadata now read correctly.
- **2026-09-25 (97db17f):** Phase 3, HMDA 2021 and the 2021 panel.
- **2026-09-25 (c407eb8, a484345):** Phase 2, Call Report history and views.
- **2026-09-24 (9f4d898):** Phase 1, Call Report fetch, ingest and the repricing model for
  the latest quarter.
- **2026-09-24 (0aef4ae):** Phase 0, scaffold.
