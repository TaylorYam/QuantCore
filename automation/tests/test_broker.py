"""Broker（券商連線層，#185）：clientId 規則、ib_insync adapter 與 in-memory fake。

clientId：現有號碼逐一對照不變（手動單認成交靠 clientId 比對）；不認得的帳戶報錯。
adapter：用一個只會記錄呼叫的假 ib_insync.IB 驗證連線參數、
連不上時收掉 socket。fake：和 adapter 同一個 interface。不連真的 IB。
"""
from __future__ import annotations

import inspect
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

import broker
from broker import (
    ROLE_MANUAL_ORDER, ROLE_MONITOR, ROLE_MOO_CHECK, ROLE_PANEL,
    ROLE_REBALANCE, ROLE_REPORT, ROLE_TAKEOVER, Broker, Connection, UnknownAccount,
)
from tests.fake_broker import FakeConnection

ACCOUNTS = [{"name": "Taylor"}, {"name": "Rosa"}, {"name": "Yvonne"}]


# ── clientId ─────────────────────────────────────────────────────────

# #185 之前散在 account_utils、送單閘門、ib_fetcher、paper 驗證腳本的號碼，一個都不能變
EXISTING_IDS = {
    ROLE_REBALANCE: (20, 30, 40),
    ROLE_REPORT: (21, 31, 41),
    ROLE_MOO_CHECK: (22, 32, 42),
    ROLE_PANEL: (25, 35, 45),
    ROLE_MANUAL_ORDER: (26, 36, 46),
    ROLE_MONITOR: (27, 37, 47),
    ROLE_TAKEOVER: (28, 38, 48),
}


@pytest.mark.parametrize("role", sorted(EXISTING_IDS))
def test_every_existing_client_id_is_unchanged(role):
    ids = tuple(broker.client_id(a["name"], role, accounts=ACCOUNTS) for a in ACCOUNTS)
    assert ids == EXISTING_IDS[role]


def test_account_names_match_case_insensitively():
    assert broker.client_id("rosa", ROLE_MANUAL_ORDER, accounts=ACCOUNTS) == 36


def test_unknown_account_is_an_error_not_taylors_client_ids():
    with pytest.raises(UnknownAccount, match="Nobody"):
        broker.client_id("Nobody", ROLE_MANUAL_ORDER, accounts=ACCOUNTS)


def test_client_id_reads_accounts_json_when_not_given(fake_config):
    assert broker.client_id("Yvonne", ROLE_TAKEOVER) == 48


def test_roles_are_distinct_and_fit_one_account_block():
    roles = list(EXISTING_IDS)
    assert len(roles) == len(set(roles))
    assert all(0 <= r < broker._CLIENT_ID_STEP for r in roles)


def test_price_feed_client_ids():
    assert broker.price_feed_client_id(25, readonly=True) == 1025
    assert broker.price_feed_client_id(69, readonly=True) == 1069
    assert broker.price_feed_client_id(20, readonly=False) == 99


def test_paper_verify_client_id():
    assert broker.PAPER_VERIFY_CLIENT_ID == 91


def test_account_ctx_takes_its_client_id_from_the_broker_rule(fake_config):
    from account_ctx import AccountCtx
    assert AccountCtx.from_name("Rosa", ROLE_TAKEOVER).client_id == 38


# ── ib_insync adapter ───────────────────────────────────────────────

class RecordingIB:
    """假 ib_insync.IB：只記錄呼叫。"""

    def __init__(self, managed=("U1",), fail_connect=None):
        self.calls = []
        self.managed = list(managed)
        self.fail_connect = fail_connect
        self.connected = False
        self.RequestTimeout = 0

    def connect(self, host, port, **kw):
        self.calls.append(("connect", host, port, kw))
        if self.fail_connect:
            raise self.fail_connect
        self.connected = True

    def managedAccounts(self):
        return self.managed

    def isConnected(self):
        return self.connected

    def disconnect(self):
        self.calls.append(("disconnect",))
        self.connected = False

    def openTrades(self):
        return ["mine"]

    def reqAllOpenOrders(self):
        return ["mine", "tws"]

    def cancelOrder(self, order):
        self.calls.append(("cancel", order))

    def qualifyContracts(self, *contracts):
        self.calls.append(("qualify",) + tuple(c.symbol for c in contracts))
        return list(contracts)

    def placeOrder(self, contract, order):
        self.calls.append(("place", contract.symbol, order))
        return "trade"

    def sleep(self, seconds):
        self.calls.append(("sleep", seconds))

    def reqTickers(self, *contracts):
        return [NS(contract=c, bid=10.0, ask=float("nan"), last=0.0, close=9.5,
                   midpoint=lambda: float("nan")) for c in contracts]


