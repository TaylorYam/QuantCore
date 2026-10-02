"""Offline contract tests for the Panel manual-order flow.

The seam under test is the public ManualOrderService preview/submit/status API:
it represents the Panel confirmation boundary and receives a fake IB adapter.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import sqlite3
from decimal import Decimal
from types import SimpleNamespace as NS
import re

import pytest

import manual_orders
from manual_orders import (
    ManualOrderMonitor, ManualOrderService, ManualOrderStore,
    BrokerQuote, whole_share_quantity,
)


# IB ContractDetails.tradingHours for a SMART-routed US stock (US/Eastern):
# 04:00-20:00 on regular days and 04:00-17:00 on the 2026-11-27 early close.
EXTENDED_TRADING_HOURS = (
    "20260914:0400-20260914:2000;20260915:0400-20260915:2000;"
    "20261127:0400-20261127:1700"
)
# IB ContractDetails.tradingHours for the same listing routed to OVERNIGHT
# (paper, 2026-09-28): 20:00-03:50 ET across midnight, Sunday from 18:00.
OVERNIGHT_TRADING_HOURS = (
    "20260913:1800-20260914:0350;20260914:2000-20260915:0350;"
    "20260915:2000-20260916:0350"
)


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
        self.positions_by_account = {account: {} for account in self.managed}
        self.trading_hours = EXTENDED_TRADING_HOURS
        self.overnight_hours = OVERNIGHT_TRADING_HOURS
        self.valid_exchanges = "SMART,NASDAQ,ARCA,IEX,OVERNIGHT"
        self.contract_time_zone = "US/Eastern"
        self.sent_tifs = []
        self.next_log = []
        self.place_errors = []
        self.pending_errors = []
        # {n: action(ib, trade)} run on the n-th ib.sleep() after placeOrder.
        self.sleep_actions = {}
        self.ib_instances = []
        self.sleeps_since_place = 0
        # (date, close) daily bars returned by reqHistoricalData.
        self.daily_bars = []
        self.history_requests = []
        self.history_error = None
        # NetLiquidation (USD) returned by accountSummary, per account.
        self.net_liquidation = {account: "16000" for account in self.managed}
        self.summary_requests = []


class FakeEvent:
    """The part of ib_insync's errorEvent used by the service: +=, -=, emit."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self

    def emit(self, *args):
        for handler in list(self.handlers):
            handler(*args)


class FakeWrapper:
    """ib_insync's Wrapper.completedOrder turns IB's record into a Trade and
    drops orderState (and its completedStatus)."""

    def completedOrder(self, contract, order, order_state):
        pass


class FakeIB:
    def __init__(self, state):
        self.state = state
        self.connected = False
        self.errorEvent = FakeEvent()
        self.wrapper = FakeWrapper()
        state.ib_instances.append(self)

    def connect(self, host, port, *, clientId, timeout):
        import asyncio

        assert asyncio.get_event_loop() is not None
        self.connected = True
        self.state.client_ids.append(clientId)

    def managedAccounts(self):
        return self.state.managed

    def accountSummary(self, account=""):
        self.state.summary_requests.append(account)
        rows = [NS(account=account, tag="NetLiquidation", value=value, currency="USD")
                for account, value in self.state.net_liquidation.items()]
        # Another account's figure and a non-USD row must never be picked.
        rows.append(NS(account=account, tag="NetLiquidation", value="1", currency="HKD"))
        return rows

    def positions(self, account=""):
        return [
            NS(account=account, contract=NS(
                symbol=symbol, secType="STK", currency="USD", conId=123456,
            ), position=quantity)
            for symbol, quantity in self.state.positions_by_account[account].items()
        ]

    def qualifyContracts(self, contract):
        contract.conId = 123456
        contract.primaryExchange = self.state.primary_exchange
        return [contract]

    def reqContractDetails(self, contract):
        overnight = getattr(contract, "exchange", "") == "OVERNIGHT"
        hours = self.state.overnight_hours if overnight else self.state.trading_hours
        if hours is None:
            return []
        return [NS(
            contract=contract, tradingHours=hours,
            liquidHours=hours if overnight else "20260914:0930-20260914:1600",
            timeZoneId=self.state.contract_time_zone,
            validExchanges=self.state.valid_exchanges,
        )]

    def reqHistoricalData(self, contract, **kwargs):
        self.state.history_requests.append((getattr(contract, "exchange", ""), kwargs))
        if self.state.history_error:
            raise self.state.history_error
        return [NS(date=day, close=close) for day, close in self.state.daily_bars]

    def openTrades(self):
        return list(self.state.open_trades)

    def reqCompletedOrders(self, apiOnly=True):
        assert apiOnly is True
        for trade in self.state.completed_trades:
            # IB's completedOrder message: orderState.status is Cancelled for
            # both an expiry and a trader cancel; completedStatus tells them
            # apart (paper, 2026-09-29).
            self.wrapper.completedOrder(trade.contract, trade.order, NS(
                status=trade.orderStatus.status,
                completedStatus=getattr(trade, "completed_status", ""),
            ))
        return list(self.state.completed_trades)

    def fills(self):
        return list(self.state.fills)

    def reqExecutions(self):
        return list(self.state.fills)

    def placeOrder(self, contract, order):
        self.state.sent_tifs.append(order.tif)
        if getattr(contract, "exchange", "") == "OVERNIGHT" and order.tif == "DAY":
            # IB records an OVERNIGHT-routed DAY order with its Overnight tif.
            order.tif = "OVERNIGHT"
        order.orderId = len(self.state.placed) + 1
        order.permId = 9000 + order.orderId
        trade = NS(
            contract=contract,
            order=order,
            orderStatus=NS(status=self.state.next_status, filled=0, avgFillPrice=0.0),
            log=list(self.state.next_log),
        )
        self.state.next_status = "Submitted"
        self.state.next_log = []
        # IB delivers order warnings asynchronously; they reach errorEvent
        # while the caller waits in ib.sleep().
        self.state.pending_errors.extend(
            (order.orderId, code, message) for code, message in self.state.place_errors
        )
        self.state.place_errors = []
        self.state.sleeps_since_place = 0
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
        self.state.sleeps_since_place += 1
        pending, self.state.pending_errors = self.state.pending_errors, []
        for req_id, code, message in pending:
            self.errorEvent.emit(req_id, code, message, None)
        action = self.state.sleep_actions.get(self.state.sleeps_since_place)
        if action is not None and self.state.placed:
            action(self, self.state.placed[-1])

    def disconnect(self):
        self.connected = False


def service_for(tmp_path, state, *, universe=None, notifier=None,
                quote_reader=None, clock=None):
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
            cashQty="UNSET",
        ),
        notifier=notifier or (lambda account_name, message: (
            state.notifications.append((account_name, message)) or True
        )),
        quote_reader=quote_reader,
        clock=clock,
    )


