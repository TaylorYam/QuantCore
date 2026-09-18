# IB broker symbol contract (#52)

QuantCore keeps the S&P/Wikipedia spelling as the canonical symbol in strategy
signals, local data filenames, Combine, Universe classification, runtime
snapshots, and reports. The only broker-specific translation is in
`automation/symbol_contract.py`:

| Canonical / Constituents | IB stock contract `symbol` | `primaryExchange` | Local data file |
|---|---|---|---|
| `BRK.B` | `BRK B` | `NYSE` | `stocks/BRK-B.csv` |
| `BF.B` | `BF B` | `NYSE` | `stocks/BF-B.csv` |

The adapter constructs `Stock(<IB symbol>, "SMART", "USD")`, adding the native
listing exchange for these class shares. Position and ticker responses are
canonicalized before they reach decision, state, or reporting code. Unknown
symbols are unchanged; this is an explicit format mapping, not a rename or
company-identity lookup.

## Verification record

The #52 issue evidence records the observed IB spelling (`BRK B` / `BF B`) for
both contract lookup and returned positions. This change did not open a live
IB/TWS connection because repository rules prohibit IB connections during
development and tests. The offline contract is pinned by
`automation/tests/test_broker_symbol_contract.py`.

For the required paper/TWS check, use the account's paper Gateway only after a
human approves the live-environment step and capture the returned fields from
`reqContractDetails` for both mappings:

```python
from ib_insync import IB
from automation.symbol_contract import ib_stock

ib = IB()
ib.connect("127.0.0.1", PAPER_PORT, clientId=READ_ONLY_CLIENT_ID,
           readonly=True)
for symbol in ("BRK.B", "BF.B"):
    details = ib.reqContractDetails(ib_stock(symbol))
    print(symbol, [
        (d.contract.symbol, d.contract.localSymbol,
         d.contract.primaryExchange, d.contract.conId)
        for d in details
    ])
ib.disconnect()
```

The procedure follows IBKR's contract guidance: a stock contract is identified
by symbol, security type, exchange, and currency, and SMART-routed contracts
should include the native primary exchange. `reqContractDetails` returns the
contract's symbol, local symbol, and exchange fields. See the [IBKR TWS API
stock contract documentation](https://interactivebrokers.github.io/tws-api/basic_contracts.html)
and [contract-details documentation](https://www.interactivebrokers.com/docs/tws-api/doc/contracts-financial-instruments/contract-details/introduction).

## Offline behavior validation

The full SP500 backtest was run on the current local dataset with only
`CLASS_SHARES_TRADABLE` patched in memory. The Constituents workbook currently
ends at 2026-09-16, so that is the inclusive end date used below.

| | A: flag off | B: flag on |
|---|---:|---:|
| Range | 2018-07-02 → 2026-09-16 | 2018-07-02 → 2026-09-16 |
| Sessions | 2,063 | 2,063 |
| Loaded tickers | 598 | 598 |
| History SHA-256 | `d3bbc347d66ffa410d6615dda5f7166b54bd73b53f717d1fe7251d5ddfa05d1e` | same |
| Trades SHA-256 | `949c9fdf96e9791bb6a7d0cd7512c77c6e8d2aed110798c59c33de1d6ed9cdea` | same |

The 3-account × 5-scenario characterization harness also returned identical
weight and state-file hashes for all 15 cases. No live state files, output
files, Telegram destination, or IB connection were used.
