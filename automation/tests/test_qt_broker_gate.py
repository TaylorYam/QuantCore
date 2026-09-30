"""新 Panel 的送單閘門與 paper 連線設定（#151，ADR-0012）。

閘門是新 Panel 所有送單路徑共用的連線工廠：連上 Gateway 後先讀它管理的帳號，
並行模式下不是全部 paper（DU/DF 開頭）就斷線拒絕。不需要 PySide6。
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from qt_panel.broker_gate import (
    PAPER_CONFIG_KEY, PaperTarget, SendGate, SendRefused, load_paper_target,
)
from qt_panel.mode import PanelMode


class FakeBroker:
    def __init__(self, managed):
        self.managed = list(managed)
        self.connected = False
        self.calls = []
        self.errorEvent = NS(name="errorEvent")

    def connect(self, host, port, *, clientId, timeout):
        self.connected = True
        self.calls.append(("connect", host, port, clientId))

    def managedAccounts(self):
        return self.managed

    def disconnect(self):
        self.connected = False
        self.calls.append(("disconnect",))

    def placeOrder(self, contract, order):
        self.calls.append(("placeOrder", contract, order))
        return "trade"

    def cancelOrder(self, order):
        self.calls.append(("cancelOrder", order))
        return "cancelled"

    def openTrades(self):
        return ["t"]


def gated(managed, mode=PanelMode.PARALLEL):
    raw = FakeBroker(managed)
    return SendGate(mode, lambda: raw).broker(), raw


# ── 閘門 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("managed", [["DU111"], ["DF222"], ["du333", "DU444"]])
def test_parallel_mode_lets_paper_only_gateways_through(managed):
    ib, raw = gated(managed)
    ib.connect("127.0.0.1", 4013, clientId=92, timeout=10)
    assert ib.placeOrder("c", "o") == "trade"
    assert ib.cancelOrder("o") == "cancelled"
    assert ib.openTrades() == ["t"]
    assert ib.errorEvent is raw.errorEvent
    assert raw.connected


@pytest.mark.parametrize("managed", [["U123"], ["DU111", "U123"], []])
def test_parallel_mode_disconnects_and_refuses_live_or_unknown_gateways(managed):
    ib, raw = gated(managed)
    with pytest.raises(SendRefused) as exc:
        ib.connect("127.0.0.1", 4001, clientId=92, timeout=10)
    assert not raw.connected
    assert raw.calls[-1] == ("disconnect",)
    text = str(exc.value)
    assert "並行模式" in text and "paper" in text and "沒有送出任何單" in text


def test_refusal_names_the_live_account_so_the_trader_can_find_the_wrong_port():
    ib, _raw = gated(["U123"])
    with pytest.raises(SendRefused, match="U123"):
        ib.connect("127.0.0.1", 4001, clientId=92, timeout=10)


def test_order_calls_recheck_the_gateway_and_never_reach_a_live_account():
    ib, raw = gated(["DU111"])
    ib.connect("127.0.0.1", 4013, clientId=92, timeout=10)
    raw.managed = ["U123"]          # 同一連線後來回報實盤帳號
    with pytest.raises(SendRefused):
        ib.placeOrder("c", "o")
    with pytest.raises(SendRefused):
        ib.cancelOrder("o")
    assert not any(call[0] in ("placeOrder", "cancelOrder") for call in raw.calls)


def test_order_calls_before_a_checked_connection_are_refused():
    ib, raw = gated(["DU111"])
    with pytest.raises(SendRefused):
        ib.placeOrder("c", "o")
    assert raw.calls == []


def test_every_connection_gets_its_own_check():
    raws = [FakeBroker(["DU111"]), FakeBroker(["U123"])]
    gate = SendGate(PanelMode.PARALLEL, lambda: raws.pop(0))
    first, second = gate.broker(), gate.broker()
    first.connect("h", 1, clientId=1, timeout=1)
    with pytest.raises(SendRefused):
        second.connect("h", 1, clientId=1, timeout=1)


# ── paper 連線設定（config.json 的 qt_panel_paper）───────────────────

ACCOUNTS = [{"name": "Taylor", "port": 4001}, {"name": "Rosa", "port": 4002},
            {"name": "Yvonne", "port": 4003}]


def test_paper_target_reads_the_config_section_with_defaults():
    target, problem = load_paper_target(
        {PAPER_CONFIG_KEY: {"port": 4013, "account": "dur217314"}}, ACCOUNTS)
    assert problem == ""
    assert target == PaperTarget(host="127.0.0.1", port=4013, account="DUR217314",
                                 client_id=target.client_id)
    assert target.client_id not in (91,)       # paper_verify_manual_orders 的預設


def test_paper_target_accepts_host_and_client_id():
    target, _ = load_paper_target(
        {PAPER_CONFIG_KEY: {"host": "10.0.0.5", "port": "4013", "account": "DF1",
                            "client_id": 55}}, ACCOUNTS)
    assert (target.host, target.port, target.client_id) == ("10.0.0.5", 4013, 55)


@pytest.mark.parametrize("section, words", [
    (None, ["尚未設定", PAPER_CONFIG_KEY]),
    ({"port": 4013}, ["account"]),
    ({"port": 4013, "account": "U17800779"}, ["DU/DF"]),
    ({"account": "DU1"}, ["port"]),
    ({"port": "abc", "account": "DU1"}, ["port"]),
    ({"port": 4002, "account": "DU1"}, ["4002", "Rosa", "實盤"]),
    ({"port": 4013, "account": "DU1", "client_id": "x"}, ["client_id"]),
    ("4013", [PAPER_CONFIG_KEY]),
])
def test_bad_or_missing_paper_config_explains_what_to_fix(section, words):
    config = {} if section is None else {PAPER_CONFIG_KEY: section}
    target, problem = load_paper_target(config, ACCOUNTS)
    assert target is None
    for word in words:
        assert word in problem


def test_paper_target_maps_every_account_name_to_the_paper_account():
    target, _ = load_paper_target(
        {PAPER_CONFIG_KEY: {"port": 4013, "account": "DU1"}}, ACCOUNTS)
    ctx = target.account_ctx("Rosa", 6)
    assert (ctx.name, ctx.account, ctx.host, ctx.port, ctx.client_id) == (
        "Rosa", "DU1", "127.0.0.1", 4013, target.client_id)