MKT_NOW = datetime(2026, 9, 14, 18, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("action,allow_short,position,side", [
    ("BUY", False, 0, "ask"),
    ("COVER", False, -10, "ask"),
    ("SELL", False, 10, "bid"),
    ("SELL", True, 0, "bid"),
])
def test_market_cash_uses_correct_live_side_and_sends_whole_share_mkt(
    tmp_path, action, allow_short, position, side,
):
    state = FakeIBState()
    state.positions_by_account["U001"]["ZZZZ"] = position
    seen_sides = []

    def quote(_ib, _contract, requested_side):
        seen_sides.append(requested_side)
        return BrokerQuote(30 if requested_side == "ask" else 25,
                           MKT_NOW, requested_side)

    service = service_for(tmp_path, state, quote_reader=quote, clock=lambda: MKT_NOW)
    preview = service.preview("Taylor", "ZZZZ", None, None, action=action,
                              allow_short=allow_short, amount_usd="100",
                              order_type="MKT")
    assert preview.quantity == (3 if side == "ask" else 4)
    assert preview.conversion_price == (30 if side == "ask" else 25)
    assert "IB 即時買賣報價" in preview.confirmation_text
    assert "報價時點" in preview.confirmation_text
    assert "換算價" in preview.confirmation_text
    assert "預估委託金額" in preview.confirmation_text
    assert "未指定成交價格" in preview.confirmation_text
    assert "實際成交金額可能超出輸入金額" in preview.confirmation_text
    service.validate_preview(preview)
    result = service.submit(preview)
    assert seen_sides == [side, side, side]
    order = state.placed[0].order
    assert order.orderType == "MKT"
    assert order.totalQuantity == preview.quantity
    assert order.outsideRth is False
    assert order.tif == "DAY"
    assert order.cashQty == "UNSET"
    assert order.lmtPrice > 1e300
    assert result["order_type"] == "MKT"


def test_market_explicit_shares_need_no_quote_but_still_require_session(tmp_path):
    state = FakeIBState()
    service = service_for(
        tmp_path, state,
        quote_reader=lambda *_args: pytest.fail("share MKT must not read quote"),
        clock=lambda: MKT_NOW,
    )
    preview = service.preview("Taylor", "ZZZZ", 7, None, order_type="MKT")
    assert preview.quantity == 7
    assert preview.quote is None
    assert "未指定成交價格" in preview.confirmation_text
    service.submit(preview)
    assert state.placed[0].order.totalQuantity == 7

    closed = service_for(tmp_path / "closed", state, clock=lambda: MKT_NOW + timedelta(hours=3))
    with pytest.raises(ValueError, match="一般交易時段"):
        closed.preview("Taylor", "ZZZZ", 7, None, order_type="MKT")


@pytest.mark.parametrize("quote", [
    None,
    BrokerQuote(30, MKT_NOW, "ask", market_data_type=3),
    BrokerQuote(30, MKT_NOW - timedelta(seconds=16), "ask"),
    BrokerQuote(float("nan"), MKT_NOW, "ask"),
    BrokerQuote(30, MKT_NOW, "bid"),
])
def test_market_cash_rejects_missing_delayed_stale_or_wrong_quote(tmp_path, quote):
    state = FakeIBState()
    service = service_for(tmp_path, state, quote_reader=lambda *_args: quote,
                          clock=lambda: MKT_NOW)
    with pytest.raises(ValueError, match="明確股數"):
        service.preview("Taylor", "ZZZZ", None, None,
                        amount_usd="100", order_type="MKT")
    assert not state.placed


def test_market_cash_rejects_changed_quote_even_when_quantity_same(tmp_path):
    state = FakeIBState()
    prices = iter((30, 31, 30, 31))
    service = service_for(
        tmp_path, state,
        quote_reader=lambda _ib, _contract, side: BrokerQuote(next(prices), MKT_NOW, side),
        clock=lambda: MKT_NOW,
    )
    first = service.preview("Taylor", "ZZZZ", None, None,
                            amount_usd="100", order_type="MKT")
    with pytest.raises(ValueError, match="報價"):
        service.validate_preview(first)
    second = service.preview("Taylor", "ZZZZ", None, None,
                             amount_usd="100", order_type="MKT")
    with pytest.raises(ValueError, match="報價"):
        service.submit(second)
    assert not state.placed


def test_market_cash_below_one_share_stops_without_order(tmp_path):
    state = FakeIBState()
    service = service_for(
        tmp_path, state,
        quote_reader=lambda _ib, _contract, side: BrokerQuote(101, MKT_NOW, side),
        clock=lambda: MKT_NOW,
    )
    with pytest.raises(ValueError, match="不足一股"):
        service.preview("Taylor", "ZZZZ", None, None,
                        amount_usd="100", order_type="MKT")
    assert not state.placed


def test_market_submit_rechecks_account_position_and_open_order(tmp_path):
    state = FakeIBState()
    state.positions_by_account["U001"]["ZZZZ"] = 5
    service = service_for(
        tmp_path, state,
        quote_reader=lambda _ib, _contract, side: BrokerQuote(20, MKT_NOW, side),
        clock=lambda: MKT_NOW,
    )
    preview = service.preview("Taylor", "ZZZZ", None, None, action="SELL",
                              amount_usd="60", order_type="MKT")
    state.positions_by_account["U001"]["ZZZZ"] = 4
    with pytest.raises(ValueError, match="持倉"):
        service.submit(preview)
    state.positions_by_account["U001"]["ZZZZ"] = 5
    service.store.create(preview, "QUANTCORE_MANUAL:TAYLOR:other")
    with pytest.raises(ValueError, match="未結案手動單"):
        service.submit(preview)
    assert not state.placed


def test_market_cash_quote_failure_at_submit_blocks_order(tmp_path):
    state = FakeIBState()
    quotes = iter((BrokerQuote(20, MKT_NOW, "ask"), None))
    service = service_for(
        tmp_path, state,
        quote_reader=lambda *_args: next(quotes), clock=lambda: MKT_NOW,
    )
    preview = service.preview("Taylor", "ZZZZ", None, None,
                              amount_usd="60", order_type="MKT")
    with pytest.raises(ValueError, match="明確股數"):
        service.submit(preview)
    assert not state.placed


def test_market_session_uses_nyse_timezone_holiday_and_early_close():
    from manual_orders import is_regular_session

    eastern = ZoneInfo("America/New_York")
    assert is_regular_session(datetime(2026, 9, 14, 9, 30, tzinfo=eastern))
    assert not is_regular_session(datetime(2026, 9, 14, 9, 29, tzinfo=eastern))
    assert not is_regular_session(datetime(2026, 9, 14, 16, 0, tzinfo=eastern))
    assert not is_regular_session(datetime(2026, 9, 7, 10, 0, tzinfo=eastern))
    assert is_regular_session(datetime(2026, 11, 27, 12, 59, tzinfo=eastern))
    assert not is_regular_session(datetime(2026, 11, 27, 13, 0, tzinfo=eastern))


@pytest.mark.parametrize("market_type,tick_type,accepted", [
    (1, 2, True),
    (3, 67, False),
    (2, 2, False),
])
def test_ib_quote_adapter_requires_realtime_selected_side_tick(
    market_type, tick_type, accepted,
):
    from ib_insync import Ticker
    from ib_insync.objects import TickData
    from manual_orders import read_ib_quote

    class FakeQuoteIB:
        def __init__(self):
            self.ticker = Ticker()
            self.ticker.marketDataType = market_type
            self.cancelled = False
            self.requested_type = None

        def reqMarketDataType(self, value):
            self.requested_type = value

        def reqMktData(self, contract, generic, snapshot, regulatory):
            assert (generic, snapshot, regulatory) == ("", False, False)
            return self.ticker

        def sleep(self, _seconds):
            self.ticker.ask = 30
            self.ticker.askSize = 10
            self.ticker.ticks = [TickData(MKT_NOW, tick_type, 30, 10)]
            self.ticker.updateEvent.emit(self.ticker)

        def cancelMktData(self, _contract):
            self.cancelled = True

    ib = FakeQuoteIB()
    if accepted:
        result = read_ib_quote(ib, NS(symbol="ZZZZ"), "ask")
        assert (result.price, result.side, result.market_data_type) == (30, "ask", 1)
    else:
        with pytest.raises(ValueError, match="明確股數"):
            read_ib_quote(ib, NS(symbol="ZZZZ"), "ask")
    assert ib.requested_type == 1
    assert ib.cancelled


def test_existing_manual_order_store_migrates_to_order_type(tmp_path):
    path = tmp_path / "manual.sqlite3"
    store = ManualOrderStore(path)
    preview = service_for(tmp_path, FakeIBState()).preview("Taylor", "ZZZZ", 1, 10.0)
    store.create(preview, "QUANTCORE_MANUAL:TAYLOR:legacy")
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE manual_orders DROP COLUMN order_type")
        conn.execute("DELETE FROM manual_order_schema_migrations WHERE version=3")
    store = ManualOrderStore(path)
    with store._connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(manual_orders)")}
        assert "order_type" in columns
        assert conn.execute(
            "SELECT COUNT(*) FROM manual_order_schema_migrations WHERE version=3"
        ).fetchone()[0] == 1
    assert store.get("QUANTCORE_MANUAL:TAYLOR:legacy")["order_type"] == "LMT"


PRE_MARKET_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)   # 08:00 ET
AFTER_HOURS_NOW = datetime(2026, 9, 14, 21, 0, tzinfo=timezone.utc)  # 17:00 ET
IB_IGNORED_OUTSIDE_RTH = (
    2109,
    "Order Event Warning: Attribute 'Outside Regular Trading Hours' is ignored "
    "based on the order type and destination. PlaceOrder is now being processed.",
)


@pytest.mark.parametrize("session,now,label", [
    ("PRE_MARKET", PRE_MARKET_NOW, "盤前"),
    ("AFTER_HOURS", AFTER_HOURS_NOW, "盤後"),
])
def test_extended_session_lmt_is_confirmed_and_sent_outside_rth_day(
    tmp_path, session, now, label,
):
    state = FakeIBState()
    service = service_for(tmp_path, state, clock=lambda: now)

    preview = service.preview("Taylor", "ZZZZ", 5, 10.25, session=session)

    assert preview.session == session
    text = preview.confirmation_text
    assert f"交易時段：{label}" in text
    assert "限價：$10.25" in text
    assert "效期：DAY" in text
    assert "不跨日" in text
    service.validate_preview(preview)
    result = service.submit(preview)

    order = state.placed[0].order
    assert (order.orderType, order.lmtPrice, order.tif, order.outsideRth,
            order.account, order.totalQuantity) == (
                "LMT", 10.25, "DAY", True, "U001", 5,
            )
    assert (result["session"], result["outside_rth"], result["status"]) == (
        session, 1, "ACCEPTED",
    )


def test_pre_market_order_states_it_can_continue_into_same_day_sessions(tmp_path):
    service = service_for(tmp_path, FakeIBState(), clock=lambda: PRE_MARKET_NOW)
    text = service.preview(
        "Taylor", "ZZZZ", 1, 10.0, session="PRE_MARKET",
    ).confirmation_text
    assert "一般時段與盤後" in text
    assert "美東 20:00" in text


def test_regular_session_lmt_keeps_regular_hours_order_settings(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state, clock=lambda: AFTER_HOURS_NOW)
    preview = service.preview("Taylor", "ZZZZ", 1, 10.0)
    assert preview.session == "REGULAR"
    assert "交易時段：一般時段" in preview.confirmation_text
    result = service.submit(preview)
    assert state.placed[0].order.outsideRth is False
    assert (result["session"], result["outside_rth"]) == ("REGULAR", 0)


