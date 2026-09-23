# SQLite account-log store and cutover runbook

This is the storage implementation for roadmap item 3. It migrates the two
existing Excel logs into one SQLite database and provides a reversible runtime
writer seam. Since the 2026-09-22 live activation, SQLite is authoritative for
account logs and Excel is generated as a compatibility projection. Processes
without an explicit backend setting still use the Excel fallback.

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
below documents the approved activation; the rollback procedure remains the
source of truth for recovery.

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
available for trade rows. The code still falls back to Excel when the backend
environment variable is unset. Live scheduled writers and the panel manual-order
process explicitly select SQLite through the checked-in wrapper, as recorded in
the activation section below.

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

## Cutover rehearsal (2026-09-22)

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
The separate runtime rehearsal verifies storage behavior. Market-data quality
remains covered by the separate audits and tracked follow-ups in
`docs/data-roadmap.md`.

### Scheduler activation and rollback record (2026-09-22)

The SQLite backend is enabled for the live scheduled log writers. Activation
followed the frozen-input characterization, real-log rehearsal, CI checks and a
fresh live import/backup. No task was started during the change. The first
ordinary scheduled writer run and its SQLite-to-Excel reconciliation completed
on 2026-09-23; see [First scheduled writer verification](#first-scheduled-writer-verification-2026-09-23).

On 2026-09-22 at 20:27 Taipei, an authorized elevated **read-only** export
captured all eight QuantCore task XML definitions. Their SHA-256 hashes are
recorded in the local `output/issue-73/task-xml-20260922-202722/manifest.json`.
All use SYSTEM (`S-1-5-18`) and direct Python 3.13 executable actions; executable
and script paths exist, script arguments agree with the expected task definitions,
and no action specifies a working directory. No tasks were started or modified.
This resolves the earlier non-elevated Access is denied inspection failure.
The export agrees with the saved 2026-09-15 inventory. The activated log-writer
actions are:

| Existing task / entry point | Existing script and arguments | Active process setting |
|---|---|---|
| `QuantCore_DailyReport` | `daily_report.py --account all` | `QUANTCORE_LOG_BACKEND=sqlite` |
| `QuantCore_SR_Rebalance_Tue` | `rebalance_ib.py --mode all --account all` | same |
| `QuantCore_SP500_Rebalance_Wed` | `rebalance_ib.py --mode all --account all` | same |
| `QuantCore_SkewLS_DailyCheck` | `rebalance_ib.py --mode skewls --account all` | same |
| Panel manual-order process | existing panel launcher | same; restarted after activation |

The verified Python executable, principal, triggers and script arguments were
preserved. The wrapper sets a process-local environment; no SYSTEM-wide
environment value was changed.

The logger call sites are `daily_report.py`, `rebalance_ib.py` and the panel's
manual-order path. `install_all_tasks.py::task_specs` defines the expected daily
report and Tuesday/Wednesday/SkewLS rebalance task names. It registers direct
Python actions, so editing the legacy `.bat` launchers alone does not switch
those tasks. The installed actions were checked against this source during
activation. The checked-in `automation/run_with_sqlite_backend.py` wrapper sets
`QUANTCORE_LOG_BACKEND=sqlite` for the four scheduled writers; the panel uses the
same wrapper for manual orders and was restarted after activation. For future
task edits, preserve the verified executable, arguments, working directory,
principal and triggers. Do not use `install_all_tasks.py` to change only the
backend setting because it replaces multiple tasks. Task XML and launcher
backups from activation are recorded in the local cutover artifact directory.

The live activation evidence is stored outside Git under the cutover artifact
directory. The initial import and acceptance checks on 2026-09-22 reported:

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
constituent history remains unresolved. The source-verified current-history
repair was merged in PR #86 and Issue #81 is closed. CSRA and VIX remain tracked
under [Issue #82](https://github.com/TaylorYam/QuantCore/issues/82). These data
findings do not change the SQLite log hashes or backend activation.

### First scheduled writer verification (2026-09-23)

The first ordinary scheduled `DailyReport` completed from 04:40:02 to 04:40:12
Taipei time. Its run-status record reports `ok` for Taylor, Rosa and Yvonne, with
the top-level job error set to null. A read-only reconciliation of the live
database with both current Excel projections produced:

| Table | Excel rows | SQLite rows | Missing keys | Unexpected keys | Matching SHA-256 |
|---|---:|---:|---:|---:|---|
| `portfolio_snapshots` | 5,097 | 5,097 | 0 | 0 | `8486de36316377cebd4ac92e58898b9954cd3af9040ce56e546f879e4fcd2162` |
| `trades` | 921 | 921 | 0 | 0 | `573253ca0c9aef5ad5fe009a73634e815c97eac7b69497f400daf24cd5dca830` |

The source and database hashes matched for both tables, with no missing or
unexpected rows. This completes the first scheduled-writer release check and
the live cutover follow-up for Issue #73. Reconciliation only reads the SQLite
database and Excel projections; it does not start a trading task or change the
store.

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
