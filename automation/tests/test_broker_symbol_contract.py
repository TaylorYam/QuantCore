"""Offline tests for the Constituents ↔ IB broker symbol boundary (#52)."""

from types import SimpleNamespace as NS

import pandas as pd


def _position(symbol, qty=3):
    return NS(
        account="U00000000",
        contract=NS(symbol=symbol, localSymbol=symbol, secType="STK"),
        position=qty,
        avgCost=100.0,
    )


def test_class_share_mapping_is_explicit_and_contract_is_smart_nyse():
    from symbol_contract import (
        broker_symbol,
        canonical_symbol,
        ib_stock,
        primary_exchange,
    )

    assert canonical_symbol("BRK B") == "BRK.B"
    assert canonical_symbol("BF B") == "BF.B"
    assert broker_symbol("BRK.B") == "BRK B"
    assert broker_symbol("BF.B") == "BF B"
    assert canonical_symbol("AAPL") == "AAPL"
    assert primary_exchange("BRK.B") == "NYSE"

    contract = ib_stock("BRK.B")
    assert contract.symbol == "BRK B"
    assert contract.exchange == "SMART"
    assert contract.currency == "USD"
    assert contract.primaryExchange == "NYSE"


def test_positions_normalize_broker_class_share_to_canonical_and_preserve_local():
    from broker import Connection

    class FakeIB:
        def positions(self, account):
            return [_position("BRK B", 3), _position("BRK.B", 2),
                    _position("AAPL", 1)]

    conn = Connection(FakeIB(), NS(account="U00000000"))
    assert conn.positions() == {
        "BRK.B": 5,
        "AAPL": 1,
    }

    detail = conn.positions_detail()
    assert [p.contract.symbol for p in detail] == ["BRK.B", "BRK.B", "AAPL"]
    assert detail[0].contract.localSymbol == "BRK B"


def test_class_share_is_classified_as_sp500_after_position_normalization():
    import rebalance_plan

    split = rebalance_plan.classify_holdings(
        {"BRK.B": 5, "XLK": 2, "MANUAL": 1},
        {"BRK.B"},
    )
    assert split.sp500 == ["BRK.B"]
    assert split.sr == ["XLK"]
    assert split.other == ["MANUAL"]


def test_price_fallback_uses_provider_filename_for_class_share(tmp_path, monkeypatch):
    from data import market_data
    import price_utils

    stock_dir = tmp_path / "stocks"
    stock_dir.mkdir()
    pd.DataFrame({"Date": ["2026-10-06"], "Close": [500.0]}).to_csv(
        stock_dir / "BRK-B.csv", index=False)
    monkeypatch.setattr(market_data, "DATA_ROOT", tmp_path)

    assert price_utils.load_csv_close("BRK.B") == 500.0


def test_ib_price_request_uses_broker_symbol_and_returns_canonical_key():
    from broker import Connection

    requested = []

    class FakeIB:
        client = NS(port=4001)

        def qualifyContracts(self, *contracts):
            requested.extend(contracts)

        def reqTickers(self, *contracts):
            contract = NS(symbol="BRK B", localSymbol="BRK B")
            return [NS(contract=contract,
                       midpoint=lambda: 501.0,
                       last=0, close=0)]

    prices = Connection(FakeIB(), NS(price_feed_port=None)).prices(["BRK.B"])
    assert prices == {"BRK.B": 501.0}
    assert requested[0].symbol == "BRK B"
    assert requested[0].primaryExchange == "NYSE"


def test_order_path_uses_broker_symbol_in_mock_ib():
    import strategy_orders
    from account_ctx import AccountCtx

    requested = []

    class FakeIB:
        def qualifyContracts(self, contract):
            requested.append(contract)

    ctx = AccountCtx(
        name="Test", account="U00000000", host="127.0.0.1", port=4001,
        client_id=20, dry_run=True, order_type="MOO", outside_rth=False,
        wait_fill_sec=0, alloc_sr=0.0, alloc_sp500=0.5, alloc_skewls=0.0,
        price_feed_port=None, tg_token="", tg_chat_id="",
    )
    from broker import Connection
    sent = strategy_orders.send(
        Connection(FakeIB(), ctx), [{"symbol": "BRK.B", "action": "BUY", "qty": 2}], ctx,
    )
    placed = sent.placed
    assert sent.fills == []
    assert [o["symbol"] for o in placed] == ["BRK.B"]
    assert requested[0].symbol == "BRK B"
    assert requested[0].primaryExchange == "NYSE"


def test_fill_and_report_keys_normalize_ib_class_share():
    import strategy_orders

    trade = NS(
        contract=NS(symbol="BRK B"),
        order=NS(action="BUY", totalQuantity=2),
        fills=[],
        orderStatus=NS(status="Filled"),
    )
    assert strategy_orders.fill_summary([trade])[0]["symbol"] == "BRK.B"