@pytest.mark.parametrize("amount", [None, "100"])
def test_extended_session_rejects_mkt_before_contacting_ib(tmp_path, amount):
    state = FakeIBState()
    service = service_for(
        tmp_path, state, clock=lambda: AFTER_HOURS_NOW,
        quote_reader=lambda *_args: pytest.fail("extended MKT must not read quotes"),
    )
    with pytest.raises(ValueError, match="只接受 LMT"):
        service.preview(
            "Taylor", "ZZZZ", None if amount else 5, None,
            amount_usd=amount, order_type="MKT", session="AFTER_HOURS",
        )
    assert state.client_ids == []
    assert state.placed == []


def test_submit_rejects_extended_mkt_preview_without_regular_fallback(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state, clock=lambda: MKT_NOW)
    regular_mkt = service.preview("Taylor", "ZZZZ", 5, None, order_type="MKT")
    with pytest.raises(ValueError, match="只接受 LMT"):
        service.submit(replace(regular_mkt, session="PRE_MARKET"))
    with pytest.raises(ValueError, match="交易時段"):
        service.preview("Taylor", "ZZZZ", 5, 10.0, session="OVERNIGHT")
    assert state.placed == []


@pytest.mark.parametrize("session,now", [
    ("PRE_MARKET", MKT_NOW),                     # regular session is open
    ("AFTER_HOURS", MKT_NOW),
    ("PRE_MARKET", AFTER_HOURS_NOW),             # wrong extended window
    ("AFTER_HOURS", PRE_MARKET_NOW),
    ("PRE_MARKET", datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)),    # Labor Day
    ("PRE_MARKET", datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)),   # Saturday
    ("AFTER_HOURS", datetime(2026, 9, 15, 0, 30, tzinfo=timezone.utc)),  # 20:30 ET
    ("PRE_MARKET", datetime(2026, 9, 14, 13, 29, 30, tzinfo=timezone.utc)),  # 09:29:30 ET
])
def test_extended_session_outside_its_window_is_rejected_not_regularized(
    tmp_path, session, now,
):
    state = FakeIBState()
    service = service_for(tmp_path, state, clock=lambda: now)
    with pytest.raises(ValueError, match="不會改用一般時段"):
        service.preview("Taylor", "ZZZZ", 5, 10.0, session=session)
    assert state.placed == []


def test_extended_session_windows_follow_nyse_calendar_and_early_close():
    from manual_orders import extended_session_at

    eastern = ZoneInfo("America/New_York")

    def at(day, hour, minute=0):
        return extended_session_at(datetime(2026, *day, hour, minute, tzinfo=eastern))

    assert at((9, 14), 3, 59) is None
    assert at((9, 14), 4) == "PRE_MARKET"
    assert at((9, 14), 9, 29) == "PRE_MARKET"
    assert at((9, 14), 9, 30) is None
    assert at((9, 14), 15, 59) is None
    assert at((9, 14), 16) == "AFTER_HOURS"
    assert at((9, 14), 19, 59) == "AFTER_HOURS"
    assert at((9, 14), 20) is None
    assert at((9, 7), 8) is None                  # Labor Day
    assert at((11, 27), 12, 59) is None           # early close regular session
    assert at((11, 27), 13) == "AFTER_HOURS"
    assert at((11, 27), 16, 59) == "AFTER_HOURS"
    assert at((11, 27), 17) is None
    assert extended_session_at(datetime(2026, 9, 14, 8, 0)) is None  # naive
    assert extended_session_at(datetime(2031, 9, 15, 12, 0, tzinfo=timezone.utc)) is None


def test_submit_rechecks_extended_window_and_does_not_send_in_regular_session(tmp_path):
    state = FakeIBState()
    now = [datetime(2026, 9, 14, 13, 28, tzinfo=timezone.utc)]  # 09:28 ET
    service = service_for(tmp_path, state, clock=lambda: now[0])
    preview = service.preview("Taylor", "ZZZZ", 5, 10.0, session="PRE_MARKET")
    # The last minute is reserved so a forced cancel can finish before the
    # opening auction if IB drops outsideRth.
    for later in (timedelta(minutes=1), timedelta(minutes=2)):  # 09:29, 09:30 ET
        now[0] = datetime(2026, 9, 14, 13, 28, tzinfo=timezone.utc) + later
        with pytest.raises(ValueError, match="不會改用一般時段"):
            service.validate_preview(preview)
        with pytest.raises(ValueError, match="不到 1 分鐘"):
            service.submit(preview)
    assert state.placed == []
    assert service.list_orders() == []


def test_after_hours_stops_accepting_in_its_last_minute(tmp_path):
    state = FakeIBState()
    last_minute = datetime(2026, 9, 14, 23, 59, 30, tzinfo=timezone.utc)  # 19:59:30 ET
    service = service_for(tmp_path, state, clock=lambda: last_minute)
    with pytest.raises(ValueError, match="不到 1 分鐘"):
        service.preview("Taylor", "ZZZZ", 5, 10.0, session="AFTER_HOURS")
    assert state.placed == []


def _emit_2109(ib, trade):
    ib.errorEvent.emit(trade.order.orderId, *IB_IGNORED_OUTSIDE_RTH, None)


def _acknowledge(_ib, trade):
    trade.orderStatus.status = "Submitted"


def test_delayed_2109_before_ib_acknowledgement_still_cancels(tmp_path):
    state = FakeIBState()
    state.next_status = "PendingSubmit"
    state.sleep_actions = {3: _emit_2109, 4: _acknowledge}
    service = service_for(tmp_path, state, clock=lambda: AFTER_HOURS_NOW)

    result = service.submit(
        service.preview("Taylor", "ZZZZ", 5, 10.0, session="AFTER_HOURS"),
    )

    assert state.cancelled_orders == [state.placed[0].order]
    assert result["status"] == "CANCEL_PENDING"
    assert "2109" in result["status_message"]


def test_2109_delivered_with_the_acknowledgement_is_still_seen(tmp_path):
    state = FakeIBState()
    state.next_status = "PendingSubmit"
    state.sleep_actions = {1: _acknowledge, 2: _emit_2109}
    service = service_for(tmp_path, state, clock=lambda: AFTER_HOURS_NOW)

    result = service.submit(
        service.preview("Taylor", "ZZZZ", 5, 10.0, session="AFTER_HOURS"),
    )

    assert state.cancelled_orders == [state.placed[0].order]
    assert "2109" in result["status_message"]


def test_unacknowledged_extended_order_is_cancelled_fail_closed(tmp_path):
    state = FakeIBState()
    state.next_status = "PendingSubmit"  # IB never answers placeOrder
    service = service_for(tmp_path, state, clock=lambda: AFTER_HOURS_NOW)

    result = service.submit(
        service.preview("Taylor", "ZZZZ", 5, 10.0, session="AFTER_HOURS"),
    )

    assert state.cancelled_orders == [state.placed[0].order]
    assert len(state.placed) == 1
    assert result["status"] == "CANCEL_PENDING"
    assert "無法確認盤前盤後設定" in result["status_message"]


def test_unconfirmed_forced_cancel_keeps_lock_alerts_and_is_retried(tmp_path):
    state = FakeIBState()
    state.place_errors = [IB_IGNORED_OUTSIDE_RTH]
    state.cancel_error = "socket timeout"
    service = service_for(tmp_path, state, clock=lambda: AFTER_HOURS_NOW)

    result = service.submit(
        service.preview("Taylor", "ZZZZ", 5, 10.0, session="AFTER_HOURS"),
    )

    trade = state.placed[0]
    assert result["status"] == "CANCEL_UNKNOWN"
    assert "取消結果待確認" in result["status_message"]
    assert any("需要確認" in message for _account, message in state.notifications)
    with pytest.raises(ValueError, match="未結案手動單"):
        service.preview("Taylor", "ZZZZ", 1, 10.0, session="AFTER_HOURS")

    state.cancel_error = None
    service.refresh_status()      # IB still shows it working: cancel again
    assert state.cancelled_orders == [trade.order, trade.order]
    assert service.list_orders()[0]["status"] == "CANCEL_PENDING"
    service.refresh_status()      # IB now reports PendingCancel: no duplicate
    assert len(state.cancelled_orders) == 2

    trade.orderStatus.status = "Cancelled"
    service.refresh_status()
    assert service.list_orders()[0]["status"] == "CANCELLED"
    assert not service.has_active_orders()
    terminal = state.notifications[-1][1]
    assert "結果：已取消" in terminal
    assert "自動取消原因" in terminal and "2109" in terminal


@pytest.mark.parametrize("warning_sleep", [3, 11])
def test_2109_after_submitted_status_within_confirmation_window_still_cancels(
    tmp_path, warning_sleep,
):
    # IB answers Submitted on the first pump; 2109 arrives later than the old
    # 0.5 s grace (sleep 3) and at the end of the 5 s confirmation window.
    state = FakeIBState()
    state.sleep_actions = {warning_sleep: _emit_2109}
    service = service_for(tmp_path, state, clock=lambda: AFTER_HOURS_NOW)

    result = service.submit(
        service.preview("Taylor", "ZZZZ", 5, 10.0, session="AFTER_HOURS"),
    )

    assert state.cancelled_orders == [state.placed[0].order]
    assert result["status"] == "CANCEL_PENDING"
    assert "2109" in result["status_message"]
    assert "2109" in result["forced_cancel_reason"]
    assert result["outside_rth_confirmed_at"] is None


