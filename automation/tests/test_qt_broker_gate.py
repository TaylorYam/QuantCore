"""新 Panel 的送單閘門與 paper 連線設定（#151，ADR-0012）。

閘門包在 Broker 外面（#185），是新 Panel 所有送單路徑共用的連線入口：連上 Gateway
後先讀它管理的帳號，並行模式下不是全部 paper（DU/DF 開頭）就斷線拒絕，呼叫端
拿不到連線。IB 換成 tests/fake_broker.py 的 in-memory fake。不需要 PySide6。
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from qt_panel.broker_gate import (
    PAPER_CONFIG_KEY, PaperTarget, SendGate, SendRefused, load_paper_target,
)
from qt_panel.mode import PanelMode
from tests.fake_broker import FakeBroker

CTX = NS(name="Taylor", account="DU111", host="127.0.0.1", port=4013, client_id=92)


def gate_for(managed, mode=PanelMode.PARALLEL):
    fake = FakeBroker(managed=managed)
    return SendGate(mode, fake), fake


# ── 閘門 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("managed", [["DU111"], ["DF222"], ["du333", "DU444"]])
def test_parallel_mode_lets_paper_only_gateways_through(managed):
    gate, fake = gate_for(managed)
    conn = gate.connect(CTX)
    trade = conn.place(conn.contract("SPY"), NS(action="BUY", totalQuantity=1,
                                                orderType="LMT", lmtPrice=1.0))
    conn.cancel(trade.order)
    assert [p.symbol for p in fake.placed] == ["SPY"] and fake.cancelled == [trade.order]
    assert fake.connects == [("127.0.0.1", 4013, 92)] and fake.refused == 0


@pytest.mark.parametrize("managed", [["U123"], ["DU111", "U123"], []])
def test_parallel_mode_disconnects_and_refuses_live_or_unknown_gateways(managed):
    gate, fake = gate_for(managed)
    with pytest.raises(SendRefused) as exc:
        gate.connect(CTX)
    assert fake.refused == 1 and fake.closed == 1      # 已斷線，呼叫端拿不到連線
    assert fake.placed == [] and fake.cancelled == []
    text = str(exc.value)
    assert "並行模式" in text and "paper" in text and "沒有送出任何單" in text


def test_refusal_names_the_live_account_so_the_trader_can_find_the_wrong_port():
    gate, _fake = gate_for(["U123"])
    with pytest.raises(SendRefused, match="U123"):
        gate.connect(CTX)


def test_production_mode_lets_live_gateways_through():
    gate, fake = gate_for(["U123"], PanelMode.PRODUCTION)
    assert gate.connect(CTX).managed_accounts() == ["U123"]
    assert fake.refused == 0


def test_every_connection_gets_its_own_check():
    gate, fake = gate_for(["DU111"])
    gate.connect(CTX)
    fake.managed = ["U123"]                  # 同一個 port 換成實盤 Gateway
    with pytest.raises(SendRefused):
        gate.connect(CTX)


def test_the_gate_passes_the_timeout_and_wraps_the_real_broker_by_default():
    import broker
    gate = SendGate(PanelMode.PARALLEL)
    assert isinstance(gate._broker, broker.Broker)
    seen = []

    class Spy:
        def connect(self, ctx, *, timeout, check):
            seen.append((ctx, timeout))
            check(["DU1"])
            return "conn"

    assert SendGate(PanelMode.PARALLEL, Spy()).connect(CTX, timeout=10) == "conn"
    assert seen == [(CTX, 10)]


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

