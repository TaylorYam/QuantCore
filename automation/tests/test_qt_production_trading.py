"""新 Panel 正式模式的實盤送單（#154）。

正式模式下手動單、Sync Pos、接管送到所選帳戶的實盤 Gateway，帳戶身分（clientId role）、
紀錄與 Telegram 都和 Tk Panel 一樣。重點：手動單用 Tk 原本的 ROLE_MANUAL_ORDER 與同一個
紀錄檔，切換前 Tk 送出的單，切換後新 Panel 照樣認得成交。全部用假的 IB 與 Telegram，
紀錄檔都在 tmp_path。
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from tests.test_qt_panel import (  # noqa: F401  fixtures
    dialogs, extras, make_window, portfolio, ports, source,
)
from tests.test_qt_sync_pos import (  # noqa: F401  fixtures
    SyncBroker, base_ctx, broker, paper_files, sync_env,
)
from tests.test_qt_takeover import TakeoverBroker, takeover_env  # noqa: F401
from tests.test_qt_trading import (  # noqa: F401
    NOW, FakeSymbols, click_and_wait, fill, order_factory, wait_refreshed,
)
from tests.test_manual_orders import FakeIB, FakeIBState

from PySide6.QtCore import Qt

from account_utils import ROLE_MANUAL_ORDER, ROLE_REBALANCE, ROLE_TAKEOVER
from manual_orders import ManualOrderService, ManualOrderStore
from qt_panel.mode import PanelMode

UNIVERSE = {"AAPL", "MSFT", "SPY", "SSO"}


def live_ctx(name, role):
    """實盤帳戶身分：Taylor U001／4001、clientId = 20 + role（同 account_utils 的配法）。"""
    return replace(base_ctx(name, role), dry_run=False)


@pytest.fixture
def telegram(monkeypatch):
    import notify
    sent = []
    monkeypatch.setattr(notify, "tg_send",
                        lambda msg, **dest: sent.append((msg, dest)) or True)
    return sent


@pytest.fixture
def prod(make_window, qtbot):
    def make(**kw):
        from qt_panel.watch import WatchPorts
        from tests.test_qt_production import WatchSource

        kw.setdefault("mode", PanelMode.PRODUCTION)
        kw.setdefault("gateway_source", WatchSource())
        kw.setdefault("watch_ports", WatchPorts(telegram=lambda m: None,
                                                cold_connect=lambda n: None,
                                                sleep=lambda s: None))
        kw.setdefault("account_ctx_loader", live_ctx)
        kw.setdefault("symbol_loader", FakeSymbols)
        w = make_window(**kw)
        qtbot.mouseClick(w.nav_button("Trading"), Qt.LeftButton)
        return w
    return make


def test_trading_banner_says_orders_go_to_live_accounts(prod, tmp_path):
    w = prod(manual_order_service=lambda **kw: _store_service(tmp_path, FakeIBState(), **kw))
    text = w.page_text("Trading")
    assert "正式模式：手動單、Sync Pos、接管送到實盤帳戶" in text
    assert "paper" not in w.trading.banner_title.text()


# ── 手動單：Tk 的身分與紀錄 ──────────────────────────────────────────

def _store_service(tmp_path, state, **kw):
    kw.setdefault("ib_factory", lambda: FakeIB(state))
    kw.setdefault("notifier", lambda *a: True)
    return ManualOrderService(
        account_names=["Taylor", "Rosa", "Yvonne"],
        store=ManualOrderStore(tmp_path / "manual_orders.sqlite3"),
        universe_loader=lambda: {"SPY", "SSO"}, order_factory=order_factory,
        clock=lambda: NOW, **kw)


@pytest.fixture
def ib_live():
    state = FakeIBState()
    state.managed = ["U001"]
    state.positions_by_account = {"U001": {}}
    state.net_liquidation = {"U001": "20000"}
    from datetime import date
    state.daily_bars = [(date(2026, 9, 28), 100.0)]
    return state


def test_manual_orders_use_the_tk_identity_store_and_telegram(
        prod, ib_live, tmp_path, dialogs, qtbot):
    from qt_panel.live_trading import account_telegram

    built = []

    def factory(**kw):
        built.append(kw)
        return _store_service(tmp_path, ib_live, **kw)

    # 切換前 Tk Panel 送出的單（Tk 的 service：同一個紀錄檔、同一個 clientId role）
    tk = _store_service(tmp_path, ib_live, account_ctx_factory=live_ctx)
    tk_order = tk.submit(tk.preview("Taylor", "ZZZZ", 4, 100.0))
    tk_trade = ib_live.open_trades.pop()

    w = prod(manual_order_service=factory, broker_factory=lambda: FakeIB(ib_live))
    [kw] = built
    assert kw["notifier"] is account_telegram                 # 結案通知發該帳戶 Telegram
    assert kw["account_ctx_factory"]("Taylor", ROLE_MANUAL_ORDER).client_id == 26

    panel = w.trading.manual
    fill(panel)
    click_and_wait(qtbot, panel, panel.preview_button)
    click_and_wait(qtbot, panel, panel.confirm_button)
    [trade] = ib_live.placed[1:]
    assert (trade.order.account, trade.order.action, trade.order.totalQuantity) == (
        "U001", "BUY", 5)
    assert set(ib_live.client_ids) == {26}                    # 和 Tk 同一個手動單 clientId
    wait_refreshed(qtbot, panel)

    # Tk 那張單切換後成交：新 Panel 用同一個 clientId 認得它
    tk_trade.orderStatus.status = "Filled"
    tk_trade.orderStatus.filled = 4
    tk_trade.orderStatus.avgFillPrice = 99.0
    ib_live.completed_trades.append(tk_trade)
    ib_live.fills.append(NS(contract=tk_trade.contract, execution=NS(
        acctNumber="U001", permId=tk_trade.order.permId, orderId=tk_trade.order.orderId,
        clientId=26, shares=4.0, price=99.0, execId="tk-fill", orderRef=tk_order["order_ref"])))
    click_and_wait(qtbot, panel, panel.refresh_button)
    wait_refreshed(qtbot, panel)
    service = w.trading.manual.service
    row = next(r for r in service.list_orders() if r["order_ref"] == tk_order["order_ref"])
    assert (row["status"], row["filled_quantity"]) == ("FILLED", 4)


def test_manual_order_tracking_runs_in_the_background_like_tk(prod, tmp_path):
    started = []
    service = NS(has_active_orders=lambda: False, has_pending_notifications=lambda: False,
                 list_orders=lambda **_: [], start_monitor=lambda: started.append("start"),
                 stop_monitor=lambda: started.append("stop"))
    w = prod(manual_order_service=lambda **kw: service)
    assert started == ["start"]
    w.shutdown()
    assert started == ["start", "stop"]


def test_parallel_mode_does_not_start_background_tracking(make_window, paper_files):
    from qt_panel.broker_gate import PaperTarget
    started = []
    service = NS(has_active_orders=lambda: False, has_pending_notifications=lambda: False,
                 list_orders=lambda **_: [], start_monitor=lambda: started.append("start"))
    make_window(paper_target=PaperTarget("127.0.0.1", 4013, "DU001", 92),
                manual_order_service=lambda **kw: service, paper_files=paper_files)
    assert started == []


# ── Sync Pos：實盤 ───────────────────────────────────────────────────

def _no_manual_service(**_kw):
    return NS(has_active_orders=lambda: False, has_pending_notifications=lambda: False,
              list_orders=lambda **_: [])


def test_live_sync_uses_the_rebalance_identity_and_reports_like_tk(
        prod, broker, sync_env, telegram, monkeypatch, dialogs, qtbot):
    import excel_logger

    rows = []
    monkeypatch.setattr(excel_logger, "append_trade_log", lambda *a, **k: rows.append(k))
    broker.managed = ["U001"]
    broker.positions = {"AAPL": 100.0}
    w = prod(broker_factory=lambda: SyncBroker(broker), manual_order_service=_no_manual_service)
    qtbot.mouseClick(w.trading.sync_tab, Qt.LeftButton)
    sync = w.trading.sync
    sync.account_box.setCurrentText("Taylor")
    qtbot.mouseClick(sync.compute_button, Qt.LeftButton)
    qtbot.waitUntil(lambda: not sync.computing(), timeout=5000)

    ctx = broker.ctxs[0]
    assert (ctx.account, ctx.port, ctx.client_id) == ("U001", 4001, 20 + ROLE_REBALANCE)
    assert broker.persist == [True]                       # 實盤 SP500 state 照 Tk 寫回
    qtbot.mouseClick(sync.confirm_button, Qt.LeftButton)
    qtbot.waitUntil(lambda: not sync.sending(), timeout=5000)
    [(_title, prompt, _warning)] = dialogs.confirms
    assert "送往：實盤帳戶" in prompt
    assert broker.placed == [("AAPL", "BUY", 150, "LMT", "U001"),
                             ("XYZ", "BUY", 250, "LMT", "U001")]
    assert [r["account_name"] for r in rows] == ["Taylor", "Taylor"]
    [(msg, dest)] = telegram
    assert "[Taylor] Sync Position — LMT" in msg
    assert dest == {"token": "LIVE-TOKEN", "chat_id": "LIVE-CHAT"}
    assert "限價單（實盤）" in sync.status_text()


# ── 交給策略接管：實盤 ───────────────────────────────────────────────

def test_live_takeover_uses_the_takeover_identity_and_telegram(
        prod, broker, takeover_env, telegram, dialogs, qtbot):
    import manual_attribution as ma

    ma.evaluate("Taylor", {"AAPL": 100.0, "XYZ": 10.0}, UNIVERSE)          # 實盤紀錄檔
    ma.evaluate("Taylor", {"AAPL": 100.0, "XYZ": 10.0}, UNIVERSE | {"XYZ"})
    broker.managed = ["U001"]
    w = prod(broker_factory=lambda: TakeoverBroker(broker),
             manual_order_service=_no_manual_service)
    qtbot.mouseClick(w.trading.takeover_tab, Qt.LeftButton)
    panel = w.trading.takeover
    assert panel.symbols() == ["XYZ"]
    qtbot.mouseClick(panel.compute_button, Qt.LeftButton)
    qtbot.waitUntil(lambda: not panel.computing(), timeout=5000)
    ctx = broker.ctxs[0]
    assert (ctx.account, ctx.client_id) == ("U001", 20 + ROLE_TAKEOVER)

    qtbot.mouseClick(panel.confirm_button, Qt.LeftButton)
    qtbot.waitUntil(lambda: not panel.sending(), timeout=5000)
    assert broker.placed == [("XYZ", "BUY", 240, "LMT", "U001")]
    assert "✅ 接管完成" in panel.status_text()
    assert "XYZ" not in ma.paused_symbols("Taylor")
    msgs = [m for m, dest in telegram if dest == {"token": "LIVE-TOKEN", "chat_id": "LIVE-CHAT"}]
    assert any("XYZ 已交給策略接管" in m for m in msgs)
    assert "[手動持倉·實盤] [Taylor] ✅ XYZ 已交給策略接管" in w.dashboard.log_text()