def test_extended_order_is_confirmed_only_after_a_quiet_confirmation_window(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state, clock=lambda: AFTER_HOURS_NOW)
    result = service.submit(
        service.preview("Taylor", "ZZZZ", 5, 10.0, session="AFTER_HOURS"),
    )
    assert result["status"] == "ACCEPTED"
    assert result["outside_rth_confirmed_at"]
    assert result["forced_cancel_reason"] is None
    assert state.cancelled_orders == []

    regular = service.submit(service.preview("Rosa", "ZZZZ", 5, 10.0))
    assert regular["outside_rth_confirmed_at"] is None


def test_connection_lost_before_confirmation_keeps_cancel_state_across_reconnect(tmp_path):
    state = FakeIBState()

    def drop_connection(_ib, _trade):
        raise ConnectionError("socket closed")

    state.sleep_actions = {4: drop_connection}
    state.cancel_error = "not connected"
    service = service_for(tmp_path, state, clock=lambda: AFTER_HOURS_NOW)

    result = service.submit(
        service.preview("Taylor", "ZZZZ", 5, 10.0, session="AFTER_HOURS"),
    )

    trade = state.placed[0]
    assert result["status"] == "CANCEL_UNKNOWN"
    assert "確認盤前盤後設定前中斷" in result["forced_cancel_reason"]
    assert any("需要確認" in message for _account, message in state.notifications)

    state.cancel_error = None
    restarted = service_for(tmp_path, state, clock=lambda: AFTER_HOURS_NOW)
    restarted.refresh_status()
    assert state.cancelled_orders == [trade.order, trade.order]
    assert restarted.list_orders()[0]["status"] == "CANCEL_PENDING"


def test_reconcile_cancels_extended_order_left_unconfirmed_by_a_crash(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state, clock=lambda: AFTER_HOURS_NOW)
    regular = service.submit(service.preview("Rosa", "ZZZZ", 1, 10.0))
    # The Panel died after IB accepted this order but before confirmation.
    crashed = service.preview("Taylor", "ZZZZ", 5, 10.0, session="AFTER_HOURS")
    order_ref = "QUANTCORE_MANUAL:TAYLOR:crashed"
    service.store.create(crashed, order_ref)
    service.store.update(order_ref, status="ACCEPTED", status_message="IB 已接受")
    trade = NS(
        contract=NS(symbol="ZZZZ", secType="STK", currency="USD", conId=123456),
        order=NS(account="U001", orderRef=order_ref, orderId=55, permId=9055,
                 totalQuantity=5),
        orderStatus=NS(status="Submitted", filled=0, avgFillPrice=0.0),
        log=[],
    )
    state.open_trades.append(trade)

    service_for(tmp_path, state, clock=lambda: AFTER_HOURS_NOW).refresh_status()

    assert state.cancelled_orders == [trade.order]
    row = service.store.get(order_ref)
    assert row["status"] == "CANCEL_PENDING"
    assert "確認盤前盤後設定前中斷" in row["forced_cancel_reason"]
    assert service.store.get(regular["order_ref"])["status"] == "ACCEPTED"


def test_reconcile_cancels_confirmed_order_that_ib_records_without_outside_rth(tmp_path):
    # Paper, 2026-09-28: orders IB answered with 2109 were later read back on
    # a fresh connection with outsideRth=False, while honoured extended-hours
    # orders read back True. That record needs no 2109 timing to be seen.
    state = FakeIBState()
    service = service_for(tmp_path, state, clock=lambda: AFTER_HOURS_NOW)
    submitted = service.submit(
        service.preview("Taylor", "ZZZZ", 5, 10.0, session="AFTER_HOURS"),
    )
    regular = service.submit(service.preview("Rosa", "ZZZZ", 1, 10.0))
    assert submitted["outside_rth_confirmed_at"]
    extended_trade, regular_trade = state.placed
    assert regular_trade.order.outsideRth is False

    extended_trade.order.outsideRth = False  # IB's record on the next connection
    service.refresh_status()

    assert state.cancelled_orders == [extended_trade.order]
    row = service.store.get(submitted["order_ref"])
    assert row["status"] == "CANCEL_PENDING"
    assert "outsideRth=False" in row["forced_cancel_reason"]
    assert service.store.get(regular["order_ref"])["status"] == "ACCEPTED"

    extended_trade.orderStatus.status = "Cancelled"
    service.refresh_status()
    assert "自動取消原因" in state.notifications[-1][1]
    assert "outsideRth=False" in state.notifications[-1][1]


def test_user_cancel_timeout_is_not_auto_retried(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state, clock=lambda: AFTER_HOURS_NOW)
    submitted = service.submit(
        service.preview("Taylor", "ZZZZ", 5, 10.0, session="AFTER_HOURS"),
    )
    state.cancel_error = "socket timeout"
    assert service.cancel(submitted["order_ref"])["status"] == "CANCEL_UNKNOWN"
    state.cancel_error = None
    service.refresh_status()
    assert len(state.cancelled_orders) == 1


@pytest.mark.parametrize("trading_hours,time_zone", [
    ("20260914:0930-20260914:1600", "US/Eastern"),    # RTH-only contract
    ("20260914:CLOSED;20260915:0400-20260915:2000", "US/Eastern"),
    (None, "US/Eastern"),                             # no contract details
    ("not-ib-hours", "US/Eastern"),
    ("20260914:0400-20260914:2000", "Mars/Olympus"),
])
def test_extended_session_requires_ib_contract_hours_to_cover_now(
    tmp_path, trading_hours, time_zone,
):
    state = FakeIBState()
    state.trading_hours = trading_hours
    state.contract_time_zone = time_zone
    service = service_for(tmp_path, state, clock=lambda: AFTER_HOURS_NOW)
    with pytest.raises(ValueError, match="合約"):
        service.preview("Taylor", "ZZZZ", 5, 10.0, session="AFTER_HOURS")
    assert state.placed == []


def test_extended_session_accepts_legacy_ib_trading_hours_format(tmp_path):
    state = FakeIBState()
    state.trading_hours = "20260914:0400-0930,0930-1600,1600-2000"
    service = service_for(tmp_path, state, clock=lambda: AFTER_HOURS_NOW)
    service.submit(service.preview("Taylor", "ZZZZ", 5, 10.0, session="AFTER_HOURS"))
    assert state.placed[0].order.outsideRth is True


@pytest.mark.parametrize(
    "action,allow_short,position,expected_action,expected_position",
    [
        ("BUY", False, 0, "BUY", 3),
        ("SELL", False, 4, "SELL", 1),
        ("SELL", True, 1, "SELL", -2),
        ("COVER", False, -5, "BUY", -2),
    ],
)
def test_extended_cash_amount_floors_by_manual_limit_with_position_rules(
    tmp_path, action, allow_short, position, expected_action, expected_position,
):
    state = FakeIBState()
    state.positions_by_account["U001"]["ZZZZ"] = position
    service = service_for(tmp_path, state, clock=lambda: PRE_MARKET_NOW)
    preview = service.preview(
        "Taylor", "ZZZZ", None, 30.0, amount_usd="100", action=action,
        allow_short=allow_short, session="PRE_MARKET",
    )
    assert (preview.quantity, preview.projected_position) == (3, expected_position)
    assert "輸入美元金額：$100（預估交易目標）" in preview.confirmation_text
    service.submit(preview)
    order = state.placed[0].order
    assert (order.action, order.totalQuantity, order.lmtPrice, order.outsideRth) == (
        expected_action, 3, 30.0, True,
    )
    assert order.cashQty == "UNSET"


def test_extended_session_keeps_sell_short_cover_limits_and_submit_recheck(tmp_path):
    state = FakeIBState()
    state.positions_by_account["U001"]["ZZZZ"] = 2
    state.positions_by_account["U002"]["ZZZZ"] = -1
    service = service_for(tmp_path, state, clock=lambda: AFTER_HOURS_NOW)
    with pytest.raises(ValueError, match="允許放空"):
        service.preview("Taylor", "ZZZZ", 3, 10.0, action="SELL", session="AFTER_HOURS")
    with pytest.raises(ValueError, match="空單"):
        service.preview("Rosa", "ZZZZ", 2, 10.0, action="COVER", session="AFTER_HOURS")
    sell = service.preview("Taylor", "ZZZZ", 2, 10.0, action="SELL", session="AFTER_HOURS")
    state.positions_by_account["U001"]["ZZZZ"] = 1
    with pytest.raises(ValueError, match="允許放空"):
        service.submit(sell)
    assert state.placed == []


def test_ib_ignoring_outside_rth_is_cancelled_instead_of_left_for_regular_session(tmp_path):
    state = FakeIBState()
    state.place_errors = [IB_IGNORED_OUTSIDE_RTH]
    service = service_for(tmp_path, state, clock=lambda: AFTER_HOURS_NOW)

    result = service.submit(
        service.preview("Taylor", "ZZZZ", 5, 10.0, session="AFTER_HOURS"),
    )

    trade = state.placed[0]
    assert state.cancelled_orders == [trade.order]
    assert len(state.placed) == 1
    assert result["status"] == "CANCEL_PENDING"
    assert "2109" in result["status_message"]
    assert "不會改以一般時段成交" in result["status_message"]
    trade.orderStatus.status = "Cancelled"
    service.refresh_status()
    assert service.list_orders()[0]["status"] == "CANCELLED"
    assert "盤後 DAY" in state.notifications[-1][1]


def test_outside_rth_warning_for_another_order_does_not_cancel_this_one(tmp_path):
    state = FakeIBState()
    state.pending_errors = [(999, *IB_IGNORED_OUTSIDE_RTH)]
    service = service_for(tmp_path, state, clock=lambda: AFTER_HOURS_NOW)
    result = service.submit(
        service.preview("Taylor", "ZZZZ", 5, 10.0, session="AFTER_HOURS"),
    )
    assert result["status"] == "ACCEPTED"
    assert state.cancelled_orders == []


