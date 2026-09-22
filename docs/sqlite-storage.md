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
