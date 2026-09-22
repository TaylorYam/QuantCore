# SQLite staging store for account logs

This is the storage implementation for roadmap item 3. It migrates copies of
the two existing Excel logs into one SQLite database and provides a reversible
runtime writer seam. The live-compatible default remains the Excel writer;
the SQLite writer is selected only with an explicit environment variable.

`automation/sqlite_store.py` owns two tables:

- `portfolio_snapshots`: every source row, keyed by account/date/symbol plus
  its occurrence in the source workbook. Excel does not store a capture time,
  so same-day rows are retained instead of silently collapsing different NAV
  or price observations. `Strategy` is stored exactly as the existing
  heuristic classification string.
- `trades`: one row per logged order, including the fill fields. A stable hash
  key is derived from the order identity and its occurrence among otherwise
  identical rows, so repeated imports update the same record.

The database also contains a schema version and indexes for account/date and
account/time queries. State files such as `sp500_state_*.json` and
`skewls_state_*.json` are intentionally not represented in the schema.

## Staging workflow

Run these commands against a copy or an isolated output directory:

```powershell
python -m automation.sqlite_store import `
  --db output/issue-69/quantcore_logs.sqlite3 `
  --portfolio automation/portfolio_tracker.xlsx `
  --trades automation/trade_log.xlsx

python -m automation.sqlite_store reconcile `
  --db output/issue-69/quantcore_logs.sqlite3 `
  --portfolio automation/portfolio_tracker.xlsx `
  --trades automation/trade_log.xlsx

python -m automation.sqlite_store export `
  --db output/issue-69/quantcore_logs.sqlite3 `
  --portfolio output/issue-69/portfolio_tracker.xlsx `
  --trades output/issue-69/trade_log.xlsx
```

Imports run in one SQLite transaction. A validation or writer failure rolls
back all rows from that import. `backup` uses SQLite's online backup API and
publishes the result atomically; `restore` validates the backup before replacing
the target database. The existing Excel files are never modified by `import`
or `reconcile`.

The migration is not a live cutover. A later approval step must first compare
the exported workbooks with the source, run the full characterization harness,
and then change the writer and readers together. Until that step, Excel is the
authoritative log and SQLite is a reviewable copy.

## Runtime writer seam

`automation/excel_logger.py` continues to use Excel when
`QUANTCORE_LOG_BACKEND` is unset (or set to `excel`). Setting
`QUANTCORE_LOG_BACKEND=sqlite` makes the logger initialize or validate
`automation/quantcore_logs.sqlite3`, append through `sqlite_store.append_runtime`
in one transaction, and regenerate both Excel workbooks as compatibility
projections after the database commit. Runtime state JSON files are untouched.

Runtime appends require an explicit capture or order identity. Reusing that
identity updates the same row; a new identity on the same date/account/symbol
is retained as a separate occurrence. Callers can pass `capture_id` to
`append_daily_snapshot` and `append_trade_log`; broker order ids are used when
available for trade rows. The default Excel path does not change until the
characterization and release checks approve the switch.

The isolated staging copy measured a 473.76 ms median to read both Excel
workbooks versus 22.99 ms for the equivalent SQLite queries (5,054 snapshots
and 921 trades, five samples). Rebuilding the Excel compatibility projection
was 612.93 ms median, so the projection remains an intentional compatibility
cost during rollout.

## Real-log staging verification (2026-09-22)

Read-only copies of the live workbooks were imported under an isolated output
directory. The source and database hashes below are over canonical records
sorted by idempotency key:

| Table | Source rows | SQLite rows | Missing keys | Unexpected keys | Hash |
|---|---:|---:|---:|---:|---|
| `portfolio_snapshots` | 5,054 | 5,054 | 0 | 0 | `268d2242612cfb70ba144495e68a8c54ce870bba7f4f78b9049843d67e96db0b` |
| `trades` | 921 | 921 | 0 | 0 | `573253ca0c9aef5ad5fe009a73634e815c97eac7b69497f400daf24cd5dca830` |

The database-exported workbooks reconciled to the same hashes. An atomic
SQLite backup and restore also reconciled to the same source rows. The output
database and exported files are local staging artifacts and are not committed;
the live Excel files were read only. The 39 same-day duplicate-key groups in
the source are retained as separate occurrence rows because 38 groups contain
different NAV or price observations.

## Cutover rehearsal and remaining release gates (2026-09-22)

Reproduce the isolated rehearsal from a development worktree with a fresh output
path (the command refuses an existing output directory):

```powershell
python -m data.rehearse_sqlite_cutover `
  --portfolio C:/QuantCore-master/automation/portfolio_tracker.xlsx `
  --trades C:/QuantCore-master/automation/trade_log.xlsx `
  --output output/issue-73/rehearsal
```