def test_ib_rejection_of_extended_order_is_final_and_not_resent_as_regular(tmp_path):
    state = FakeIBState()
    state.next_status = "Inactive"
    state.next_log = [NS(message="Order rejected - outside RTH trading not permitted")]
    service = service_for(tmp_path, state, clock=lambda: AFTER_HOURS_NOW)

    result = service.submit(
        service.preview("Rosa", "ZZZZ", 5, 10.0, session="AFTER_HOURS"),
    )

    assert result["status"] == "REJECTED"
    assert "outside RTH trading not permitted" in result["status_message"]
    assert [trade.order.outsideRth for trade in state.placed] == [True]
    assert [account for account, _message in state.notifications] == ["Rosa"]
    assert "盤後 DAY" in state.notifications[0][1]


def test_extended_order_lifecycle_keeps_session_and_other_sources_untouched(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state, clock=lambda: AFTER_HOURS_NOW)
    submitted = service.submit(
        service.preview("Taylor", "ZZZZ", 4, 10.0, session="AFTER_HOURS"),
    )
    trade = state.placed[0]
    strategy_trade = NS(
        contract=NS(symbol="YYYY", secType="STK", currency="USD", conId=654321),
        order=NS(account="U001", orderRef="STRATEGY:REBALANCE", orderId=77,
                 permId=7077, outsideRth=True),
        orderStatus=NS(status="Submitted", filled=0, avgFillPrice=0.0),
        log=[],
    )
    state.open_trades.append(strategy_trade)

    trade.orderStatus.filled = 1
    trade.orderStatus.avgFillPrice = 9.99
    service.refresh_status()
    active = service.list_orders(active_only=True)
    assert [(row["status"], row["session"], row["filled_quantity"]) for row in active] == [
        ("PARTIAL", "AFTER_HOURS", 1),
    ]
    # Another-source extended-hours order in the same Account is neither
    # tracked nor treated as this tool's duplicate lock.
    assert service.preview(
        "Taylor", "YYYY", 1, 10.0, session="AFTER_HOURS",
    ).symbol == "YYYY"

    state.open_trades.remove(trade)
    trade.orderStatus.status = "Expired"
    state.completed_trades.append(trade)
    service.refresh_status()

    row = service.list_orders()[0]
    assert (row["order_ref"], row["status"], row["session"]) == (
        submitted["order_ref"], "EXPIRED", "AFTER_HOURS",
    )
    assert len(service.list_orders()) == 1
    assert state.cancelled_orders == []
    assert strategy_trade.orderStatus.status == "Submitted"
    [(account, message)] = state.notifications
    assert account == "Taylor"
    assert "LMT $10.00｜盤後 DAY" in message
    assert "已失效" in message
    assert "1/4" in message


def test_cancel_extended_order_waits_for_ib_and_reports_session(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state, clock=lambda: PRE_MARKET_NOW)
    submitted = service.submit(
        service.preview("Yvonne", "ZZZZ", 2, 10.0, session="PRE_MARKET"),
    )
    pending = service.cancel(submitted["order_ref"])
    assert pending["status"] == "CANCEL_PENDING"
    assert state.notifications == []
    state.placed[0].orderStatus.status = "Cancelled"
    service.refresh_status()
    assert service.list_orders()[0]["status"] == "CANCELLED"
    assert "盤前 DAY" in state.notifications[0][1]


def test_unknown_state_alert_names_extended_session(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state, clock=lambda: AFTER_HOURS_NOW)
    service.submit(service.preview("Taylor", "ZZZZ", 1, 10.0, session="AFTER_HOURS"))
    state.open_trades.clear()
    service.refresh_status()
    assert "需要確認" in state.notifications[0][1]
    assert "盤後" in state.notifications[0][1]


def test_existing_manual_order_store_migrates_to_extended_hours_columns(tmp_path):
    path = tmp_path / "manual.sqlite3"
    store = ManualOrderStore(path)
    preview = service_for(tmp_path, FakeIBState()).preview("Taylor", "ZZZZ", 1, 10.0)
    store.create(preview, "QUANTCORE_MANUAL:TAYLOR:legacy")
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE manual_orders DROP COLUMN session")
        conn.execute("ALTER TABLE manual_orders DROP COLUMN forced_cancel_reason")
        conn.execute("ALTER TABLE manual_orders DROP COLUMN outside_rth_confirmed_at")
        conn.execute("DELETE FROM manual_order_schema_migrations WHERE version IN (4, 5, 6)")
    store = ManualOrderStore(path)
    with store._connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(manual_orders)")}
        assert {"session", "forced_cancel_reason", "outside_rth_confirmed_at"} <= columns
        assert conn.execute(
            "SELECT COUNT(*) FROM manual_order_schema_migrations WHERE version IN (4, 5, 6)"
        ).fetchone()[0] == 3
    legacy = store.get("QUANTCORE_MANUAL:TAYLOR:legacy")
    assert (legacy["session"], legacy["outside_rth"], legacy["forced_cancel_reason"],
            legacy["outside_rth_confirmed_at"]) == ("REGULAR", 0, None, None)


def test_cash_target_uses_exact_decimal_floor_and_rejects_less_than_one_share():
    assert whole_share_quantity(amount_usd="100", limit_price="30") == 3
    assert whole_share_quantity(amount_usd="0.30", limit_price="0.10") == 3
    assert whole_share_quantity(amount_usd="0.299999999999999999", limit_price="0.10") == 2
    with pytest.raises(ValueError, match="不足一股"):
        whole_share_quantity(amount_usd="29.999999999999999999", limit_price="30")


def test_cash_preview_shows_target_conversion_and_sends_shares_only(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state)

    preview = service.preview(
        "Taylor", "ZZZZ", None, 30.0, amount_usd="100.00",
    )
    assert preview.quantity == 3
    assert str(preview.amount_usd) == "100.00"
    assert "輸入美元金額：$100.00（預估交易目標）" in preview.confirmation_text
    assert "換算整股股數：3" in preview.confirmation_text
    assert "預估委託金額：$90.0" in preview.confirmation_text
    assert "非成交金額上限" in preview.confirmation_text
    assert "方向：買進" in preview.confirmation_text
    assert "限價：$30.0" in preview.confirmation_text

    service.validate_preview(preview)
    service.submit(preview)
    order = state.placed[0].order
    assert (order.action, order.account, order.totalQuantity, order.lmtPrice) == (
        "BUY", "U001", 3, 30.0,
    )
    assert isinstance(order.totalQuantity, int)
    assert order.cashQty == "UNSET"


def test_cash_mode_rejects_ambiguous_inputs_and_invalid_amounts(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state)
    for quantity, amount in ((3, "100"), (None, None)):
        with pytest.raises(ValueError, match="擇一"):
            service.preview("Taylor", "ZZZZ", quantity, 30.0, amount_usd=amount)
    for amount in ("0", "-1", "NaN", "Infinity", "bad"):
        with pytest.raises(ValueError, match="美元金額"):
            service.preview("Taylor", "ZZZZ", None, 30.0, amount_usd=amount)
    assert state.placed == []


def test_equity_percent_preview_uses_net_liquidation_and_rounds_down(tmp_path):
    state = FakeIBState()
    state.net_liquidation["U002"] = "999999"
    service = service_for(tmp_path, state)

    preview = service.preview("Taylor", "ZZZZ", None, 300.0, equity_pct="30")
    # 16000 x 30% = 4800 -> 4800 / 300 = 16 shares
    assert (preview.quantity, preview.amount_usd) == (16, Decimal("4800.00"))
    assert (preview.account_equity, preview.equity_pct) == (Decimal("16000"), Decimal("30"))
    assert "帳戶權益（IB 淨清算值）：$16,000.00 × 30% = $4,800.00" in preview.confirmation_text
    assert "換算整股股數：16" in preview.confirmation_text
    assert state.summary_requests == ["U001"]

    # 16000 x 25% = 4000 -> 4000 / 301 = 13.29 -> 13 shares
    assert service.preview("Taylor", "ZZZZ", None, 301.0, equity_pct="25").quantity == 13

    # Equity moves after the preview: the fixed amount still validates and submits.
    state.net_liquidation["U001"] = "20000"
    checked = service.validate_preview(preview)
    assert checked.quantity == 16
    assert "× 30% = $4,800.00" in checked.confirmation_text
    service.submit(preview)
    order = state.placed[0].order
    assert (order.action, order.account, order.totalQuantity, order.lmtPrice) == (
        "BUY", "U001", 16, 300.0,
    )


def test_equity_percent_market_order_converts_with_ib_quote(tmp_path):
    state = FakeIBState()
    service = service_for(
        tmp_path, state, clock=lambda: MKT_NOW,
        quote_reader=lambda _ib, _contract, side: BrokerQuote(300, MKT_NOW, side),
    )
    preview = service.preview("Taylor", "ZZZZ", None, None, equity_pct="50",
                              order_type="MKT")
    assert (preview.quantity, preview.amount_usd) == (26, Decimal("8000.00"))
    assert preview.conversion_price == 300


