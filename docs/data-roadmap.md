# Data quality and storage roadmap

## Phase 1 — offline audit and observable failures (#46)

Run from an isolated worktree. This command only reads market CSVs; redirect its
JSON output into the worktree, not a live directory:

```powershell
python -m data.health --data-root C:/QuantCore-master/data > data-health-report.json
```

Exit 1 means findings, including missing input directories; 0 means the checks
performed found no issues. Neither means the dataset is proven complete.
The report records file hashes, row counts, date bounds, read/check duration,
missing columns, invalid numeric values, duplicate sessions and unsorted dates.
Stocks require Adj Close; ETFs do not. Zero volume is allowed (including VIX).

For a known Reference Close and caller-verified Constituents, add
`--expected-session YYYY-MM-DD --required-stocks AAPL,MSFT`.
This checks only those stocks for stale endpoints and missing files. It does not
infer listing dates, delistings, exchange holidays or internal session gaps.
Do not supply today's calendar date automatically: Reference Close is the last
completed session, not the upcoming Execution Session.

Update functions return confirmed errors. The automation entry point propagates
them into its existing failure notification and Daily Run Summary. Standalone
and shared CLIs exit nonzero for confirmed errors. Empty vendor responses alone
remain inconclusive: an inactive member of the historical Universe or a market
holiday need not have new bars. Empty responses are not proof of completeness;
the explicit freshness audit is separate. No new notification channel is added.

Local read-only observation on 2026-09-18: 652 CSVs checked in about 9.6 seconds;
CSRA High/Low and VIX Open each contained one invalid value. This is a candidate
data-quality finding, not an instruction to rewrite prices. The generated report
stays local. No strategy performance inference follows from these findings.

Validation: synthetic temporary CSVs and mocked downloads/config/notification;
run `python -m pytest automation/tests/ -v`. Tests needing untracked historical
data can skip in a clean worktree. This phase adds checks and fixes error
reporting; it is not a strategy refactor or storage migration. It does not claim
the OLD/NEW signal/state characterization required for a future migration.

## Subsequent tasks, each with its own Issue / branch / PR

### Current implementation status (2026-09-22)

- Phase 1 and the session/Constituents audit are complete (#46, #48, #49).
- Coverage repair, identity review, historical aliases, and the Constituents
  cache are merged (#50, #51, #54, #55, #56).
- Validated staging, atomic publication, manifests, and the price continuity
  repair are merged (#53, #67). The live Excel logs remain the current writer.
- Roadmap item 3 staging is merged in
  [Issue #69](https://github.com/TaylorYam/QuantCore/issues/69). The real-log
  staging run reconciled without loss. A reversible runtime writer seam is
  prepared in [Issue #73](https://github.com/TaylorYam/QuantCore/issues/73);
  the live backend switch remains a separate, explicitly approved release
  action.

Issue #48 adds an exact-snapshot coverage audit and SP500 stage timing tools;
see [the session coverage study](data-session-study.md) for commands, measured
findings, calendar evidence and limitations. Issue #50 corrects the reference
calendar against the official NYSE registry, adds the symbol naming contract and
classifies/reconciles every coverage gap; see [the coverage repair report](data-coverage-repair.md).
Automatic per-ticker update outcomes remain separate work.

1. Build a verified session/Constituents coverage audit and separate timings for
   loading, indicators and backtests. Add structured per-ticker outcomes including
   unknown/no-data, rather than treating all missing responses as failures.
2. Introduce validated staging, atomic publication and dataset version manifests.
   Keep existing price precision, ordering, adjustment and fill semantics. Define
   historical revision review and rollback; never silently replace research inputs.
3. Migrate copies of account snapshots and order/fill records to SQLite, with
   idempotency keys, transaction boundaries, backup/restore tests and reconciliation.
   Preserve heuristic Strategy attribution as heuristic. After approved cutover,
   export Excel from the single authoritative store. Keep runtime state separate.
4. Benchmark a Parquet/DuckDB research prototype only if measured I/O warrants it.
   Retain code/parameter/dataset versions in backtest results.

Storage/loader refactors must capture OLD before editing and NEW afterward for
3 Accounts × 5 scenarios, comparing signal and state bytes, with isolated state
backup/restore each round. Data corrections that change signals are separate work.
CI green and human approval are required before merge. The live main checkout
is deployment, so switching it is a separate deliberate release action.
