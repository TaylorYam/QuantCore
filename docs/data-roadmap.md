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
Stocks and ETFs require Adj Close. ETF OHLC remains the provider's raw price
series; strategy return calculations use Adj Close and corresponding adjusted
Open/High/Low only when the ticker's full Adj Close history is available. A
legacy partial history stays on its existing Close/Open basis until rebuilt, so
the price basis never changes partway through a file. VIX has no distributions,
so its blank Adj Close is valid and its consumers and health audit fall back to
Close.
Zero volume is allowed (including VIX).

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

The 2026-09-23 read-only audit checked 664 CSVs and found the two #82 findings:
CSRA High/Low on 2026-08-12 and VIX Open on 2026-04-02. Source-backed repairs
are staged and reconciled in [the #82 data-quality report](data-quality-issue82.md).
The staged audit has no remaining findings. The report makes no strategy
performance inference.

Validation: synthetic temporary CSVs and mocked downloads/config/notification;
run `python -m pytest automation/tests/ -v`. Tests needing untracked historical
data can skip in a clean worktree. This phase adds checks and fixes error
reporting; it is not a strategy refactor or storage migration. It does not claim
the OLD/NEW signal/state characterization required for a future migration.

## Subsequent tasks, each with its own Issue / branch / PR

### Current implementation status (2026-09-23)

- The exact-session / Constituents coverage audit and SP500 stage timings are
  complete (#46, #48, #49); see [the session coverage study](data-session-study.md).
  Structured per-ticker update outcomes remain a tracked follow-up (#83).
- Coverage repair, identity review, historical aliases and the Constituents
  cache are merged (#50, #51, #54, #55, #56). The source-verified DOW/SNDK
  repair is merged as PR #86 and Issue #81 is closed.
- ETF close semantics and inverse-ETF split histories were resolved by merged
  PR #88; Issue #68 is closed and the verified data refresh is recorded locally.
- Validated staging, atomic publication, dataset manifests and price continuity
  repair are merged (#53, #67); see [the coverage repair report](data-coverage-repair.md).
- Roadmap item 3, authoritative SQLite logging, is complete (#69, #73). The
  approved live cutover and first scheduled writer verification are recorded in
  [the SQLite runbook](sqlite-storage.md#first-scheduled-writer-verification-2026-09-23).
  SQLite is the source of truth for account logs; Excel remains a generated
  compatibility projection. Issue #73 is closed.

Issue #50 corrected the reference calendar against the official NYSE registry,
added the symbol naming contract and classified/reconciled coverage gaps. Data
quality follow-ups are tracked separately so unverified price corrections do not
silently alter research inputs.

### Roadmap status

1. **Implemented:** verified session/Constituents coverage and pipeline timings.
   Structured per-ticker outcomes, including unknown/no-data, remain open in
   [Issue #83](https://github.com/TaylorYam/QuantCore/issues/83).
2. **Implemented:** validated staging, atomic publication and dataset manifests
   (#53, #67), with price precision, ordering and adjustment behavior preserved.
3. **Complete:** account snapshots and trade/fill logs use the authoritative
   SQLite store with idempotency, transaction boundaries, backup/restore,
   reconciliation and Excel projections. Runtime state remains separate.
4. **Conditional research:** benchmark Parquet/DuckDB only if measured I/O shows
   a material need; retain code, parameter and dataset versions in any resulting
   backtest records.

### Priority order (2026-09-23)

0. **Release sync:** the live checkout `C:\QuantCore-master` is synchronized
   with `origin/main` at `b09774e`, including PRs #86, #87 and #88. There is no
   outstanding code sync task.
1. [Issue #82](https://github.com/TaylorYam/QuantCore/issues/82): source-verified
   CSRA identity cleanup and VIX OHLC correction are staged; the health-audit
   exception and regression coverage are included in the pending PR.
2. [Issue #83](https://github.com/TaylorYam/QuantCore/issues/83): add structured
   per-ticker update outcomes after the data-quality findings are resolved.
   Empty provider responses must remain inconclusive without an explicit
   freshness failure.

The Parquet/DuckDB prototype remains conditional on measured I/O need; it is not
an active task.

### Tracked follow-ups

- [Issue #82](https://github.com/TaylorYam/QuantCore/issues/82): source-check the
  CSRA and VIX repairs through CI, human review and the planned local data
  publication.
- [Issue #83](https://github.com/TaylorYam/QuantCore/issues/83): structured
  per-ticker update outcomes.

Storage/loader refactors still require OLD/NEW characterization for 3 Accounts ×
5 scenarios, byte comparison of signals and state, and isolated state
backup/restore. Data corrections that change signals require their own evidence.
CI green and human approval remain required before merge. The live main checkout
is deployment; production changes remain deliberate release actions.

The SQLite frozen-input comparison passed byte-for-byte on the same historical
snapshot: all six seeded state files were restored per round and 677 input-file
hashes stayed unchanged. Its offline suite reported 619 passed and 20 skipped.
At the 2026-09-22 activation snapshot, the live store reconciled to 5,054
snapshots and 921 trades; the first scheduled writer result and its later
reconciliation are recorded in the runbook. Do not start trading tasks as an
offline test.
