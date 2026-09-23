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

The migration was initially staged as a reviewable copy. The live cutover record
below documents the later approved activation; the rollback procedure remains
the source of truth for recovery.

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
**619 passed, 20 skipped**. Historical-data tests may skip in the isolated worktree;
this result does not replace signal/state characterization.

### Completed frozen-input characterization

The initial attempt using `C:/QuantCore-master/data` could not load SPY/SSO:
its stocks/etfs directories were empty. A complete existing historical snapshot
was subsequently located at
`C:/QuantCore-master/output/issue55-live-staging-20260919-005022`.
The comparison used that **same frozen snapshot** for both revisions:

| Evidence | Result |
|---|---|
| OLD | `6763ca4`, before the runtime writer seam, Excel environment |
| NEW | `56e469a`, candidate including harness cleanup fix, SQLite environment |
| Accounts / scenarios | Taylor, Rosa, Yvonne × all five existing SP500 scenarios = 15 |
| Complete output bytes | Identical, SHA-256 `eb3b4557a0ee2aa53319253eee29cb8866e292a5d17644141ad390a785a659d7` |
| Input manifest | 677 files (637 stock CSVs, 25 ETF CSVs, 15 reference workbooks), all hashes unchanged after both runs |
| Manifest hash | `323fdf35d60224247cdf1366dac926fbb97ca4707d4fe67c827c7ef4cacb3523` (SHA-256 of sorted-key JSON mapping relative paths to SHA-256, default JSON separators) |
| State restoration | All six seeded SP500/SkewLS files restored byte-for-byte in each independent state directory |

Both runs disabled constituent-cache writes with
`QUANTCORE_DISABLE_CONSTITUENTS_CACHE=1` and blocked socket connections. The
existing `data.characterize_constituents` harness generated each matrix. Its
output includes canonical signal hashes and exact SP500 state file UTF-8 bytes.
Detailed output, input manifest and summary remain local under the development
worktree's `output/issue-73/`; no live state or input data was changed.

The first seeded run exposed a Windows case-insensitive cleanup bug in the
harness: interleaving deletion of generated filenames with restoration could
remove an already-restored differently cased filename. Cleanup now removes
created names first and then restores every original. The regression test covers
both `Taylor` and `taylor` filenames. That failed run is not counted as passing
evidence; the successful comparison was rerun with normal Account filenames.

This proves SP500 signal/state equivalence on a frozen historical dataset; the
harness contains no logger calls and does not execute SR/SkewLS signal generation.
The separate runtime rehearsal verifies the storage behavior. It does not prove
that the currently empty live data directories are operational; identify the
active live data location before an approved production release.

### Scheduler activation and rollback record (2026-09-22)

The SQLite backend is now enabled for the live scheduled log writers. The
activation was performed after the frozen-input characterization, real-log
rehearsal, CI checks and a fresh live import/backup. No task was started during
the change. The first ordinary scheduled writer run remains an operational
follow-up because trading tasks must not be started as an offline validation.

On 2026-09-22 at 20:27 Taipei, an authorized elevated **read-only** export
captured all eight QuantCore task XML definitions. Their SHA-256 hashes are
recorded in the local `output/issue-73/task-xml-20260922-202722/manifest.json`.
All use SYSTEM (`S-1-5-18`) and direct Python 3.13 executable actions; executable
and script paths exist, script arguments agree with the expected task definitions,
and no action specifies a working directory. No tasks were started or modified.
This resolves the earlier non-elevated Access is denied inspection failure.
The export agrees with the saved 2026-09-15 inventory. The reviewed activation
targets for log writers are:

| Existing task / entry point | Existing script and arguments | Proposed process setting |
|---|---|---|
| `QuantCore_DailyReport` | `daily_report.py --account all` | `QUANTCORE_LOG_BACKEND=sqlite` |
| `QuantCore_SR_Rebalance_Tue` | `rebalance_ib.py --mode all --account all` | same |
| `QuantCore_SP500_Rebalance_Wed` | `rebalance_ib.py --mode all --account all` | same |
| `QuantCore_SkewLS_DailyCheck` | `rebalance_ib.py --mode skewls --account all` | same |
| Panel manual-order process | existing panel launcher | same; restart after activation |

Preserve the verified Python executable, principal, triggers and script arguments.
Use an explicit launcher-local environment, with the current XML saved first;
never change SYSTEM-wide environment values as an implicit switch for all jobs.

The logger call sites are `daily_report.py`, `rebalance_ib.py` and the panel's
manual-order path. `install_all_tasks.py::task_specs` defines the expected daily
report and Tuesday/Wednesday/SkewLS rebalance task names. It registers direct
Python actions, so editing the legacy `.bat` launchers alone would not switch
those tasks. Verify the installed actions against this source, then route the
confirmed writer actions through the approved process-scoped launcher. Do not
run the installer as a way to inspect or change only this setting: it replaces
multiple tasks.

Use the checked-in `automation/run_with_sqlite_backend.py` wrapper, which sets an
explicit process-scoped `QUANTCORE_LOG_BACKEND=sqlite` for **every scheduled
log-writing process**. The four live actions now wrapped are DailyReport,
Tuesday rebalance, Wednesday rebalance and the SkewLS check. The panel is not a
scheduled task; launch it through the same wrapper when it is used for manual
orders.
Do not rely on setting an environment variable in an interactive terminal: that
does not change an existing panel or a separately launched scheduled process.
Prefer a reviewed launcher that sets the variable and then runs the existing
Python executable, arguments and working directory without changing schedules.
Do not change `accounts.json` or `config.json`. Export and save the actual task
XML and launcher files before any approved release change.

The live activation evidence is stored outside Git under the cutover artifact
directory. The initial import and acceptance checks reported:

| Check | Result |
|---|---|
| Data files | 637 stock CSVs and 25 ETF CSVs present; all ETF files and 598 stocks reached 2026-09-21 |
| SQLite import | 5,054 portfolio snapshots and 921 trades |
| Source reconciliation | Snapshot hash `268d2242612cfb70ba144495e68a8c54ce870bba7f4f78b9049843d67e96db0b`; trade hash `573253ca0c9aef5ad5fe009a73634e815c97eac7b69497f400daf24cd5dca830` |
| Export reconciliation | Both generated Excel projections matched SQLite with zero missing/unexpected keys |
| Atomic backup/restore | Backup restored and reconciled to both source hashes |
| Runtime acceptance | Synthetic append reached 5,055/922 rows, then restored to 5,054/921 and both source hashes |
| Excel compatibility | Current portfolio and trade workbooks stayed byte-identical to the pre-cutover copies |
| Task actions | Four writer actions wrapped; four non-writer tasks unchanged; SYSTEM principal and non-action settings preserved |

The data health audit has two explicit existing value exceptions: one invalid
historical `CSRA` High/Low row and one invalid `VIX` Open row. The `DOW` and
`SNDK` identity rules now allow provider append only when each local file starts
at or after that provider identity's first-trade date; their missing earlier
constituent history remains unresolved. These data findings do not change the
SQLite log hashes or backend activation.

The following operational steps apply after the first scheduled writer run:

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
