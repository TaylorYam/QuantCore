# Source-verified market-data corrections (#82)

As of 2026-09-23, two invalid rows were found in the read-only audit of 664
market-data CSVs. Both repairs are staged in the local `output/issue-82-20260923-verified/`
artifact and pass the updated health audit. Live data publication is still
pending review and the normal release step.

## CSRA: reused ticker in the stock archive

`stocks/CSRA.csv` contains the legacy CSRA Inc. share history through
2018-04-03, followed by 29 bars from 2026-08-12 through 2026-09-22. The 2026
bars are not CSRA Inc. shares: the provider identifies CSRA as the Cohen &
Steers Real Assets Active ETF, and the issuer announced that the ETF began
trading on 2026-08-12. CSRA Inc.'s SEC filing says its shares ceased NYSE
trading after the merger was consummated on 2018-04-03. The local 2018-04-03
OHLC values match the independent [historical CSRA Inc. listing](https://www.investing.com/equities/csra-inc-historical-data).

The staged stock archive removes only the 29 post-reuse ETF rows. It preserves
all legacy rows through 2018-04-03, byte-for-byte, and adds an identity rule
that truncates at that date and refuses future appends under the reused symbol.
The 2026 ETF bars are not moved into `etfs/`; that is a distinct instrument
archive and is outside this correction.

Evidence:

- [SEC Form 8-K](https://www.sec.gov/Archives/edgar/data/1646383/000119312518106369/d562306d8k.htm)
  records the merger and NYSE trading suspension on 2018-04-03.
- [Cohen & Steers launch announcement](https://www.cohenandsteers.com/news/cohen-steers-launches-real-assets-active-etf-csra/)
  identifies the new ETF and its 2026-08-12 first trading day.
- Yahoo Finance metadata and the 2026-08-12 history row were captured locally
  with yfinance 1.0 in `evidence/yahoo_csra_identity.json`.

## VIX: corrected official OHLC

For `etfs/VIX.csv` on 2026-04-02, the staged OHLC is
`26.780000 / 27.890000 / 23.870000 / 23.870000` (Open / High / Low / Close),
matching the [Cboe historical VIX CSV](https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv).
The source file hash and exact source row are retained in the local stage
manifest. The existing zero Volume and blank Adj Close are preserved: VIX is an
index without distributions, so calculations use Close as documented in the
[price contract](etf-price-semantics.md). The health audit now honors this
Close fallback and does not treat VIX's blank Adj Close as a data error.

## OLD / NEW reconciliation

| File | OLD SHA-256 | NEW SHA-256 | Change |
|---|---|---|---|
| `stocks/CSRA.csv` | `c1a6f6b442358f9de9d31cb49e4a0a5b3f7e9ea0ef48b1bd6556421beada41ea` | `bc41d159904eeaa30a0b0cd855ef7abea92fe71855977cc493ca14510e12172e` | 627 → 598 rows; remove 29 ETF rows |
| `etfs/VIX.csv` | `bf665791dc541e39936becd548913adbac0e2c2e7e442342023deb8b709682ed` | `290898f63c40ba2e782bcd1f900b6bfd38fe454c26d28fd671ef661b0b29f8e1` | Correct four OHLC fields; retain Volume 0 and blank Adj Close |

Only these two files differ in the dataset manifest. The health audit changed
from two files with findings to 664 files checked and zero files with issues.
The isolated identity audit also passes after the CSRA truncation rule is
applied. `python -m pytest automation/tests/ -v` reports 648 passed and 20
skipped. The tests use offline fixtures; no IB connection, live config,
Telegram action, or strategy performance inference is involved.

The local evidence directory contains the `OLD` and `NEW` data snapshots,
source Cboe CSV, Yahoo identity snapshot and `stage-manifest.json`. It is ignored
by Git and can be removed after the data has been published and the backup is
no longer needed.