def test_equity_percent_rejects_bad_input_and_missing_equity(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state)
    for pct in ("0", "-5", "100.01", "NaN", "abc", ""):
        with pytest.raises(ValueError, match="權益百分比"):
            service.preview("Taylor", "ZZZZ", None, 30.0, equity_pct=pct)
    with pytest.raises(ValueError, match="擇一"):
        service.preview("Taylor", "ZZZZ", 3, 30.0, equity_pct="30")
    with pytest.raises(ValueError, match="擇一"):
        service.preview("Taylor", "ZZZZ", None, 30.0, amount_usd="100", equity_pct="30")
    assert service.preview("Taylor", "ZZZZ", None, 30.0, equity_pct="100%").quantity == 533

    del state.net_liquidation["U001"]
    with pytest.raises(ValueError, match="NetLiquidation"):
        service.preview("Taylor", "ZZZZ", None, 30.0, equity_pct="30")
    state.net_liquidation["U001"] = "0"
    with pytest.raises(ValueError, match="NetLiquidation"):
        service.preview("Taylor", "ZZZZ", None, 30.0, equity_pct="30")
    # Too little equity for one share is the normal cash-mode error.
    state.net_liquidation["U001"] = "50"
    with pytest.raises(ValueError, match="不足一股"):
        service.preview("Taylor", "ZZZZ", None, 30.0, equity_pct="25")
    assert state.placed == []


@pytest.mark.parametrize(
    "action,allow_short,position,expected_action,expected_position,direction",
    [
        ("BUY", False, 0, "BUY", 2, "買進"),
        ("SELL", False, 3, "SELL", 1, "賣出"),
        ("SELL", True, 1, "SELL", -1, "放空"),
        ("COVER", False, -3, "BUY", -1, "回補空單"),
    ],
)
def test_cash_amount_uses_same_position_rules_for_every_direction(
    tmp_path, action, allow_short, position, expected_action, expected_position,
    direction,
):
    state = FakeIBState()
    state.positions_by_account["U001"]["ZZZZ"] = position
    service = service_for(tmp_path, state)
    preview = service.preview(
        "Taylor", "ZZZZ", None, 20.0, amount_usd="45",
        action=action, allow_short=allow_short,
    )
    assert (preview.quantity, preview.projected_position) == (2, expected_position)
    assert f"方向：{direction}" in preview.confirmation_text
    service.submit(preview)
    order = state.placed[0].order
    assert (order.action, order.account, order.totalQuantity, order.lmtPrice) == (
        expected_action, "U001", 2, 20.0,
    )
    assert order.cashQty == "UNSET"


def test_cash_sell_and_cover_reject_account_specific_position_overages(tmp_path):
    state = FakeIBState()
    state.positions_by_account["U001"]["ZZZZ"] = 3
    state.positions_by_account["U002"]["ZZZZ"] = 1
    state.positions_by_account["U003"]["ZZZZ"] = -2
    service = service_for(tmp_path, state)
    taylor = service.preview(
        "Taylor", "ZZZZ", None, 20.0, amount_usd="60", action="SELL",
    )
    with pytest.raises(ValueError, match="可賣持倉"):
        service.preview("Rosa", "ZZZZ", None, 20.0, amount_usd="60", action="SELL")
    with pytest.raises(ValueError, match="空單"):
        service.preview("Yvonne", "ZZZZ", None, 20.0, amount_usd="60", action="COVER")
    rosa = service.preview(
        "Rosa", "ZZZZ", None, 20.0, amount_usd="20", action="SELL",
    )
    cover = service.preview(
        "Yvonne", "ZZZZ", None, 20.0, amount_usd="40", action="COVER",
    )
    service.submit(taylor)
    service.submit(rosa)
    service.submit(cover)
    assert [(trade.order.action, trade.order.account, trade.order.totalQuantity)
            for trade in state.placed] == [
                ("SELL", "U001", 3), ("SELL", "U002", 1), ("BUY", "U003", 2),
            ]


def test_cash_preview_rechecks_amount_price_position_and_open_order(tmp_path):
    state = FakeIBState()
    state.positions_by_account["U001"]["ZZZZ"] = 4
    service = service_for(tmp_path, state)
    preview = service.preview(
        "Taylor", "ZZZZ", None, 20.0, amount_usd="60",
        action="SELL",
    )
    with pytest.raises(ValueError, match="預覽已變更"):
        service.submit(replace(preview, amount_usd=Decimal("39")))
    with pytest.raises(ValueError, match="預覽已變更"):
        service.submit(replace(preview, limit_price=31.0))
    state.positions_by_account["U001"]["ZZZZ"] = 3
    with pytest.raises(ValueError, match="預覽後變更"):
        service.validate_preview(preview)
    state.positions_by_account["U001"]["ZZZZ"] = 4
    service.store.create(preview, "QUANTCORE_MANUAL:TAYLOR:cash-other")
    with pytest.raises(ValueError, match="未結案手動單"):
        service.submit(preview)
    assert state.placed == []


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


def test_sell_requires_whole_share_long_position_and_short_opt_in(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state)
    with pytest.raises(ValueError, match="可賣持倉"):
        service.preview("Taylor", "ZZZZ", 1, 10.0, action="SELL")
    state.positions_by_account["U001"]["ZZZZ"] = 3.5
    with pytest.raises(ValueError, match="可賣持倉"):
        service.preview("Taylor", "ZZZZ", 4, 10.0, action="SELL")
    sell = service.preview("Taylor", "ZZZZ", 2, 10.0, action="SELL")
    assert (sell.current_position, sell.projected_position) == (3.5, 1.5)
    assert "方向：賣出" in sell.confirmation_text
    assert "預計持倉：1.5" in sell.confirmation_text
    result = service.submit(sell)
    assert result["action"] == "SELL"
    assert (state.placed[0].order.action, state.placed[0].order.account,
            state.placed[0].order.totalQuantity) == ("SELL", "U001", 2)


def test_short_sale_requires_checkbox_and_previews_negative_position(tmp_path):
    state = FakeIBState()
    state.positions_by_account["U001"]["ZZZZ"] = 2
    service = service_for(tmp_path, state)
    with pytest.raises(ValueError, match="允許放空"):
        service.preview("Taylor", "ZZZZ", 3, 10.0, action="SELL")
    short = service.preview(
        "Taylor", "ZZZZ", 3, 10.0, action="SELL", allow_short=True,
    )
    assert (short.current_position, short.projected_position) == (2, -1)
    assert "方向：放空" in short.confirmation_text
    assert "預計空單：1" in short.confirmation_text
    result = service.submit(short)
    assert result["action"] == "SELL_SHORT"
    assert state.placed[0].order.action == "SELL"


def test_cover_is_separate_buy_capped_at_whole_short_position(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state)
    with pytest.raises(ValueError, match="空單"):
        service.preview("Taylor", "ZZZZ", 1, 10.0, action="COVER")
    state.positions_by_account["U001"]["ZZZZ"] = -2.5
    with pytest.raises(ValueError, match="空單"):
        service.preview("Taylor", "ZZZZ", 3, 10.0, action="COVER")
    cover = service.preview("Taylor", "ZZZZ", 2, 10.0, action="COVER")
    assert (cover.current_position, cover.projected_position) == (-2.5, -0.5)
    assert "方向：回補空單" in cover.confirmation_text
    result = service.submit(cover)
    assert result["action"] == "COVER"
    assert (state.placed[0].order.action, state.placed[0].order.account,
            state.placed[0].order.totalQuantity) == ("BUY", "U001", 2)


def test_position_revalidated_before_confirmation_and_submission(tmp_path):
    state = FakeIBState()
    state.positions_by_account["U001"]["ZZZZ"] = 4
    service = service_for(tmp_path, state)
    preview = service.preview("Taylor", "ZZZZ", 2, 10.0, action="SELL")
    state.positions_by_account["U001"]["ZZZZ"] = 3
    with pytest.raises(ValueError, match="預覽後變更"):
        service.validate_preview(preview)
    with pytest.raises(ValueError, match="預覽後變更"):
        service.submit(preview)
    assert state.placed == []


def test_confirmation_rechecks_universe_and_manual_order_lock(tmp_path):
    state = FakeIBState()
    universe = {"SPY"}
    service = service_for(tmp_path, state, universe=universe)
    preview = service.preview("Taylor", "ZZZZ", 1, 10.0)
    universe.add("ZZZZ")
    with pytest.raises(ValueError, match="策略 Universe"):
        service.validate_preview(preview)
    universe.remove("ZZZZ")
    service.store.create(preview, "QUANTCORE_MANUAL:TAYLOR:external")
    with pytest.raises(ValueError, match="未結案手動單"):
        service.validate_preview(preview)
    assert state.placed == []


def test_sell_rechecks_reduced_position_at_submission(tmp_path):
    state = FakeIBState()
    state.positions_by_account["U001"]["ZZZZ"] = 5
    service = service_for(tmp_path, state)
    preview = service.preview("Taylor", "ZZZZ", 4, 10.0, action="SELL")
    state.positions_by_account["U001"]["ZZZZ"] = 2
    with pytest.raises(ValueError, match="允許放空"):
        service.submit(preview)
    assert state.placed == []