CTX = NS(name="Taylor", account="U1", host="127.0.0.1", port=4001, client_id=26)


def test_connect_passes_the_ctx_identity():
    ib = RecordingIB()
    conn = Broker(lambda: ib).connect(CTX, timeout=10)
    assert ib.calls == [("connect", "127.0.0.1", 4001, {"clientId": 26, "timeout": 10})]
    assert isinstance(conn, Connection) and conn.ctx is CTX and conn._ib is ib


def test_readonly_connect_is_bounded_and_names_the_account():
    ib = RecordingIB()
    Broker(lambda: ib).connect(CTX, timeout=15, readonly=True)
    assert ib.calls[0][3] == {"clientId": 26, "timeout": 15, "readonly": True,
                              "account": "U1"}
    assert ib.RequestTimeout == 20


def test_failed_connect_closes_the_socket():
    ib = RecordingIB(fail_connect=TimeoutError("no gateway"))
    with pytest.raises(TimeoutError):
        Broker(lambda: ib).connect(CTX)
    assert ib.calls[-1] == ("disconnect",)


def test_connection_orders_go_to_the_ib_calls():
    ib = RecordingIB()
    conn = Broker(lambda: ib).connect(CTX)
    contract = conn.contract("BRK.B")
    assert contract.symbol == "BRK B" and ("qualify", "BRK B") in ib.calls
    assert conn.place(contract, "order") == "trade"
    conn.cancel("o")
    conn.wait(2)
    assert ib.calls[-3:] == [("place", "BRK B", "order"), ("cancel", "o"), ("sleep", 2)]
    assert conn.open_orders() == ["mine"]
    assert conn.open_orders(all_clients=True) == ["mine", "tws"]


def test_quotes_drop_invalid_values():
    conn = Broker(lambda: RecordingIB()).connect(CTX)
    assert conn.quotes(["XLK"]) == {"XLK": {"bid": 10.0, "ask": None, "mid": None,
                                            "last": None, "close": 9.5}}


def test_close_is_idempotent_and_never_raises():
    ib = RecordingIB()
    conn = Broker(lambda: ib).connect(CTX)
    with conn:
        pass
    conn.close()
    assert ib.calls.count(("disconnect",)) == 1

    ib.disconnect = lambda: (_ for _ in ()).throw(OSError("gone"))
    ib.connected = True
    conn.close()


def test_scheduled_reads_go_to_the_ib_calls():
    ib = RecordingIB()
    ib.trades = lambda: ["moo"]
    ib.fills = lambda: ["fill"]
    conn = Broker(lambda: ib).connect(CTX)
    assert conn.trades() == ["moo"] and conn.fills() == ["fill"]


def test_scheduled_connect_keeps_the_identity_timeout_and_log_line(monkeypatch):
    """排程入口的連線（#208 起直接向 Broker 要）：clientId、timeout 20 秒與連上後那行
    log 都和 #185 之前的 ib_fetcher.connect 相同（21:00 log 逐字不變）。"""
    ib = RecordingIB()
    monkeypatch.setattr(broker, "_default_ib", lambda: ib)
    conn = Broker().connect(CTX, timeout=20)
    assert isinstance(conn, Connection) and conn._ib is ib
    assert ib.calls == [("connect", "127.0.0.1", 4001, {"clientId": 26, "timeout": 20})]
    assert broker.connected_line(CTX) == (
        "Connected  host=127.0.0.1 port=4001 clientId=26 account=U1")


def test_cold_connect_probe_opens_and_closes_through_the_broker(monkeypatch, fake_config):
    from qt_panel.watch import cold_connect
    ib = RecordingIB()
    monkeypatch.setattr(broker, "_default_ib", lambda: ib)
    cold_connect("Rosa")
    assert ib.calls == [("connect", "127.0.0.1", 4002, {"clientId": 37, "timeout": 10}),
                        ("disconnect",)]


