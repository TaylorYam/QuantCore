# ETF price semantics and staged repair (#68)

Status: code and staged research are in the isolated `fix/issue-68-etf-close-semantics`
worktree. No live ETF CSV or runtime state has been changed. Replacing the live
CSV files remains a separate release action because strategy targets change.

## Price contract

Store provider raw `Open`, `High`, `Low`, and `Close` beside `Adj Close`. Sector
Rotation and Sector Skew LS calculate returns from total-return OHLC: each day's
raw Open/High/Low is multiplied by `Adj Close / Close`, and the adjusted close is
used as the close. Skew LS retains raw Close marks for per-trade share commission
calculations. SP500's SPY moving average, all-time high and drawdown use Adj Close.
VIX uses Close because it has no distributions.

Legacy files may have only partial Adj Close coverage. Until coverage is complete,
load the whole ticker as its existing Close/Open series; do not change price basis
partway through a file. The staged dataset has complete Adj Close coverage and
activates the total-return contract as one consistent history.

The batch migration now preserves yfinance's `Adj Close` field for every ETF,
including inverse ETFs. The daily updater requires valid Adj Close for ETF bars,
checks recent adjusted-history rebases, validates a full-history replacement
against the stored instrument, and writes it atomically. An unverifiable history
remains unchanged and reports an error.

## Source findings

The 14 sector/benchmark files show a clear 2025-12-23 seam. Through 2025-12-22,
their local Close equals the staged provider Adj Close times a fixed per-ticker
scale; the largest observed ratio deviation from its median is 1.1×10⁻⁶. From
2025-12-23, local Close tracks provider raw Close. Replaying the updater's 30-day
overlap check against the boundary detects only XLRE: 13 of 14 symbols have raw
Close overlap within the 1% trigger. The old files lack historical Adj Close for
that second check. This reproduces a plausible append path; the exact command or
job that created the 2025 boundary is not known.

REW, RXD, SDP, SIJ, SMN and SZK each show a false 96–106% local Close return on
2026-05-13. Their staged provider histories remove the jump. ProShares lists the
1:2 reverse splits for those six symbols as effective before the open on
2026-05-28, so May 13 is not the effective split date ([official announcement](https://www.proshares.com/press-releases/proshares-announces-etf-share-splits-052826)).

## Staging and signal evidence

The staging proof covers the 24 sector, benchmark and inverse ETFs. VIX stays
byte-identical to its current file because its invalid Open is tracked separately
in #82 and its Close has no dividend-adjustment mismatch. Every targeted OLD
snapshot is byte-identical to the read-only live source, every NEW hash matches
its staging manifest, all NEW files pass structural checks, and all staged
Adj Close values are present and positive. Provider history ends on 2026-09-21
for the 24 targeted symbols; each existing local 2026-09-22 row is preserved. The OLD copies
provide byte-for-byte rollback.

The 3-account × 5-scenario matrix records signal and state hashes for SR, SkewLS
and SP500 (45 outputs). SR and SP500 outputs match between OLD and NEW for these
scenarios; SP500 states also match the pre-change characterization byte-for-byte.
All 15 SkewLS signal outputs differ because its sector return history is adjusted.
SkewLS threshold decisions are captured in the matrix. All state files used by
the harness were isolated under the research scratch directory; no real state
was read or written.

Backtests use the configured costs, including Skew LS per-trade commissions at
the configured $100,000 account capital:

| Strategy / view | Sharpe | CAGR | Max drawdown |
|---|---:|---:|---:|
| SR, existing hybrid Close | 0.7342 | 11.16% | −22.31% |
| SR, total-return adjusted | 0.7390 | 11.25% | −22.31% |
| SkewLS, existing hybrid Close | 1.4184 | 13.42% | −8.54% |
| SkewLS, total-return adjusted | 1.4590 | 13.79% | −8.51% |

SR weights differ on 35 of 2,947 sessions (maximum daily L1 difference 0.35).
SkewLS long and inverse-proxy weights differ on 1,949 of 2,076 sessions; direct
XLC short weights differ on 870 sessions. SPY above/below-MA200 classification is
unchanged on all 2,947 sessions (2,285 above-MA days, 30 bear entries, and 182
bear days with drawdown at least 15%). The price level, ATH and drawdown values
still change even though those classifications do not.

Rolling Pardo 3:1 WFO used an IS-Sharpe sweep of SR Z-score threshold
`{0.3, 0.4, 0.5}` and SkewLS VIX threshold `{1.8, 2.0, 2.2}`. It ran on both
OLD and NEW views, with N=8 plus N=7/N=9 sensitivity. The table shows the NEW
view; every run passed the Pardo >0.50 and Sharpe >0.40 gates:

| Strategy | N | IS / OOS sessions per fold | Pardo WFE | Sharpe WFE | OOS losing folds | IS fold winners |
|---|---:|---:|---:|---:|---:|---|
| SR | 7 | 882 / 294 | 0.979 | 1.324 | 0 / 7 | 0.3: 2, 0.4: 5, 0.5: 0 |
| SR | 8 | 801 / 267 | 0.932 | 1.111 | 1 / 8 | 0.3: 1, 0.4: 6, 0.5: 1 |
| SR | 9 | 735 / 245 | 0.900 | 1.139 | 2 / 9 | 0.3: 1, 0.4: 7, 0.5: 1 |
| SkewLS | 7 | 621 / 207 | 0.937 | 1.205 | 0 / 7 | 1.8: 3, 2.0: 2, 2.2: 2 |
| SkewLS | 8 | 564 / 188 | 1.079 | 1.244 | 0 / 8 | 1.8: 3, 2.0: 2, 2.2: 3 |
| SkewLS | 9 | 519 / 173 | 1.032 | 1.198 | 0 / 9 | 1.8: 3, 2.0: 3, 2.2: 3 |

N=8 to N=7/N=9 Pardo-WFE deltas are 0.046/0.032 for SR and 0.142/0.047 for
SkewLS, below the 0.15 sensitivity threshold. SkewLS threshold winners split
across the grid, so the current 2.0 setting remains a reasonable non-load-bearing
choice; this task does not retune it or SR's 0.4 threshold. The detailed CSVs
remain in the ignored local research artifact.

## Applying the staged data

The staged files, OLD byte copies and full hash/rollback proof remain in the
ignored local research artifact under `output/issue68-research/`. Regenerate the
stage if any source hash changes before an approved apply. Apply the whole
verified set in a deliberate release window so every strategy reads the same
data contract. Verify live hashes against OLD before replacement and against NEW
afterward. If any NEW verification fails, restore the matching OLD file and
verify its SHA-256. The current live files remain unchanged.

Offline tests: `python -m pytest automation/tests/ -v`.
