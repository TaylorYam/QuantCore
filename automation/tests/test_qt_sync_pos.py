"""新 Panel 的 Sync Pos（#152，接 paper）。

從主視窗操作，跑真的 sync_position（計算、差額、送單）與 manual_attribution，
只把 IB 換成假的 Broker、訊號換成固定值。驗證：送單閘門擋實盤、paper 可預覽 →
確認 → 送出、碰撞暫停原因顯示、沒人在等的計算結果被丟棄、實盤紀錄不被寫入。
不連 IB、不發 Telegram、不讀真實 config。
"""
from __future__ import annotations

import threading
from types import SimpleNamespace as NS

import pytest

from tests.test_qt_panel import (  # noqa: F401  fixtures
    dialogs, extras, make_window, portfolio, ports, source,
)

from PySide6.QtCore import Qt

from qt_panel.broker_gate import PaperTarget

PAPER = PaperTarget(host="127.0.0.1", port=4013, account="DU001", client_id=92)
UNIVERSE = {"AAPL", "MSFT", "SPY", "SSO"}
JOINED = UNIVERSE | {"XYZ"}          # XYZ 後來被加入 S&P 500


class SyncBroker:
    """假的 IB：記下連線與送單；帳號、持倉、報價由 state 決定。"""

    def __init__(self, state):
        self.state = state
        self.connected = False
        self.client = NS(port=4013)
        state.instances.append(self)

    def connect(self, host, port, *, clientId, timeout):
        self.connected = True
        self.state.connects.append((host, port, clientId))

    def managedAccounts(self):
        return list(self.state.managed)

    def disconnect(self):
        self.connected = False

    def isConnected(self):
        return self.connected

    def openTrades(self):
        return []

    def qualifyContracts(self, *contracts):
        return list(contracts)

    def reqTickers(self, *contracts):
        return [NS(contract=c, bid=99.9, ask=100.1, last=100.0, close=100.0,
                   midpoint=lambda: 100.0) for c in contracts]

    def placeOrder(self, contract, order):
        order.orderId = 500 + len(self.state.placed)
        self.state.placed.append((contract.symbol, order.action, int(order.totalQuantity),
                                  order.orderType, order.account))
        return NS(order=order, contract=contract, fills=[], orderStatus=NS(status="Submitted"))

    def sleep(self, *_):
        pass


@pytest.fixture
def broker():
    return NS(managed=["DU001"], positions={"AAPL": 100.0, "XYZ": 10.0}, instances=[],
              connects=[], placed=[], ctxs=[], persist=[], gate=None)


@pytest.fixture
def sync_env(broker, monkeypatch, tmp_path):
    """固定訊號與 IB 讀數；sync_position、rebalance_plan、manual_attribution 用真的。"""
    import ib_fetcher
    import manual_attribution as ma
    import rebalance_ib
    import signals

    live = NS(ledger=tmp_path / "live_attribution.sqlite3",
              orders=tmp_path / "live_orders.sqlite3")
    monkeypatch.setattr(ma, "DEFAULT_LEDGER_PATH", live.ledger)
    monkeypatch.setattr(ma, "DEFAULT_MANUAL_ORDERS_PATH", live.orders)

    def sr_weights():
        if broker.gate is not None:
            broker.gate.wait(5)
        return {}, "N/A"

    def sp500_weights(holdings, name, persist=True):
        broker.persist.append(persist)
        return {"AAPL": 0.5, "XYZ": 0.5}, "2026-09-29"

    def nav(ib, ctx):
        broker.ctxs.append(ctx)
        return 100_000.0

    monkeypatch.setattr(signals, "get_sr_weights", sr_weights)
    monkeypatch.setattr(signals, "get_sp500_weights", sp500_weights)
    monkeypatch.setattr(signals, "get_sp500_universe", lambda historical=False: set(JOINED))
    monkeypatch.setattr(ib_fetcher, "get_nav", nav)
    monkeypatch.setattr(ib_fetcher, "get_positions", lambda ib, ctx: dict(broker.positions))
    monkeypatch.setattr(ib_fetcher, "get_prices",
                        lambda ib, syms, ctx, **k: {s: 100.0 for s in syms})
    monkeypatch.setattr(rebalance_ib, "check_fills", lambda trades: [])
    return live