def test_acceptance_check_client_ids_stay_clear_of_every_role():
    ids = [broker.acceptance_check_client_id(i) for i in range(3)]
    assert ids == [16025, 16035, 16045]
    assert not {i for row in EXISTING_IDS.values() for i in row} & set(ids)


# ── in-memory fake 和 adapter 同一個 interface ─────────────────────

def _public(cls):
    return {name for name, _ in inspect.getmembers(cls) if not name.startswith("_")}


def test_fake_connection_offers_the_same_interface_as_the_adapter():
    assert _public(Connection) <= _public(FakeConnection)


# ── 讀數與限價統一（#185 第三階段）─────────────────────────────────

def _value(tag, value, currency="USD", account="U1"):
    return NS(account=account, tag=tag, value=str(value), currency=currency, modelCode="")


class AccountIB(RecordingIB):
    """accountValues 照 IB 的形狀：別的帳戶、非 USD 的列也混在裡面。"""

    def __init__(self, rows, **kw):
        super().__init__(**kw)
        self.rows = rows
        self.connected = True

    def accountValues(self, account=""):
        self.calls.append(("accountValues", account))
        return list(self.rows)


NORMAL_ROWS = [
    _value("NetLiquidation", "99999.5", account="U2"),       # 別的帳戶
    _value("NetLiquidation", "7.8", currency="HKD"),          # 非 USD
    _value("NetLiquidation", "38199.96"),
    _value("Cushion", "0.643", currency=""),
    _value("ExcessLiquidity", "24551.2"),
    _value("MaintMarginReq", "13000"),
]


def test_equity_is_the_selected_accounts_net_liquidation_in_usd():
    conn = Connection(AccountIB(NORMAL_ROWS), CTX)
    assert conn.equity() == Decimal("38199.96")
    assert conn.nav() == 38199.96
    assert conn.account_info() == {"nav": 38199.96, "cushion_pct": 64.3,
                                   "excess_liq": 24551.2}


@pytest.mark.parametrize("rows", [
    [],
    [_value("NetLiquidation", "99999.5", account="U2")],      # 只有別的帳戶的
    [_value("NetLiquidation", "7.8", currency="HKD")],         # 只有非 USD 的
    [_value("NetLiquidation", "0")],
    [_value("NetLiquidation", "nan")],
    [_value("NetLiquidation", "")],
])
def test_equity_that_cannot_be_read_is_an_error_never_zero_or_another_account(rows):
    conn = Connection(AccountIB(rows), CTX)
    with pytest.raises(broker.AccountValueUnavailable, match="U1"):
        conn.nav()
    with pytest.raises(broker.AccountValueUnavailable):
        conn.equity()


def test_equity_needs_a_configured_account():
    conn = Connection(AccountIB(NORMAL_ROWS), NS(**{**vars(CTX), "account": ""}))
    with pytest.raises(broker.AccountValueUnavailable):
        conn.nav()


@pytest.mark.parametrize("missing", ["Cushion", "ExcessLiquidity"])
def test_cushion_and_excess_liquidity_follow_the_same_strict_rule(missing):
    rows = [r for r in NORMAL_ROWS if r.tag != missing]
    rows.append(_value(missing, "0.9", account="U2"))           # 別的帳戶的不能拿來用
    conn = Connection(AccountIB(rows), CTX)
    with pytest.raises(broker.AccountValueUnavailable, match=missing):
        conn.account_info()


def test_portfolio_readings_use_the_same_strict_reads():
    ib = AccountIB(NORMAL_ROWS, managed=["U1"])
    ib.positions = lambda account="": [
        NS(account="U1", contract=NS(symbol="BRK B", secType="STK"), position=3.0),
        NS(account="U1", contract=NS(symbol="SPY", secType="OPT"), position=1.0),
    ]
    out = {}
    Connection(ib, CTX, readonly=True).portfolio_readings(out)
    assert (out["account"], out["nav"], out["cushion"], out["excess_liq"]) == (
        "U1", 38199.96, 64.3, 24551.2)
    assert [p.contract.symbol for p in out["positions"]] == ["BRK.B"]


