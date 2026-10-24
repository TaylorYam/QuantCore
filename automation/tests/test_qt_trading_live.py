"""Panel 的 Trading 畫面送實盤（#154、#183）。

Trading 拿到實盤送單目標：畫面寫出「送往：實盤帳戶」、Output Log 標記「實盤」，手動單
的背景對帳隨主視窗啟停。帳戶身分（clientId role）、紀錄、state、Telegram 在
test_qt_order_target 驗證。全部用假的 IB 與 Telegram，紀錄檔都在 tmp_path。
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from tests.test_qt_panel import (  # noqa: F401  fixtures
    dialogs, extras, make_window, portfolio, ports, source,
)
from tests.test_qt_order_target import (  # noqa: F401  fixtures
    broker, live_ctx, live_files, pause_xyz, sent, sync_env, takeover_signals, telegram,
    trade_log,
)
from tests.test_qt_trading import FakeSymbols

from PySide6.QtCore import Qt


@pytest.fixture
def prod(make_window, live_files, qtbot):
    def make(**kw):
        # 假的 Gateway 來源與值班 I/O 是 make_window 的預設
        kw.setdefault("account_ctx_loader", live_ctx)
        kw.setdefault("live_files", live_files)
        kw.setdefault("symbol_loader", FakeSymbols)
        w = make_window(**kw)
        qtbot.mouseClick(w.nav_button("Trading"), Qt.LeftButton)
        return w
    return make


def _no_manual_service(**_kw):
    return NS(has_active_orders=lambda: False, has_pending_notifications=lambda: False,
              list_orders=lambda **_: [])


def test_trading_banner_says_orders_go_to_live_accounts(prod):
    w = prod(manual_order_service=_no_manual_service)
    text = w.page_text("Trading")
    assert "手動單、Sync Pos、接管送到實盤帳戶" in text and "並行模式" not in text
    assert "paper" not in w.trading.banner_title.text()


def test_manual_order_tracking_runs_in_the_background(prod, tmp_path):
    started = []
    service = NS(has_active_orders=lambda: False, has_pending_notifications=lambda: False,
                 list_orders=lambda **_: [], start_monitor=lambda: started.append("start"),
                 stop_monitor=lambda: started.append("stop"))
    w = prod(manual_order_service=lambda **kw: service)
    assert started == ["start"]
    w.shutdown()
    assert started == ["start", "stop"]


# ── Sync Pos、接管：實盤的畫面文字 ───────────────────────────────────

def test_live_sync_confirm_and_log_say_live(
        prod, broker, sync_env, telegram, trade_log, dialogs, qtbot):
    broker.managed = ["U001"]
    broker.positions = {"AAPL": 100.0}
    w = prod(broker=broker,
             manual_order_service=_no_manual_service)
    qtbot.mouseClick(w.trading.sync_tab, Qt.LeftButton)
    sync = w.trading.sync
    sync.account_box.setCurrentText("Taylor")
    qtbot.mouseClick(sync.compute_button, Qt.LeftButton)
    qtbot.waitUntil(lambda: not sync.computing(), timeout=5000)
    assert "Sync Position（實盤）" in w.dashboard.log_text()
    qtbot.mouseClick(sync.confirm_button, Qt.LeftButton)
    qtbot.waitUntil(lambda: not sync.sending(), timeout=5000)
    [(_title, prompt, _warning)] = dialogs.confirms
    assert "送往：實盤帳戶（各帳戶自己的 Gateway）" in prompt
    assert sent(broker) == [("AAPL", "BUY", 150, "LMT", "U001"),
                             ("XYZ", "BUY", 250, "LMT", "U001")]
    assert "限價單（實盤）" in sync.status_text()


def test_live_takeover_lists_live_pauses_and_logs_live(
        prod, broker, takeover_signals, live_files, telegram, dialogs, qtbot):
    pause_xyz(live_files)
    broker.managed = ["U001"]
    w = prod(broker=broker,
             manual_order_service=_no_manual_service)
    qtbot.mouseClick(w.trading.takeover_tab, Qt.LeftButton)
    panel = w.trading.takeover
    assert panel.symbols() == ["XYZ"]
    qtbot.mouseClick(panel.compute_button, Qt.LeftButton)
    qtbot.waitUntil(lambda: not panel.computing(), timeout=5000)
    qtbot.mouseClick(panel.confirm_button, Qt.LeftButton)
    qtbot.waitUntil(lambda: not panel.sending(), timeout=5000)
    [(_title, prompt, _warning)] = dialogs.confirms
    assert "送往：實盤帳戶（各帳戶自己的 Gateway）" in prompt
    assert "✅ 接管完成" in panel.status_text()
    log = w.dashboard.log_text()
    assert "交給策略接管（實盤）" in log
    assert "[手動持倉·實盤] [Taylor] ✅ XYZ 已交給策略接管" in log