def test_three_accounts_use_only_their_own_positions_for_sell_and_cover(tmp_path):
    state = FakeIBState()
    state.positions_by_account["U001"]["ZZZZ"] = 3
    state.positions_by_account["U002"]["ZZZZ"] = -2
    service = service_for(tmp_path, state)
    taylor = service.preview("Taylor", "ZZZZ", 3, 10.0, action="SELL")
    rosa = service.preview("Rosa", "ZZZZ", 2, 10.0, action="COVER")
    with pytest.raises(ValueError, match="可賣持倉"):
        service.preview("Yvonne", "ZZZZ", 1, 10.0, action="SELL")
    with pytest.raises(ValueError, match="空單"):
        service.preview("Taylor", "ZZZZ", 1, 10.0, action="COVER")
    service.submit(taylor)
    service.submit(rosa)
    assert [(t.order.action, t.order.account) for t in state.placed] == [
        ("SELL", "U001"), ("BUY", "U002"),
    ]


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


# --- #105 IBKR Overnight manual LMT orders -------------------------------

OVERNIGHT_NOW = datetime(2026, 9, 15, 1, 0, tzinfo=timezone.utc)  # Mon 21:00 ET


def test_overnight_lmt_is_routed_to_overnight_with_day_tif(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state, clock=lambda: OVERNIGHT_NOW)

    preview = service.preview("Taylor", "ZZZZ", 5, 10.25, session="OVERNIGHT")

    text = preview.confirmation_text
    assert "交易時段：隔夜" in text
    assert "限價：$10.25" in text
    assert "不進入白天時段" in text
    result = service.submit(preview)

    trade = state.placed[0]
    assert (trade.contract.exchange, trade.contract.primaryExchange) == ("OVERNIGHT", "NASDAQ")
    assert state.sent_tifs == ["DAY"]
    assert (trade.order.orderType, trade.order.lmtPrice, trade.order.outsideRth,
            trade.order.account, trade.order.totalQuantity) == ("LMT", 10.25, False, "U001", 5)
    assert (result["session"], result["status"]) == ("OVERNIGHT", "ACCEPTED")
    assert state.cancelled_orders == []


@pytest.mark.parametrize("amount", [None, "100"])
def test_overnight_rejects_mkt_before_contacting_ib(tmp_path, amount):
    state = FakeIBState()
    service = service_for(tmp_path, state, clock=lambda: OVERNIGHT_NOW)
    with pytest.raises(ValueError, match="只接受 LMT"):
        service.preview("Taylor", "ZZZZ", None if amount else 5, None,
                        amount_usd=amount, order_type="MKT", session="OVERNIGHT")
    assert state.client_ids == []


def test_overnight_requires_ib_to_list_the_symbol_on_overnight(tmp_path):
    state = FakeIBState()
    state.valid_exchanges = "SMART,NASDAQ,ARCA"
    service = service_for(tmp_path, state, clock=lambda: OVERNIGHT_NOW)
    with pytest.raises(ValueError, match="隔夜可交易"):
        service.preview("Taylor", "ZZZZ", 5, 10.0, session="OVERNIGHT")
    assert state.placed == []


@pytest.mark.parametrize("now,hours", [
    (datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc), OVERNIGHT_TRADING_HOURS),  # 16:00 ET
    (datetime(2026, 9, 15, 7, 49, 30, tzinfo=timezone.utc), OVERNIGHT_TRADING_HOURS),  # 03:49:30 ET
    (OVERNIGHT_NOW, "20260914:CLOSED;20260915:2000-20260916:0350"),
    (OVERNIGHT_NOW, None),
    (OVERNIGHT_NOW, "not-ib-hours"),
])
def test_overnight_needs_ib_overnight_hours_to_cover_now_and_the_last_minute(
    tmp_path, now, hours,
):
    state = FakeIBState()
    state.overnight_hours = hours
    service = service_for(tmp_path, state, clock=lambda: now)
    with pytest.raises(ValueError, match="不會改用其他時段"):
        service.preview("Taylor", "ZZZZ", 5, 10.0, session="OVERNIGHT")
    assert state.placed == []


def test_sunday_overnight_session_follows_ib_hours_not_a_fixed_clock(tmp_path):
    state = FakeIBState()
    sunday_1900_et = datetime(2026, 9, 13, 23, 0, tzinfo=timezone.utc)
    service = service_for(tmp_path, state, clock=lambda: sunday_1900_et)
    service.submit(service.preview("Taylor", "ZZZZ", 1, 10.0, session="OVERNIGHT"))
    assert state.placed[0].contract.exchange == "OVERNIGHT"


def test_overnight_cash_amount_floors_by_manual_limit_with_position_rules(tmp_path):
    state = FakeIBState()
    state.positions_by_account["U001"]["ZZZZ"] = 2
    service = service_for(tmp_path, state, clock=lambda: OVERNIGHT_NOW)
    with pytest.raises(ValueError, match="允許放空"):
        service.preview("Taylor", "ZZZZ", None, 20.0, amount_usd="65",
                        action="SELL", session="OVERNIGHT")
    preview = service.preview("Taylor", "ZZZZ", None, 20.0, amount_usd="45",
                              action="SELL", session="OVERNIGHT")
    assert (preview.quantity, preview.projected_position) == (2, 0)
    service.submit(preview)
    assert (state.placed[0].order.action, state.placed[0].order.totalQuantity) == ("SELL", 2)


def test_gateway_direct_routing_precaution_rejection_is_explicit_and_final(tmp_path):
    # Paper, 2026-09-29: with the redirect precaution on, IB answered 10329
    # then 201 and ib_insync marked the order Cancelled.
    state = FakeIBState()
    state.next_status = "Cancelled"
    state.next_log = [
        NS(message="Error 10329, reqId 60: This order will be directly routed to OVERNIGHT.",
           errorCode=10329),
        NS(message="Error 201, reqId 60: Order rejected - reason:", errorCode=201),
    ]
    service = service_for(tmp_path, state, clock=lambda: OVERNIGHT_NOW)

    result = service.submit(service.preview("Rosa", "ZZZZ", 1, 10.0, session="OVERNIGHT"))

    assert result["status"] == "REJECTED"
    assert "股票API委託單跳過重新傳遞委託單警告" in result["status_message"]
    assert len(state.placed) == 1
    assert [account for account, _message in state.notifications] == ["Rosa"]

    # Another active Rosa order makes reconciliation read IB again. A fresh
    # connection's completed-order record says only Cancelled, without the
    # rejection log; that must not downgrade the rejection.
    service.submit(service.preview("Rosa", "YYYY", 1, 10.0, session="OVERNIGHT"))
    rejected = state.placed[0]
    rejected.log = []
    state.open_trades.remove(rejected)
    state.completed_trades.append(rejected)
    service.refresh_status()
    assert service.store.get(result["order_ref"])["status"] == "REJECTED"
    assert len(state.notifications) == 1


def test_reconcile_cancels_overnight_order_ib_records_without_overnight_tif(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state, clock=lambda: OVERNIGHT_NOW)
    kept = service.submit(service.preview("Taylor", "ZZZZ", 1, 10.0, session="OVERNIGHT"))
    service.refresh_status()                     # IB record: tif=OVERNIGHT, exch=OVERNIGHT
    assert state.cancelled_orders == []

    changed = service.submit(service.preview("Rosa", "ZZZZ", 1, 10.0, session="OVERNIGHT"))
    state.placed[1].order.tif = "DAY"            # IB would keep it past the session
    service.refresh_status()

    assert state.cancelled_orders == [state.placed[1].order]
    row = service.store.get(changed["order_ref"])
    assert row["status"] == "CANCEL_PENDING"
    assert "tif=DAY" in row["forced_cancel_reason"]
    assert service.store.get(kept["order_ref"])["status"] == "ACCEPTED"


def test_overnight_notification_and_lifecycle_name_the_session(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state, clock=lambda: OVERNIGHT_NOW)
    submitted = service.submit(
        service.preview("Yvonne", "ZZZZ", 2, 10.0, session="OVERNIGHT"),
    )
    trade = state.placed[0]
    trade.orderStatus.filled = 1
    trade.orderStatus.avgFillPrice = 9.98
    service.refresh_status()
    assert service.list_orders()[0]["status"] == "PARTIAL"

    trade.orderStatus.status = "Cancelled"   # IB ends an unfilled Overnight order
    service.refresh_status()
    row = service.list_orders()[0]
    assert (row["order_ref"], row["status"], row["session"]) == (
        submitted["order_ref"], "CANCELLED", "OVERNIGHT",
    )
    [(account, message)] = state.notifications
    assert account == "Yvonne"
    assert "LMT $10.00｜隔夜\n" in message
    assert "DAY" not in message
    assert "1/2" in message


# --- IB expiry vs trader cancel (completedStatus) -------------------------

@pytest.mark.parametrize("session,clock,label", [
    ("REGULAR", MKT_NOW, "一般時段 DAY"),
    ("AFTER_HOURS", AFTER_HOURS_NOW, "盤後 DAY"),
    ("OVERNIGHT", OVERNIGHT_NOW, "隔夜"),
])
def test_ib_expiry_is_shown_as_expired_not_cancelled(tmp_path, session, clock, label):
    # Paper, 2026-09-28/29: IB ended unfilled DAY and Overnight orders with
    # status Cancelled and completedStatus "Expired (5)".
    state = FakeIBState()
    service = service_for(tmp_path, state, clock=lambda: clock)
    submitted = service.submit(
        service.preview("Taylor", "ZZZZ", 4, 10.0, session=session),
    )
    trade = state.open_trades.pop()
    trade.orderStatus.status = "Cancelled"
    trade.orderStatus.filled = 1
    trade.completed_status = "Expired (5)"
    state.completed_trades.append(trade)

    service.refresh_status()

    row = service.store.get(submitted["order_ref"])
    assert (row["status"], row["filled_quantity"]) == ("EXPIRED", 1)
    assert service.list_orders()[0]["status_label"] == "已失效"
    [(_account, message)] = state.notifications
    assert "結果：已失效" in message
    assert label in message
    assert "1/4" in message


