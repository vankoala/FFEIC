# ARM reset exposure tool

How much US residential mortgage balance faces an interest-rate reset in a given period,
who holds it, and how that is changing. The tool uses two public FFIEC sources and shows
them side by side, never summed:

- **Call Reports** (FFIEC 031/041/051, CDR bulk data): bank-held first-lien 1–4 family
  loans by remaining maturity or next repricing date.
- **HMDA LAR** (CFPB Data Browser API): originations with months until the first rate
  change, rolled forward into a first-reset calendar.

`PLAN.md` is the build plan and `docs/progress.md` tracks where it stands.
`docs/verification.md` records what was checked against real files, `docs/methodology.md`
states every assumption, and `docs/data_dictionary.md` describes the tables. `CLAUDE.md` holds
the working rules for Claude Code sessions.

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
uv run armtool fetch lenders                           # Philadelphia Fed HMDA Lender File (15 MB)
uv run armtool fetch hmda                              # HMDA loans, 2018-2025 (25 GB of CSV)
uv run armtool fetch panel --years 2021-2025           # CFPB lender lists (a cross-check only)
uv run armtool build                                   # staging, tables, reset calendar, views
uv run armtool validate                                # data/qa_report.md, with the calendar
uv run armtool status                                  # downloads, as-of dates, tables
uv run armtool spotcheck 852218                        # one bank vs its filed Call Report
uv run armtool app                                     # the dashboard, at http://localhost:8501
```

**The dashboard** (`armtool app`, after `armtool build`) has five pages: the reset calendar
(the home page), bank repricing, a bank drill-down with the HMDA vs Call Report cross-check,
an HMDA explorer and the methodology. It listens on this machine only
(`.streamlit/config.toml`); from Windows, open http://localhost:8501 while it runs in WSL.
It reads the warehouse and never writes to it, so rebuild with `armtool build` and reload
the page.

**Quick start.** The full Call Report history takes about 12 minutes to download, and the
eight HMDA years about 20. For a first look, fetch one quarter and one year:

```bash
uv run armtool fetch cdr --start latest                # latest quarter only, under a minute
uv run armtool fetch lenders
uv run armtool fetch hmda --years 2021                 # needs about 6 GB free while it converts
uv run armtool build && uv run armtool validate        # then open data/qa_report.md
```

Without `--years`, `fetch hmda` downloads every year in `config.yaml` (2018–2025), one at a
time. That's 25 GB of CSV, kept as 2.7 GB of Parquet; the largest year, 2021, is 5.5 GB.

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
| 3 | HMDA 2021 and panel | done |
| 4 | HMDA all years, reset calendar, coverage matrix | done |
| 5 | Streamlit pages | done; checkpoint under review |
| 6 | SQL console, optional NL-to-SQL | |
| 7 | Bloomberg import contract (optional) | |

## Development

```bash
uv run pytest                    # no network: any connection attempt fails the test
uv run ruff check . && uv run ruff format --check .
```
