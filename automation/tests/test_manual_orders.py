"""Offline contract tests for the Panel manual-order flow.

The seam under test is the public ManualOrderService preview/submit/status API:
it represents the Panel confirmation boundary and receives a fake IB adapter.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from manual_orders import ManualOrderService, ManualOrderStore


class FakeIBState:
    def __init__(self):
        self.managed = ["U001", "U002", "U003"]
        self.open_trades = []
        self.completed_trades = []
        self.placed = []
        self.client_ids = []
        self.primary_exchange = "NASDAQ"
        self.next_status = "Submitted"


class FakeIB:
    def __init__(self, state):
        self.state = state
        self.connected = False

    def connect(self, host, port, *, clientId, timeout):
        self.connected = True
        self.state.client_ids.append(clientId)

    def managedAccounts(self):
        return self.state.managed

    def qualifyContracts(self, contract):
        contract.conId = 123456
        contract.primaryExchange = self.state.primary_exchange
        return [contract]

    def reqOpenOrders(self):
        return list(self.state.open_trades)

    def reqCompletedOrders(self, apiOnly=True):
        assert apiOnly is True
        return list(self.state.completed_trades)

    def placeOrder(self, contract, order):
        order.orderId = len(self.state.placed) + 1
        order.permId = 9000 + order.orderId
        trade = NS(
            contract=contract,
            order=order,
            orderStatus=NS(status=self.state.next_status, filled=0, avgFillPrice=0.0),
            log=[],
        )
        self.state.next_status = "Submitted"
        self.state.placed.append(trade)
        self.state.open_trades.append(trade)
        return trade

    def sleep(self, seconds):
        pass

    def disconnect(self):
        self.connected = False


def service_for(tmp_path, state, *, universe=None):
    accounts = {
        "Taylor": "U001",
        "Rosa": "U002",
        "Yvonne": "U003",
    }

    def account_ctx(name, role):
        return NS(
            name=name,
            account=accounts[name],
            host="127.0.0.1",
            port=4001 + list(accounts).index(name),
            client_id=20 + 10 * list(accounts).index(name) + role,
        )

    return ManualOrderService(
        account_names=list(accounts),
        store=ManualOrderStore(tmp_path / "manual.sqlite3"),
        account_ctx_factory=account_ctx,
        ib_factory=lambda: FakeIB(state),
        universe_loader=lambda: set(universe or {"SPY", "SSO", "META"}),
        order_factory=lambda action, quantity, price: NS(
            action=action,
            totalQuantity=quantity,
            lmtPrice=price,
            orderType="LMT",
            tif="DAY",
            outsideRth=False,
            account="",
            orderRef="",
            orderId=0,
            permId=0,
        ),
    )


def test_panel_preview_submit_and_restart_show_ib_accepted_order(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state)

    preview = service.preview("Taylor", "ZZZZ", 7, 12.34)
    result = service.submit(preview)

    assert (preview.account_name, preview.symbol, preview.quantity,
            preview.limit_price, preview.tif) == ("Taylor", "ZZZZ", 7, 12.34, "DAY")
    assert len(state.placed) == 1
    trade = state.placed[0]
    assert (trade.contract.symbol, trade.order.action, trade.order.totalQuantity,
            trade.order.lmtPrice, trade.order.tif, trade.order.outsideRth,
            trade.order.account) == (
                "ZZZZ", "BUY", 7, 12.34, "DAY", False, "U001"
            )
    assert trade.order.orderRef.startswith("QUANTCORE_MANUAL:TAYLOR:")
    assert result["status"] == "ACCEPTED"
    assert state.client_ids and len(set(state.client_ids)) == 1

    # A fresh service/store instance sees the durable record.
    restarted = service_for(tmp_path, state)
    rows = restarted.list_orders()
    assert len(rows) == 1
    assert rows[0]["order_ref"] == trade.order.orderRef
    assert rows[0]["status"] == "ACCEPTED"


def test_preview_rejects_strategy_universe_and_non_us_contract(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state)

    with pytest.raises(ValueError, match="策略 Universe"):
        service.preview("Taylor", "META", 1, 10.0)

    state.primary_exchange = "TSE"
    with pytest.raises(ValueError, match="美國"):
        service.preview("Taylor", "ZZZZ", 1, 10.0)

    assert state.placed == []


def test_submit_rechecks_universe_and_same_account_duplicate_only(tmp_path):
    state = FakeIBState()
    universe = {"SPY", "SSO"}
    service = service_for(tmp_path, state, universe=universe)
    stale = service.preview("Taylor", "ZZZZ", 1, 10.0)
    universe.add("ZZZZ")

    with pytest.raises(ValueError, match="策略 Universe"):
        service.submit(stale)
    assert state.placed == []

    universe.remove("ZZZZ")
    taylor = service.submit(service.preview("Taylor", "ZZZZ", 1, 10.0))
    rosa = service.submit(service.preview("Rosa", "ZZZZ", 1, 10.0))
    assert taylor["status"] == rosa["status"] == "ACCEPTED"
    assert len(state.placed) == 2

    with pytest.raises(ValueError, match="未結案手動單"):
        service.preview("Taylor", "ZZZZ", 2, 11.0)


def test_order_status_refresh_records_fill_and_keeps_source_identifiable(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state)
    sent = service.submit(service.preview("Taylor", "ZZZZ", 3, 8.25))
    trade = state.open_trades.pop()
    trade.orderStatus.status = "Filled"
    trade.orderStatus.filled = 3
    trade.orderStatus.avgFillPrice = 8.20
    state.completed_trades.append(trade)

    service.refresh_status()

    refreshed = service.list_orders()[0]
    assert sent["status"] == "ACCEPTED"
    assert refreshed["status"] == "FILLED"
    assert refreshed["filled_quantity"] == 3
    assert refreshed["avg_fill_price"] == 8.20


def test_partial_cancel_releases_same_account_symbol_lock(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state)
    service.submit(service.preview("Taylor", "ZZZZ", 3, 8.25))
    trade = state.open_trades.pop()
    trade.orderStatus.filled = 1
    trade.orderStatus.avgFillPrice = 8.2
    state.completed_trades.append(trade)

    service.refresh_status()
    assert service.list_orders()[0]["status"] == "PARTIAL"

    trade.orderStatus.status = "Cancelled"
    service.refresh_status()
    assert service.list_orders()[0]["status"] == "CANCELLED"
    assert service.preview("Taylor", "ZZZZ", 1, 8.1).account_name == "Taylor"


def test_ib_inactive_order_is_persisted_as_rejected(tmp_path):
    state = FakeIBState()
    state.next_status = "Inactive"
    service = service_for(tmp_path, state)

    rejected = service.submit(service.preview("Taylor", "ZZZZ", 1, 8.25))

    assert rejected["status"] == "REJECTED"
    assert rejected["status_label"] == "遭 IB 拒單"


def test_strategy_cancel_guard_recognizes_only_manual_order_refs():
    from manual_orders import strategy_orders_to_cancel

    manual = NS(order=NS(orderRef="QUANTCORE_MANUAL:TAYLOR:abc"))
    strategy = NS(order=NS(orderRef=""))
    other = NS(order=NS(orderRef="QUANTCORE_REBALANCE"))
    assert strategy_orders_to_cancel([manual, strategy, other]) == [strategy, other]
