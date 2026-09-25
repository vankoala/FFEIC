# ARM reset exposure tool

How much US residential mortgage balance faces an interest-rate reset in a given period,
who holds it, and how that is changing. The tool uses two public FFIEC sources and shows
them side by side, never summed:

- **Call Reports** (FFIEC 031/041/051, CDR bulk data): bank-held first-lien 1–4 family
  loans by remaining maturity or next repricing date.
- **HMDA LAR** (CFPB Data Browser API): originations with months until the first rate
  change, rolled forward into a first-reset calendar.

`PLAN.md` is the build plan. `docs/verification.md` records what was checked against real
files, `docs/methodology.md` states every assumption, and `docs/data_dictionary.md`
describes the tables.

## Setup

```bash
uv sync                          # Python 3.11+, installs the `armtool` CLI
```

Put your contact email in `config.local.yaml` before fetching. It goes into the User-Agent
sent to the FFIEC and CFPB servers.

```yaml
# config.local.yaml: gitignored, overrides config.yaml
contact_email: you@example.com
```

Any setting can also be overridden with an `ARMRESET_` environment variable, using `__` for
nesting (for example `ARMRESET_HMDA__KEEP_RAW_CSV=true`). `ARMRESET_CONFIG` points the tool
at a different config file.

## Usage

```bash
uv run armtool fetch cdr                               # Call Report bulk zips, 2018Q1 to latest
uv run armtool build                                   # staging Parquet, DuckDB tables and views
uv run armtool validate                                # data/qa_report.md
uv run armtool status                                  # downloads, as-of dates, tables
uv run armtool spotcheck 852218                        # one bank vs its filed Call Report
```

Downloads go one request at a time, at least 5 seconds apart, and a file recorded in
`data/raw/manifest.json` is never downloaded again.

If the automated CDR download fails, `fetch cdr` prints manual steps. Save the zip from
the CDR bulk-data page into `data/raw/cdr/` and run `fetch cdr` again to record it.

## Build status

| Phase | Work | Status |
|---|---|---|
| 0 | Scaffold, config, settings, manifest, test harness | done |
| 1 | CDR fetch and ingest, latest quarter | done |
| 2 | CDR history, `dim_bank`, CDR views | done |
| 3 | HMDA 2021 and panel | next |
| 4 | HMDA all years, reset calendar, coverage matrix | |
| 5 | Streamlit pages | |
| 6 | SQL console, optional NL-to-SQL | |
| 7 | Bloomberg import contract (optional) | |

## Development

```bash
uv run pytest                    # no network: any connection attempt fails the test
uv run ruff check . && uv run ruff format --check .
```