@pytest.mark.parametrize("readonly", [True, False])
def test_portfolio_readings_refuse_a_gateway_without_the_selected_account(readonly):
    conn = Connection(AccountIB(NORMAL_ROWS, managed=["U2"]), CTX, readonly=readonly)
    with pytest.raises(RuntimeError, match="not available"):
        conn.portfolio_readings({})


def test_portfolio_readings_keep_what_was_read_before_a_missing_value():
    rows = [r for r in NORMAL_ROWS if r.tag != "Cushion"]
    out = {}
    with pytest.raises(broker.AccountValueUnavailable):
        Connection(AccountIB(rows, managed=["U1"]), CTX).portfolio_readings(out)
    assert out["nav"] == 38199.96 and "cushion" not in out


def _quote_ib(bid, ask, last=float("nan"), close=float("nan")):
    ib = RecordingIB()
    ib.connected = True

    def tickers(*contracts):
        mid = (bid + ask) / 2 if bid == bid and ask == ask else float("nan")
        return [NS(contract=c, bid=bid, ask=ask, last=last, close=close,
                   midpoint=lambda: mid) for c in contracts]
    ib.reqTickers = tickers
    return ib


def test_crossed_quote_counts_as_no_bid_or_ask(caplog):
    caplog.set_level("WARNING", logger="broker")
    conn = Connection(_quote_ib(10.2, 10.0, last=10.1), CTX)
    assert conn.quotes(["XLK"]) == {"XLK": {"bid": None, "ask": None, "mid": None,
                                            "last": 10.1, "close": None}}
    assert caplog.messages == ["  XLK: crossed quote bid=10.2 > ask=10.0"]


def test_wide_spread_is_logged_but_kept(caplog):
    caplog.set_level("WARNING", logger="broker")
    conn = Connection(_quote_ib(9.0, 10.0), CTX)
    assert conn.quotes(["XLK"])["XLK"]["bid"] == 9.0
    assert caplog.messages == ["  XLK: wide spread bid=9.0 ask=10.0"]


def test_normal_quote_logs_nothing(caplog):
    caplog.set_level("WARNING", logger="broker")
    Connection(_quote_ib(9.99, 10.01), CTX).quotes(["XLK"])
    assert caplog.messages == []


FULL = {"bid": 9.9, "ask": 10.1, "mid": 10.0, "last": 10.05, "close": 9.8}


@pytest.mark.parametrize("action,quote,ref,expected", [
    ("BUY", FULL, 5.0, round(10.1 * 1.003, 2)),
    ("SELL", FULL, 5.0, round(9.9 * 0.997, 2)),
    # 缺 bid／ask：mid → last → close → 呼叫端給的參考價
    ("BUY", {**FULL, "ask": None}, 5.0, round(10.0 * 1.003, 2)),
    ("SELL", {**FULL, "bid": None, "mid": None}, 5.0, round(10.05 * 0.997, 2)),
    ("BUY", {"ask": None, "mid": None, "last": None, "close": 9.8}, 5.0, round(9.8 * 1.003, 2)),
    ("SELL", {}, 5.0, round(5.0 * 0.997, 2)),
    ("BUY", {}, 0, 0.0),
    ("BUY", {}, None, 0.0),
])
def test_limit_price_is_one_rule_with_one_fallback_order(action, quote, ref, expected):
    assert broker.limit_price(action, quote, ref) == expected


# ── 手動單用的 IB 細節（#228）────────────────────────────────────────

