# SQLite staging store for account logs

This is the staging implementation for roadmap item 3. It migrates copies of
the two existing Excel logs into one SQLite database while leaving the live
Excel writer and strategy runtime state unchanged.

`automation/sqlite_store.py` owns two tables:

- `portfolio_snapshots`: one row per account, date and symbol. `Strategy` is
  stored exactly as the existing heuristic classification string.
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
