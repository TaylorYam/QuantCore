"""Broker（券商連線層，#185）：clientId 規則、ib_insync adapter 與 in-memory fake。

clientId：現有號碼逐一對照不變（手動單認成交靠 clientId 比對）；不認得的帳戶報錯。
adapter：用一個只會記錄呼叫的假 ib_insync.IB 驗證連線參數、送單閘門的 check、
連不上／被拒時收掉 socket。fake：和 adapter 同一個 interface。不連真的 IB。
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace as NS

import pytest

import broker
from broker import (
    ROLE_MANUAL_ORDER, ROLE_MONITOR, ROLE_MOO_CHECK, ROLE_PANEL, ROLE_PANEL_READONLY,
    ROLE_REBALANCE, ROLE_REPORT, ROLE_TAKEOVER, Broker, Connection, UnknownAccount,
)
from tests.fake_broker import FakeBroker, FakeConnection

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
    ROLE_PANEL_READONLY: (29, 39, 49),
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
    with pytest.raises(UnknownAccount):
        broker.panel_query_client_id("Nobody", accounts=ACCOUNTS)


def test_client_id_reads_accounts_json_when_not_given(fake_config):
    assert broker.client_id("Yvonne", ROLE_TAKEOVER) == 48


def test_roles_are_distinct_and_fit_one_account_block():
    roles = list(EXISTING_IDS)
    assert len(roles) == len(set(roles))
    assert all(0 <= r < broker._CLIENT_ID_STEP for r in roles)


def test_panel_queries_sit_outside_every_account_role():
    """新 Panel 查權益／持倉（#149）：心跳已佔 ROLE_PANEL_READONLY，查詢另用 69/79/89。"""
    ids = [broker.panel_query_client_id(a["name"], accounts=ACCOUNTS) for a in ACCOUNTS]
    assert ids == [69, 79, 89]
    taken = {i for row in EXISTING_IDS.values() for i in row}
    assert not taken & set(ids)


def test_panel_query_ids_refuse_a_layout_that_would_collide():
    with pytest.raises(ValueError):
        broker.panel_query_client_id("A0", accounts=[{"name": f"A{i}"} for i in range(5)])


def test_price_feed_client_ids():
    assert broker.price_feed_client_id(25, readonly=True) == 1025
    assert broker.price_feed_client_id(69, readonly=True) == 1069
    assert broker.price_feed_client_id(20, readonly=False) == 99


def test_paper_client_ids():
    assert broker.PAPER_CLIENT_ID == 92 and broker.PAPER_VERIFY_CLIENT_ID == 91
    assert broker.paper_client_id(92, ROLE_MANUAL_ORDER) == 92
    assert broker.paper_client_id(92, ROLE_REBALANCE) == 93      # Sync Pos
    assert broker.paper_client_id(92, ROLE_TAKEOVER) == 94
    with pytest.raises(ValueError):
        broker.paper_client_id(92, ROLE_REPORT)


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
    assert isinstance(conn, Connection) and conn.ctx is CTX and conn.ib is ib


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


def test_check_sees_the_managed_accounts_and_a_refusal_disconnects():
    ib = RecordingIB(managed=["U1", " U2 "])
    seen = []

    def refuse(accounts):
        seen.append(accounts)
        raise PermissionError("no")

    with pytest.raises(PermissionError):
        Broker(lambda: ib).connect(CTX, check=refuse)
    assert seen == [["U1", "U2"]]
    assert ib.calls[-1] == ("disconnect",) and not ib.connected


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


def test_scheduled_entries_raw_ib_is_wrapped_and_a_connection_passes_through():
    ib = RecordingIB()
    wrapped = broker.as_connection(ib, CTX)
    assert isinstance(wrapped, Connection) and wrapped.ib is ib
    conn = Broker(lambda: ib).connect(CTX)
    assert broker.as_connection(conn, CTX) is conn
    fake = FakeBroker().connect(CTX)
    assert broker.as_connection(fake, CTX) is fake


# ── in-memory fake 和 adapter 同一個 interface ─────────────────────

def _public(cls):
    return {name for name, _ in inspect.getmembers(cls) if not name.startswith("_")}


def test_fake_connection_offers_the_same_interface_as_the_adapter():
    # portfolio_readings 只給 Portfolio 頁用，第三階段會換成統一的權益讀法；
    # fake 不模仿它的多層 fallback（那是 adapter 自己的測試：test_ib_portfolio_query）
    assert _public(Connection) - {"portfolio_readings"} <= _public(FakeConnection)


def test_fake_broker_check_refusal_closes_and_counts():
    fake = FakeBroker(managed=["U9"])

    def refuse(accounts):
        raise PermissionError(accounts)

    with pytest.raises(PermissionError):
        fake.connect(CTX, check=refuse)
    assert fake.refused == 1 and fake.closed == 1 and fake.connects == [("127.0.0.1", 4001, 26)]