The tool copies the inputs before using the logger and verifies source file
SHA-256 hashes before and after. It never loads account configuration, connects
to IB, sends Telegram, or accesses runtime state. Its local `report.json` records:

- First-use import and export: 5,054 snapshots and 921 trades, with the same
  canonical hashes reported above.
- Two explicit snapshot captures on the same date survive; retrying the first
  updates it. Retrying one order identity updates the fill without another row.
- An injected exception after SQL insertion rolls back the transaction and leaves
  both Excel files byte-identical.
- Backup/restore preserves complete database records, including runtime
  identities, and the restored Excel projections reconcile.
- Restoring the pre-runtime backup returns to the original source records.

Reconciliation now compares occurrence-aware canonical **Excel-visible records**
on both sides. Excel does not carry runtime capture/order keys; comparing these
keys with import-derived keys previously reported a false mismatch after a
runtime append. Internal SQLite identities are not covered by the projection
hash; the rehearsal separately compares complete database records after restore,
while retry tests verify identity behavior. Identical duplicate rows remain
counted, and removing one fails reconciliation.

Offline validation: `python -m pytest automation/tests/ -v` produced
**618 passed, 20 skipped**. Historical-data tests may skip in the isolated worktree;
this result does not replace signal/state characterization.

### Unfinished characterization gate

The OLD capture on the unchanged `3e5c5a8` worktree failed before producing its
matrix: `C:/QuantCore-master/data/etfs` and `data/stocks` were empty when inspected,
so `SPY.csv` and `SSO.csv` could not be loaded. No OLD/NEW equality is claimed.
The harness's `finally` cleanup left its isolated state directory empty.
Provide the authoritative historical-data directory, then capture the unchanged
baseline and candidate against the same frozen inputs. Set
`QUANTCORE_DISABLE_CONSTITUENTS_CACHE=1` to avoid writing caches under the input
reference directory. Use `QUANTCORE_LOG_BACKEND=excel` for OLD and `sqlite` for NEW.
Compare all 15 account/scenario signal outputs and state file bytes, and verify
state restoration after each round. The harness covers SP500 signal/state; it
contains no logger calls, so the separate runtime rehearsal is also required.

### Scheduler activation and rollback plan (not applied)

The current Windows Task Scheduler inventory contains no action referencing
QuantCore. The actual scheduler host/actions must be identified before activation;
the checked-in batch launchers alone are not evidence of installed tasks.

Use an explicit process-scoped `QUANTCORE_LOG_BACKEND=sqlite` in the verified
launcher for **every log-writing process**, including the panel when it can log.
Do not rely on setting an environment variable in an interactive terminal: that
does not change an existing panel or a separately launched scheduled process.
Prefer a reviewed launcher that sets the variable and then runs the existing
Python executable, arguments and working directory without changing schedules.
Do not change `accounts.json` or `config.json`. Export and save the actual task
XML and launcher files before any approved release change.

After CI and human PR approval, and explicit live-release approval:

1. Identify every writer and stop concurrent writes for the cutover window.
   Verify the deployed revision, scheduler actions and panel launch environment.
2. Back up both current Excel files and runtime state; import to the live SQLite
   path only during this approved window. Reconcile both source files, export to
   separate verification paths, reconcile those, and take an atomic SQLite backup.
3. Enable the reviewed launcher environment consistently for all writers, restart
   existing writer processes with that environment, and resume the schedules.
4. After the first normal scheduled write, reconcile the database with both
   projections and verify that runtime state handling is unchanged. Keep Issue
   #73 open until live activation and rollback evidence are recorded.

For an Excel projection error after a successful SQLite commit, treat SQLite as
current: repair access and regenerate **both** projections with `export`. Do not
rerun a trading job to recover a workbook. Export currently writes the two files
sequentially, so a failure can leave one newer than the other; reconcile both
before allowing report consumers to use them.

For rollback to the Excel backend, pause all writers, back up the current SQLite
store, export its latest committed rows to both Excel files and reconcile them,
then restore the previous launcher environment and restart writer processes.
Using the pre-cutover Excel backup after new SQLite writes would lose those new
records. Keep the retired SQLite database as an archive; before a later SQLite
reactivation, reconcile/import the intervening Excel records into a fresh store
rather than reusing a stale database merely because its file exists. Restoring an
older SQLite backup is a separate data-recovery decision, not the normal backend
rollback.
