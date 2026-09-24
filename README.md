# ARM reset exposure tool

How much US residential mortgage balance faces an interest-rate reset in a given period,
who holds it, and how that is changing. The tool uses two public FFIEC sources and shows
them side by side, never summed:

- **Call Reports** (FFIEC 031/041/051, CDR bulk data): bank-held first-lien 1–4 family
  loans by remaining maturity or next repricing date.
- **HMDA LAR** (CFPB Data Browser API): originations with months until the first rate
  change, rolled forward into a first-reset calendar.

`PLAN.md` is the build plan. `docs/verification.md` records what was checked against real
files.

## Quickstart

```bash
uv sync                          # Python 3.11+, installs the `armtool` CLI
uv run armtool --help
uv run armtool status            # what's downloaded, as-of dates, manifest summary
```

Set `contact_email` in `config.yaml` before fetching. It goes into the User-Agent sent to
the FFIEC and CFPB servers. Any setting can be overridden with an `ARMRESET_` environment
variable, using `__` for nesting (for example `ARMRESET_HMDA__KEEP_RAW_CSV=true`).
`ARMRESET_CONFIG` points the tool at a different config file.

## Build status

| Phase | Work | Status |
|---|---|---|
| 0 | Scaffold, config, settings, manifest, test harness | done |
| 1 | CDR fetch and ingest, latest quarter | next |
| 2 | CDR history, `dim_bank`, CDR views | |
| 3 | HMDA 2021 and panel | |
| 4 | HMDA all years, reset calendar, coverage matrix | |
| 5 | Streamlit pages | |
| 6 | SQL console, optional NL-to-SQL | |
| 7 | Bloomberg import contract (optional) | |

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```
