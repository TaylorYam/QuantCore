"""Offline contract tests for the Panel manual-order flow.

The seam under test is the public ManualOrderService preview/submit/status API:
it represents the Panel confirmation boundary and receives a fake IB adapter.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from manual_orders import ManualOrderMonitor, ManualOrderService, ManualOrderStore


class FakeIBState:
    def __init__(self):
        self.managed = ["U001", "U002", "U003"]
        self.open_trades = []
        self.completed_trades = []
        self.fills = []
        self.placed = []
        self.client_ids = []
        self.cancelled_orders = []
        self.notifications = []
        self.primary_exchange = "NASDAQ"
        self.next_status = "Submitted"
        self.cancel_status = "PendingCancel"
        self.cancel_error = None


class FakeIB:
    def __init__(self, state):
        self.state = state
        self.connected = False

    def connect(self, host, port, *, clientId, timeout):
        import asyncio

        assert asyncio.get_event_loop() is not None
        self.connected = True
        self.state.client_ids.append(clientId)

    def managedAccounts(self):
        return self.state.managed

    def qualifyContracts(self, contract):
        contract.conId = 123456
        contract.primaryExchange = self.state.primary_exchange
        return [contract]

    def openTrades(self):
        return list(self.state.open_trades)

    def reqCompletedOrders(self, apiOnly=True):
        assert apiOnly is True
        return list(self.state.completed_trades)

    def fills(self):
        return list(self.state.fills)

    def reqExecutions(self):
        return list(self.state.fills)

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

    def cancelOrder(self, order):
        self.state.cancelled_orders.append(order)
        if self.state.cancel_error:
            raise TimeoutError(self.state.cancel_error)
        trade = next(
            trade for trade in self.state.open_trades
            if trade.order is order
        )
        trade.orderStatus.status = self.state.cancel_status
        return trade

    def sleep(self, seconds):
        pass

    def disconnect(self):
        self.connected = False


def service_for(tmp_path, state, *, universe=None, notifier=None):
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
        notifier=notifier or (lambda account_name, message: (
            state.notifications.append((account_name, message)) or True
        )),
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
    # Manual orders have a dedicated per-account clientId, separate from the
    # strategy rebalance connection (Taylor's role offsets are 0 and 6).
    assert state.client_ids == [26, 26]

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


def test_cancel_waits_for_ib_confirmation_and_only_targets_its_manual_order(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state)
    submitted = service.submit(service.preview("Taylor", "ZZZZ", 3, 8.25))
    trade = state.open_trades[0]
    strategy_trade = NS(
        contract=trade.contract,
        order=NS(account="U001", orderRef="STRATEGY:REBALANCE", orderId=99),
        orderStatus=NS(status="Submitted", filled=0, avgFillPrice=0),
        log=[],
    )
    state.open_trades.append(strategy_trade)

    with pytest.raises(ValueError, match="找不到這筆手動委託"):
        service.cancel("STRATEGY:REBALANCE")

    pending = service.cancel(submitted["order_ref"])

    assert state.cancelled_orders == [trade.order]
    assert pending["status"] == "CANCEL_PENDING"
    assert service.has_active_orders()
    assert state.notifications == []

    trade.orderStatus.status = "Cancelled"
    service.refresh_status()

    assert service.list_orders()[0]["status"] == "CANCELLED"
    assert not service.has_active_orders()
    assert len(state.notifications) == 1


def test_cancel_race_reconciles_fill_and_unknown_cancel_keeps_account_symbol_locked(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state)
    submitted = service.submit(service.preview("Taylor", "ZZZZ", 3, 8.25))
    trade = state.open_trades[0]

    state.cancel_status = "Filled"
    trade.orderStatus.filled = 3
    trade.orderStatus.avgFillPrice = 8.2
    result = service.cancel(submitted["order_ref"])

    assert result["status"] == "FILLED"
    assert result["filled_quantity"] == 3
    assert not service.has_active_orders()
    assert len(state.notifications) == 1
    assert "已成交" in state.notifications[0][1]

    # ApiCancelled only reports that an API cancellation was requested before
    # acknowledgement. It is not the final Cancelled status.
    state2 = FakeIBState()
    service2 = service_for(tmp_path / "second", state2)
    sent = service2.submit(service2.preview("Taylor", "ZZZZ", 2, 8.25))
    state2.cancel_status = "ApiCancelled"
    pending = service2.cancel(sent["order_ref"])
    assert pending["status"] == "CANCEL_PENDING"
    assert service2.has_active_orders()
    with pytest.raises(ValueError, match="未結案手動單"):
        service2.preview("Taylor", "ZZZZ", 1, 8.1)


def test_terminal_notification_is_account_specific_deduped_after_restart_and_keeps_partial_fill(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state)
    sent = service.submit(service.preview("Rosa", "ZZZZ", 4, 12.5))
    trade = state.open_trades.pop()
    trade.orderStatus.status = "Cancelled"
    trade.orderStatus.filled = 2
    trade.orderStatus.avgFillPrice = 12.4
    state.completed_trades.append(trade)

    service.refresh_status()

    assert len(state.notifications) == 1
    account, message = state.notifications[0]
    assert account == "Rosa"
    assert "2/4" in message
    assert service.list_orders()[0]["status"] == "CANCELLED"
    assert service.list_orders()[0]["order_ref"] == sent["order_ref"]

    restarted = service_for(tmp_path, state)
    restarted.refresh_status()
    assert len(state.notifications) == 1


def test_restart_reconciles_completed_order_fills_from_ib_executions(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state)
    submitted = service.submit(service.preview("Taylor", "ZZZZ", 4, 12.5))
    trade = state.open_trades.pop()
    trade.orderStatus.status = "Cancelled"
    # Completed-order snapshots can lose the filled/average fields after a
    # process restart; execution records retain the actual partial fill.
    trade.orderStatus.filled = 0
    trade.orderStatus.avgFillPrice = 0
    state.completed_trades.append(trade)
    state.fills.append(NS(
        contract=trade.contract,
        execution=NS(
            acctNumber="U001", permId=trade.order.permId,
            orderId=trade.order.orderId, clientId=26,
            shares=2.0, price=12.4, execId="manual-fill-1",
            orderRef=submitted["order_ref"],
        ),
    ))

    service.refresh_status()

    order = service.list_orders()[0]
    assert order["order_ref"] == submitted["order_ref"]
    assert order["status"] == "CANCELLED"
    assert order["filled_quantity"] == 2
    assert order["avg_fill_price"] == pytest.approx(12.4)
    assert len(state.notifications) == 1
    assert "2/4" in state.notifications[0][1]
    assert "$12.40" in state.notifications[0][1]
    assert submitted["order_ref"].rsplit(":", 1)[-1][:12].upper() in state.notifications[0][1]


def test_execution_history_can_confirm_full_fill_when_trade_snapshot_is_missing(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state)
    submitted = service.submit(service.preview("Taylor", "ZZZZ", 2, 12.5))
    trade = state.open_trades.pop()
    state.fills.append(NS(
        contract=trade.contract,
        execution=NS(
            acctNumber="U001", permId=trade.order.permId,
            orderId=trade.order.orderId, clientId=26,
            shares=2.0, price=12.45, execId="manual-fill-complete",
            orderRef=submitted["order_ref"],
        ),
    ))

    service.refresh_status()

    order = service.list_orders()[0]
    assert order["status"] == "FILLED"
    assert order["filled_quantity"] == 2
    assert order["avg_fill_price"] == pytest.approx(12.45)
    assert not service.has_active_orders()
    assert len(state.notifications) == 1
    assert "已成交" in state.notifications[0][1]


def test_incomplete_execution_snapshot_does_not_replace_persisted_average(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state)
    submitted = service.submit(service.preview("Taylor", "ZZZZ", 4, 12.5))
    trade = state.open_trades.pop()
    service.store.update(
        submitted["order_ref"], status="PARTIAL", filled_quantity=3,
        avg_fill_price=12.1,
    )
    state.fills.append(NS(
        contract=trade.contract,
        execution=NS(
            acctNumber="U001", permId=trade.order.permId,
            orderId=trade.order.orderId, clientId=26,
            shares=2.0, price=11.9, execId="manual-fill-partial-snapshot",
            orderRef=submitted["order_ref"],
        ),
    ))

    service.refresh_status()

    order = service.list_orders()[0]
    assert order["status"] == "IB_UNKNOWN"
    assert order["filled_quantity"] == 3
    assert order["avg_fill_price"] == pytest.approx(12.1)


def test_missing_ib_order_and_cancel_timeout_remain_locked_until_reconciled(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state)
    submitted = service.submit(service.preview("Taylor", "ZZZZ", 3, 8.25))
    trade = state.open_trades.pop()

    service.refresh_status()
    assert service.list_orders()[0]["status"] == "IB_UNKNOWN"
    assert service.has_active_orders()
    with pytest.raises(ValueError, match="未結案手動單"):
        service.preview("Taylor", "ZZZZ", 1, 8.1)

    state.open_trades.append(trade)
    state.cancel_error = "socket timeout"
    cancel_result = service.cancel(submitted["order_ref"])
    assert cancel_result["status"] == "CANCEL_UNKNOWN"
    assert service.has_active_orders()

    state.cancel_error = None
    trade.orderStatus.status = "Cancelled"
    service.refresh_status()
    assert service.list_orders()[0]["status"] == "CANCELLED"
    assert not service.has_active_orders()


def test_unknown_status_alert_is_deduped_and_cleared_when_ib_reconciles(tmp_path):
    state = FakeIBState()
    service = service_for(
        tmp_path, state,
        notifier=lambda account, message: (
            state.notifications.append((account, message)) or True
        ),
    )
    service.submit(service.preview("Taylor", "ZZZZ", 1, 8.25))
    trade = state.open_trades.pop()

    service.refresh_status()
    assert len(state.notifications) == 1
    assert "需要確認" in state.notifications[0][1]
    service.refresh_status()
    assert len(state.notifications) == 1

    state.open_trades.append(trade)
    service.refresh_status()
    assert service.list_orders()[0]["status"] == "ACCEPTED"
    assert not service.has_pending_notifications()

    state.open_trades.remove(trade)
    service.refresh_status()
    assert service.list_orders()[0]["status"] == "IB_UNKNOWN"
    assert len(state.notifications) == 2


def test_terminal_notification_outbox_retries_failed_send_after_restart(tmp_path):
    state = FakeIBState()
    attempts = []
    notifier = lambda _account, message: (attempts.append(message) or len(attempts) > 1)
    service = service_for(tmp_path, state, notifier=notifier)
    service.submit(service.preview("Taylor", "ZZZZ", 1, 8.25))
    trade = state.open_trades.pop()
    trade.orderStatus.status = "Cancelled"
    state.completed_trades.append(trade)

    service.refresh_status()
    assert len(attempts) == 1
    assert service.has_pending_notifications()

    restarted = service_for(tmp_path, state, notifier=notifier)
    restarted.refresh_status()
    assert len(attempts) == 2
    assert not restarted.has_pending_notifications()

    again = service_for(tmp_path, state, notifier=notifier)
    again.refresh_status()
    assert len(attempts) == 2


def test_expired_order_is_terminal_and_notified_once(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state)
    service.submit(service.preview("Yvonne", "ZZZZ", 2, 8.25))
    trade = state.open_trades.pop()
    trade.orderStatus.status = "Expired"
    state.completed_trades.append(trade)

    service.refresh_status()

    assert service.list_orders()[0]["status"] == "EXPIRED"
    assert [account for account, _message in state.notifications] == ["Yvonne"]


def test_background_monitor_reconciles_without_an_open_manual_order_window():
    import threading

    refreshed = threading.Event()

    class Service:
        def has_active_orders(self):
            return True

        def has_pending_notifications(self):
            return False

        def refresh_status(self):
            refreshed.set()

    monitor = ManualOrderMonitor(Service(), interval_seconds=0.1).start()
    try:
        assert refreshed.wait(1)
    finally:
        monitor.stop()