def base_ctx(name, role):
    from account_ctx import AccountCtx
    return AccountCtx(
        name=name, account={"Taylor": "U001", "Rosa": "U002", "Yvonne": "U003"}[name],
        host="127.0.0.1", port={"Taylor": 4001, "Rosa": 4002, "Yvonne": 4003}[name],
        client_id=20 + role, dry_run=True, order_type="MOO", outside_rth=False,
        wait_fill_sec=0, alloc_sr=0.0, alloc_sp500=0.5, alloc_skewls=0.0,
        price_feed_port=4002, tg_token="LIVE-TOKEN", tg_chat_id="LIVE-CHAT",
    )


@pytest.fixture
def paper_files(tmp_path):
    from qt_panel.trading import PaperFiles
    return PaperFiles(orders=tmp_path / "paper_orders.sqlite3",
                      attribution=tmp_path / "paper_attribution.sqlite3")


@pytest.fixture
def sync_window(make_window, broker, sync_env, paper_files, qtbot):
    def make(**kw):
        kw.setdefault("paper_target", PAPER)
        kw.setdefault("broker_factory", lambda: SyncBroker(broker))
        kw.setdefault("paper_files", paper_files)
        kw.setdefault("account_ctx_loader", base_ctx)
        kw.setdefault("manual_order_service", lambda **_kw: NS(
            has_active_orders=lambda: False, has_pending_notifications=lambda: False,
            list_orders=lambda **_: []))
        w = make_window(**kw)
        qtbot.mouseClick(w.nav_button("Trading"), Qt.LeftButton)
        qtbot.mouseClick(w.trading.sync_tab, Qt.LeftButton)
        return w
    return make


def compute(qtbot, sync, account="Taylor"):
    sync.account_box.setCurrentText(account)
    qtbot.mouseClick(sync.compute_button, Qt.LeftButton)
    qtbot.waitUntil(lambda: not sync.computing(), timeout=5000)


def wait_sent(qtbot, sync):
    qtbot.waitUntil(lambda: not sync.sending(), timeout=5000)


# ── 送單閘門 ─────────────────────────────────────────────────────────

def test_live_gateway_on_the_paper_port_is_refused_before_anything_is_computed(
        sync_window, broker, qtbot):
    broker.managed = ["U001"]
    w = sync_window()
    sync = w.trading.sync
    compute(qtbot, sync)
    assert "送單閘門拒絕" in sync.status_text() and "U001" in sync.status_text()
    assert not sync.confirm_button.isVisible()
    assert broker.placed == [] and broker.ctxs == []


def test_gateway_turning_live_after_the_preview_blocks_the_send(
        sync_window, broker, dialogs, qtbot):
    w = sync_window()
    sync = w.trading.sync
    compute(qtbot, sync)
    assert sync.confirm_button.isEnabled()
    broker.managed = ["U001"]
    qtbot.mouseClick(sync.confirm_button, Qt.LeftButton)
    wait_sent(qtbot, sync)
    assert broker.placed == []
    qtbot.waitUntil(lambda: "送單閘門拒絕" in w.dashboard.log_text(), timeout=2000)


# ── paper：計算 → 預覽 → 確認 → 送出 ─────────────────────────────────