class ManualIB(RecordingIB):
    """手動單用到的 ib_insync 呼叫：只記下參數、回給定的結果。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.connected = True
        self.errorEvent = object()
        self.wrapper = NS(completedOrder=lambda *a: None)   # 類別上的 handler 換成實例上的

    def reqContractDetails(self, contract):
        self.calls.append(("details", contract))
        return None

    def positions(self, account=""):
        self.calls.append(("positions", account))
        return ["row"]

    def reqHistoricalData(self, contract, **kw):
        self.calls.append(("history", contract, kw))
        return [NS(date="d1", close=1.5), NS(date="d2", close=2.5)]

    def reqExecutions(self):
        return None


def test_manual_order_reads_go_to_the_ib_calls():
    ib = ManualIB()
    conn = Broker(lambda: ib).connect(CTX)
    contract = NS(symbol="ZZZZ")
    assert conn.qualify(contract) == [contract] and ("qualify", "ZZZZ") in ib.calls
    assert conn.contract_details(contract) == []                # IB 給 None
    assert conn.position_records() == ["row"] and ("positions", "U1") in ib.calls
    assert conn.executions() == []
    assert conn.error_event is ib.errorEvent


def test_daily_closes_ask_ib_for_ten_regular_session_days():
    ib = ManualIB()
    conn = Broker(lambda: ib).connect(CTX)
    contract = NS(symbol="ZZZZ")
    assert conn.daily_closes(contract) == [("d1", 1.5), ("d2", 2.5)]
    [(_, asked, kw)] = [c for c in ib.calls if c[0] == "history"]
    assert asked is contract
    assert kw == {"endDateTime": "", "durationStr": "10 D", "barSizeSetting": "1 day",
                  "whatToShow": "TRADES", "useRTH": True, "formatDate": 1, "timeout": 10}


class _Wrapper:
    """ib_insync 的 Wrapper.completedOrder 把 IB 的紀錄變成 Trade，丟掉 orderState
    （連同 completedStatus）。"""

    def completedOrder(self, contract, order, order_state):
        pass


def test_completed_orders_capture_completed_status_and_leave_the_wrapper_unchanged():
    ib = ManualIB()
    ib.wrapper = _Wrapper()
    expired = NS(contract="c", order=NS(orderRef="QUANTCORE_MANUAL:T:1"))
    cancelled = NS(contract="c", order=NS(orderRef="QUANTCORE_MANUAL:T:2"))

    def completed(apiOnly):
        assert apiOnly is True
        # IB：到期和使用者撤單的 orderState.status 都是 Cancelled，completedStatus 才分得出來
        ib.wrapper.completedOrder("c", expired.order, NS(completedStatus="Expired (5)"))
        ib.wrapper.completedOrder("c", cancelled.order, NS(completedStatus="Cancelled by Trader"))
        return [expired, cancelled]

    ib.reqCompletedOrders = completed
    conn = Broker(lambda: ib).connect(CTX)
    trades, statuses = conn.completed_orders()
    assert trades == [expired, cancelled]
    assert statuses == {"QUANTCORE_MANUAL:T:1": "Expired (5)",
                        "QUANTCORE_MANUAL:T:2": "Cancelled by Trader"}
    assert "completedOrder" not in vars(ib.wrapper)


@pytest.mark.parametrize("market_type,tick_type,accepted", [
    (1, 2, True),
    (3, 67, False),
    (2, 2, False),
])
def test_realtime_quote_requires_realtime_selected_side_tick(market_type, tick_type, accepted):
    from datetime import datetime, timezone

    from ib_insync import Ticker
    from ib_insync.objects import TickData
    from broker import NoRealtimeQuote

    now = datetime(2026, 9, 14, 18, 0, tzinfo=timezone.utc)

    class FakeQuoteIB(RecordingIB):
        def __init__(self):
            super().__init__()
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
            self.ticker.ticks = [TickData(now, tick_type, 30, 10)]
            self.ticker.updateEvent.emit(self.ticker)

        def cancelMktData(self, _contract):
            self.cancelled = True

    ib = FakeQuoteIB()
    conn = Broker(lambda: ib).connect(CTX)
    if accepted:
        result = conn.realtime_quote(NS(symbol="ZZZZ"), "ask")
        assert (result.price, result.side, result.market_data_type) == (30, "ask", 1)
    else:
        with pytest.raises(NoRealtimeQuote, match="明確股數"):
            conn.realtime_quote(NS(symbol="ZZZZ"), "ask")
    assert ib.requested_type == 1
    assert ib.cancelled


def test_manual_orders_and_paper_verify_never_take_the_ib_insync_object():
    """手動單與 paper 驗證只走 Connection 的方法：沒有 `.ib` 可拿（#228）。"""
    import re
    from pathlib import Path

    here = Path(__file__).resolve().parent.parent
    for name in ("manual_orders.py", "paper_verify_manual_orders.py"):
        text = (here / name).read_text(encoding="utf-8")
        assert not re.search(r"[\w)]\.ib\b", text), name
    assert not hasattr(Connection, "ib")
