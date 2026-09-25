# ARM reset exposure tool: rules for Claude Code

Read `docs/progress.md` at the start of every session: it says where the build stands and
what comes next. Before working on a phase, read the matching sections of `PLAN.md`.

## The plan

- `PLAN.md` is the user's build plan, kept verbatim. Never edit or reformat it.
- Build in the phase order of PLAN.md §12. At each checkpoint, show the user the listed
  outputs and stop until they say to continue.
- Check every VERIFY item (PLAN.md §5 and §13) against a real file or live endpoint, and
  record it in `docs/verification.md`: the date, what was checked and against what, what was
  found, and what changed in the code. Never silently work around a mismatch.

## The user's decisions (binding)

- **No repeated data.** In the user's words: "I don't want repeated data so whatever makes
  that happen is my decision." No loan, lender or bank may be counted twice.
  - `v_cdr_bank_latest` holds only banks that filed in the latest quarter.
  - Each HMDA year comes from one source: the nationwide file, or a full set of state files.
  - Each lender-year comes from one source: the Philadelphia Fed HMDA Lender File.
  - The tool never adds numbers across sources. Call Reports, HMDA and agency pools overlap
    by design.
  - When a design choice could repeat data, pick the option that can't. Record it under
    "No loan is counted twice" in `docs/methodology.md`.
- **Lender source:** the Philadelphia Fed HMDA Lender File, for every year. The CFPB Reporter
  Panel and the Data Browser filers lists are a QA cross-check only.
- **Contact email:** the User-Agent's contact email lives in `config.local.yaml`, which is
  gitignored. Never commit it, and never copy it into a tracked file, a commit message or a
  log.

## Servers and data

- **Government and Federal Reserve servers:** one request at a time, at least 5 seconds
  apart, with the tool's User-Agent.
  - Cache everything.
  - Never re-download a file recorded in `data/raw/manifest.json`.
  - Download only through the `armtool fetch` commands, which enforce these rules.
  - If you must read a documentation page by hand, keep it to one request and note it in
    `docs/verification.md`.
- **`data/` is gitignored and local to each machine.** `docs/progress.md` lists the commands
  that rebuild it.
- **A downloaded file that fails a check is kept and recorded.** A bug in a check must never
  force a second download.

## Code

- **Before every commit**, these must pass: `uv run pytest -q`, `uv run ruff check .` and
  `uv run ruff format --check .`.
- **Tests assert logic and reconciliation, never market values.**
  - Autouse fixtures block the network and run each test in a temporary directory. Keep it
    that way.
- **After adding a rule or a check,** break it on purpose and confirm a test fails. Then
  restore it.
- **Match the surrounding code:** comment density, naming and idioms.
  - Settings live in `config/*.yaml`.
  - Code reads them through `armreset.settings` and `armreset.segments`.
- **Known traps:**
  - Markdown is excluded from ruff so that it never reformats PLAN.md's code blocks.
  - DuckDB's `USING SAMPLE` samples before `WHERE` and clusters. Use
    `ORDER BY hash(...) LIMIT n` instead.
  - Python 3.11 f-strings can't contain backslashes.

## Docs

- **What goes where:**
  - `docs/verification.md`: findings, newest first.
  - `docs/methodology.md`: every assumption, in plain words.
  - `docs/data_dictionary.md`: every table and view.
  - `README.md`: setup, usage and the build-status table.
- **Style:** short sentences, bold lead-ins, bullets and tables. Every number comes from a
  real file, and the text says which.
- **Update the docs in the same commit as the code they describe.**

## Managing progress

You own the progress tracking.

- **`docs/progress.md` is the single status page:** phases, the next steps, open items,
  decisions waiting on the user, and a dated log.
  - Update it whenever a step finishes, a decision is made or a blocker appears.
  - Commit it with the work.
- **Commit in small, verified steps** with descriptive messages, and push.
  - Work on the current branch.
  - Ask before creating or renaming branches.
- **At a checkpoint:**
  1. Run the checks.
  2. Update `docs/progress.md` and the README's build-status table.
  3. Commit and push.
  4. Show the user the checkpoint outputs, then stop.
- **Ask the user only for decisions that are theirs,** such as scope, data sources and
  modeling assumptions. Otherwise decide, record why in the docs, and carry on.