def test_trader_cancel_reported_on_completed_order_stays_cancelled(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state)
    submitted = service.submit(service.preview("Taylor", "ZZZZ", 2, 8.25))
    trade = state.open_trades.pop()
    trade.orderStatus.status = "Cancelled"
    trade.completed_status = "Cancelled by Trader"
    state.completed_trades.append(trade)

    service.refresh_status()

    assert service.store.get(submitted["order_ref"])["status"] == "CANCELLED"
    assert "結果：已取消" in state.notifications[0][1]


def test_reading_completed_orders_leaves_ib_insync_wrapper_unchanged(tmp_path):
    state = FakeIBState()
    service = service_for(tmp_path, state)
    service.submit(service.preview("Taylor", "ZZZZ", 2, 8.25))
    trade = state.open_trades.pop()
    trade.orderStatus.status = "Cancelled"
    trade.completed_status = "Expired (5)"
    state.completed_trades.append(trade)

    service.refresh_status()

    assert state.ib_instances
    for ib in state.ib_instances:
        assert "completedOrder" not in vars(ib.wrapper)


# Limit-price deviation warning (#136): compare against the last completed
# regular-session close; warn above 10%, never block.
FRI, MON = date(2026, 9, 11), date(2026, 9, 14)
DAILY_BARS = [(date(2026, 9, 10), 50.0), (FRI, 100.0), (MON, 80.0)]


@pytest.mark.parametrize("session,now,expected_day", [
    ("REGULAR", MKT_NOW, FRI),               # Mon 14:00 ET: Mon's bar is partial
    ("PRE_MARKET", PRE_MARKET_NOW, FRI),
    ("AFTER_HOURS", AFTER_HOURS_NOW, MON),   # Mon has closed
    ("OVERNIGHT", OVERNIGHT_NOW, MON),
])
def test_limit_price_is_compared_with_last_completed_close(
    tmp_path, session, now, expected_day,
):
    state = FakeIBState()
    state.daily_bars = DAILY_BARS
    service = service_for(tmp_path, state, clock=lambda: now)

    preview = service.preview("Taylor", "ZZZZ", 5, 120, session=session)

    close = dict(DAILY_BARS)[expected_day]
    assert (preview.prior_close_date, preview.prior_close) == (expected_day, close)
    percent = abs(120 / close - 1) * 100
    assert preview.confirmation_text.startswith(
        f"⚠ 價格警示：限價 $120.0 比 {expected_day:%Y-%m-%d} 收盤 ${close:g} "
        f"高 {percent:.1f}%\n"
    )
    # Closes come from the SMART listing, never the OVERNIGHT route.
    assert [exchange for exchange, _ in state.history_requests] == ["SMART"]
    assert state.history_requests[0][1]["useRTH"] is True
    # A warning never blocks the order.
    service.validate_preview(preview)
    service.submit(preview)
    assert state.placed[0].order.lmtPrice == 120


@pytest.mark.parametrize("limit,warned,word", [
    (110, False, None), (90, False, None),       # exactly 10%: no warning
    (110.01, True, "高"), (89.99, True, "低"),
    (105, False, None),
])
def test_price_warning_threshold_is_strictly_above_ten_percent(
    tmp_path, limit, warned, word,
):
    state = FakeIBState()
    state.daily_bars = DAILY_BARS
    service = service_for(tmp_path, state, clock=lambda: MKT_NOW)

    preview = service.preview("Taylor", "ZZZZ", 5, limit)

    assert bool(preview.price_warning) is warned
    assert ("價格警示" in preview.confirmation_text) is warned
    if warned:
        assert f"收盤 $100 {word} " in preview.price_warning


@pytest.mark.parametrize("bars,error", [
    ([], None),                                  # no data / no permission
    ([(date(2026, 9, 10), 100.0)], None),        # stale: expected Fri missing
    ([(FRI, float("nan"))], None),
    ([(FRI, 0.0)], None),
    (DAILY_BARS, TimeoutError("historical data timeout")),
])
def test_missing_close_says_unchecked_and_still_sends(tmp_path, bars, error):
    state = FakeIBState()
    state.daily_bars = bars
    state.history_error = error
    service = service_for(tmp_path, state, clock=lambda: MKT_NOW)

    preview = service.preview("Taylor", "ZZZZ", 5, 500)

    assert preview.prior_close is None
    assert preview.confirmation_text.startswith("⚠ 無法取得前一日收盤價，未檢查價格偏離\n")
    service.validate_preview(preview)
    service.submit(preview)
    assert len(state.placed) == 1


def test_cash_limit_order_is_also_price_checked(tmp_path):
    state = FakeIBState()
    state.daily_bars = DAILY_BARS
    service = service_for(tmp_path, state, clock=lambda: MKT_NOW)

    preview = service.preview("Taylor", "ZZZZ", None, 50, amount_usd="500")

    assert preview.quantity == 10
    assert "比 2026-09-11 收盤 $100 低 50.0%" in preview.confirmation_text


def test_market_orders_are_not_price_checked(tmp_path):
    state = FakeIBState()
    state.daily_bars = DAILY_BARS
    service = service_for(
        tmp_path, state, clock=lambda: MKT_NOW,
        quote_reader=lambda _ib, _contract, side: BrokerQuote(300, MKT_NOW, side),
    )

    for amount, quantity in (("900", None), (None, 5)):
        preview = service.preview("Taylor", "ZZZZ", quantity, None,
                                  amount_usd=amount, order_type="MKT")
        assert preview.price_warning == ""
        assert "收盤" not in preview.confirmation_text
    assert state.history_requests == []


def test_changed_close_does_not_invalidate_preview_or_get_reread_at_submit(tmp_path):
    state = FakeIBState()
    state.daily_bars = DAILY_BARS
    service = service_for(tmp_path, state, clock=lambda: MKT_NOW)
    preview = service.preview("Taylor", "ZZZZ", 5, 120)
    assert len(state.history_requests) == 1

    state.daily_bars = [(FRI, 119.0)]
    fresh = service.validate_preview(preview)
    assert fresh.price_warning == ""             # confirmation shows the latest close
    state.history_error = TimeoutError("down")
    service.submit(preview)

    assert len(state.history_requests) == 2      # preview + validate, not submit
    assert state.placed[0].order.lmtPrice == 120


# ── Status vocabulary (#190) ───────────────────────────────────────────

def test_unique_index_locks_exactly_the_active_statuses(tmp_path):
    """The Account/symbol lock lives in migration 1; it must match ACTIVE_STATUSES."""
    path = tmp_path / "manual_orders.sqlite3"
    ManualOrderStore(path)
    with sqlite3.connect(path) as conn:
        (sql,) = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='uq_manual_active_account_symbol'"
        ).fetchone()
    in_list = re.search(r"status IN \(([^)]*)\)", sql).group(1)
    assert set(re.findall(r"'([A-Z_]+)'", in_list)) == set(manual_orders.ACTIVE_STATUSES)


def test_every_status_is_either_active_or_terminal():
    active = set(manual_orders.ACTIVE_STATUSES)
    terminal = set(manual_orders.TERMINAL_STATUSES)
    assert not active & terminal
    assert active | terminal == set(manual_orders._STATUS_LABELS)
    assert set(manual_orders.UNCONFIRMED_STATUSES) == {
        "CANCEL_UNKNOWN", "IB_UNKNOWN", "SUBMISSION_UNKNOWN"}


# ── Read-only fills query (#190) ───────────────────────────────────────

def test_read_account_fills_missing_store_means_no_manual_orders(tmp_path):
    path = tmp_path / "manual_orders.sqlite3"
    assert manual_orders.read_account_fills("Rosa", path) == []
    assert not path.exists()


def test_read_account_fills_returns_one_accounts_orders_without_writing(tmp_path):
    """Read-only and migration-free: a pre-migration store is read as-is, byte for byte."""
    path = tmp_path / "manual_orders.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE manual_orders (
                   order_ref TEXT PRIMARY KEY, account_name TEXT NOT NULL,
                   symbol TEXT NOT NULL, action TEXT NOT NULL,
                   filled_quantity INTEGER NOT NULL DEFAULT 0,
                   status TEXT NOT NULL, created_at TEXT NOT NULL)""")
        conn.executemany(
            "INSERT INTO manual_orders VALUES (?, ?, ?, ?, ?, ?, ?)",
            [("M:2", "Rosa", "XYZ", "SELL_SHORT", 3, "PARTIAL", "2026-09-28T00:00:02"),
             ("M:1", "Rosa", "BTC", "BUY", 5, "FILLED", "2026-09-28T00:00:01"),
             ("M:3", "Taylor", "BTC", "BUY", 7, "FILLED", "2026-09-28T00:00:00")])
    conn.close()
    before = path.read_bytes()

    fills = manual_orders.read_account_fills("Rosa", path)

    assert fills == [
        {"order_ref": "M:1", "symbol": "BTC", "action": "BUY",
         "filled_quantity": 5, "status": "FILLED"},
        {"order_ref": "M:2", "symbol": "XYZ", "action": "SELL_SHORT",
         "filled_quantity": 3, "status": "PARTIAL"},
    ]
    assert path.read_bytes() == before