def test_paper_sync_previews_then_sends_limit_orders_to_the_paper_account(
        sync_window, broker, dialogs, qtbot):
    broker.positions = {"AAPL": 100.0}
    w = sync_window()
    sync = w.trading.sync
    assert [sync.account_box.itemText(i) for i in range(sync.account_box.count())] == [
        "Taylor", "Rosa", "Yvonne"]
    compute(qtbot, sync)

    ctx = broker.ctxs[0]
    assert (ctx.name, ctx.account, ctx.port, ctx.client_id) == ("Taylor", "DU001", 4013, 93)
    assert ctx.price_feed_port is None and ctx.dry_run is False and ctx.tg_token == ""
    assert broker.persist == [False]                    # SP500 state 只讀不寫
    assert "NAV: $100,000" in sync.subtitle_text()
    assert sync.trade_table() == [
        ("BUY", "AAPL", "150", "100", "250", "$100.00", "$15,000", "SP500"),
        ("BUY", "XYZ", "250", "0", "250", "$100.00", "$25,000", "SP500"),
    ]
    assert "Required Trades" in w.dashboard.log_text()  # 預覽摘要寫進 Output Log
    assert broker.placed == []

    qtbot.mouseClick(sync.confirm_button, Qt.LeftButton)
    wait_sent(qtbot, sync)
    [(title, prompt, _warning)] = dialogs.confirms
    assert "確定送出 2 筆限價單到 [Taylor]" in prompt
    assert "paper 帳號 DU001" in prompt
    assert broker.placed == [("AAPL", "BUY", 150, "LMT", "DU001"),
                             ("XYZ", "BUY", 250, "LMT", "DU001")]
    assert all(c[1] == 4013 for c in broker.connects)
    qtbot.waitUntil(lambda: "Placed 2 orders" in w.dashboard.log_text(), timeout=2000)
    assert not sync.confirm_button.isVisible()           # 同一份預覽不能再送一次


def test_declining_the_confirm_dialog_sends_nothing(sync_window, broker, dialogs, qtbot):
    w = sync_window()
    sync = w.trading.sync
    compute(qtbot, sync)
    dialogs.confirm_answers = [False]
    qtbot.mouseClick(sync.confirm_button, Qt.LeftButton)
    assert broker.placed == []
    assert sync.confirm_button.isEnabled()


def test_already_on_target_offers_nothing_to_send(sync_window, broker, qtbot):
    broker.positions = {"AAPL": 250.0, "XYZ": 250.0}
    w = sync_window()
    sync = w.trading.sync
    compute(qtbot, sync)
    assert "Already on target" in sync.preview_text()
    assert not sync.confirm_button.isVisible()


# ── 碰撞暫停 ─────────────────────────────────────────────────────────

def test_paused_symbols_show_their_reason_and_are_not_sent(
        sync_window, broker, paper_files, sync_env, qtbot):
    import manual_attribution as ma

    # paper 帳戶先有策略外的 XYZ，之後 XYZ 被加入 S&P 500 → 暫停
    ma.evaluate("Taylor", {"AAPL": 100.0, "XYZ": 10.0}, UNIVERSE,
                ledger_path=paper_files.attribution, manual_orders_path=paper_files.orders)
    w = sync_window()
    sync = w.trading.sync
    compute(qtbot, sync)
    text = sync.preview_text()
    assert "手動持倉暫停" in text and "XYZ" in text and "手動持倉 10 股" in text
    assert [row[1] for row in sync.trade_table()] == ["AAPL"]
    qtbot.waitUntil(lambda: "XYZ 策略單暫停" in w.dashboard.log_text(), timeout=2000)
    assert not sync_env.ledger.exists()                # 實盤歸屬紀錄沒被建立或寫入


# ── 沒人在等的結果 ───────────────────────────────────────────────────

def test_discarded_computation_is_dropped_and_never_sent(sync_window, broker, qtbot):
    broker.gate = threading.Event()
    w = sync_window()
    sync = w.trading.sync
    sync.account_box.setCurrentText("Taylor")
    qtbot.mouseClick(sync.compute_button, Qt.LeftButton)
    assert sync.computing()
    qtbot.mouseClick(sync.discard_button, Qt.LeftButton)
    assert not sync.computing()
    broker.gate.set()
    qtbot.wait(300)
    assert sync.trade_table() == []
    assert not sync.confirm_button.isVisible()
    assert broker.placed == []


def test_shutdown_drops_a_running_computation(sync_window, broker, qtbot):
    broker.gate = threading.Event()
    w = sync_window()
    sync = w.trading.sync
    qtbot.mouseClick(sync.compute_button, Qt.LeftButton)
    w.shutdown()
    broker.gate.set()
    qtbot.wait(300)
    assert sync.trade_table() == [] and broker.placed == []


def test_without_a_paper_target_there_is_no_sync(make_window):
    w = make_window(paper_problem="尚未設定 paper 連線")
    assert w.trading.sync is None
