"""Panel 的交給策略接管畫面（#153）。

從主視窗操作，跑真的 strategy_takeover（預覽、重算核對、送單、對帳）與
manual_attribution，只把 IB 換成假的 Broker、訊號換成固定值、Telegram 換成假的。
驗證畫面流程：預覽 → 確認 → 送出、拒絕與過期在畫面上說明、Sync Pos 暫停列可直接
接管、沒人在等的計算結果被丟棄。帳戶身分、紀錄、通知在 test_qt_order_target。
不連 IB、不讀真實 config。
"""
from __future__ import annotations

import threading
from types import SimpleNamespace as NS

import pytest

from tests.test_qt_panel import (  # noqa: F401  fixtures
    dialogs, extras, make_window, portfolio, ports, source,
)
from tests.test_qt_order_target import (  # noqa: F401  fixtures
    broker, live_ctx, live_files, pause_xyz, sent, sync_env, takeover_signals, telegram,
    trade_log,
)

from PySide6.QtCore import Qt


@pytest.fixture
def takeover_env(takeover_signals, sync_env, live_files):
    """帳戶 Taylor 先有策略外的 XYZ 10 股，之後 XYZ 被加入 S&P 500 → 暫停。"""
    pause_xyz(live_files)
    return sync_env


@pytest.fixture
def takeover_window(make_window, broker, takeover_env, live_files, telegram, trade_log,
                    qtbot):
    def make(**kw):
        kw.setdefault("broker", broker)
        kw.setdefault("live_files", live_files)
        kw.setdefault("account_ctx_loader", live_ctx)
        kw.setdefault("manual_order_service", lambda **_kw: NS(
            has_active_orders=lambda: False, has_pending_notifications=lambda: False,
            list_orders=lambda **_: []))
        w = make_window(**kw)
        qtbot.mouseClick(w.nav_button("Trading"), Qt.LeftButton)
        qtbot.mouseClick(w.trading.takeover_tab, Qt.LeftButton)
        return w
    return make


def preview(qtbot, panel, account="Taylor", symbol="XYZ"):
    panel.account_box.setCurrentText(account)
    panel.symbol_box.setCurrentText(symbol)
    qtbot.mouseClick(panel.compute_button, Qt.LeftButton)
    qtbot.waitUntil(lambda: not panel.computing(), timeout=5000)


def confirm(qtbot, panel):
    qtbot.mouseClick(panel.confirm_button, Qt.LeftButton)
    qtbot.waitUntil(lambda: not panel.sending(), timeout=5000)


# ── 預覽 → 確認 → 送出 ───────────────────────────────────────────────

def test_takeover_previews_then_sends_only_that_symbol(
        takeover_window, broker, dialogs, telegram, qtbot):
    w = takeover_window()
    panel = w.trading.takeover
    assert panel.symbols() == ["XYZ"]                  # 只列這個帳戶被暫停的標的
    preview(qtbot, panel)

    assert "Computing strategy targets" in panel.progress_text()
    text = panel.preview_text()
    for line in ("Account：Taylor", "標的：XYZ", "手動紀錄股數：10", "IB 現有股數：10",
                 "策略整數目標：250 股（SP500 250）", "差額：+240 股 — 買進 240 股"):
        assert line in text
    assert panel.confirm_button.text() == "確認接管並送出 BUY 240"
    assert broker.placed == []

    confirm(qtbot, panel)
    [(title, prompt, warning)] = dialogs.confirms
    assert title == "確認交給策略接管" and warning
    assert "送出 BUY 240 股 XYZ" in prompt and "實盤帳戶" in prompt
    assert sent(broker) == [("XYZ", "BUY", 240, "LMT", "U001")]
    assert "✅ 接管完成" in panel.status_text() and "XYZ 恢復策略送單" in panel.status_text()
    log = w.dashboard.log_text()
    assert "[>] 交給策略接管 [Taylor] XYZ..." in log
    assert "[手動持倉·實盤] [Taylor] ✅ XYZ 已交給策略接管" in log
    assert any("XYZ 已交給策略接管" in m for m, _d in telegram.messages)   # 也發該帳戶 Telegram
    assert not panel.confirm_button.isVisible()          # 同一份預覽不能再送一次
    assert panel.symbols() == []                         # 解除暫停後清單跟著更新


def test_declining_the_confirm_dialog_sends_nothing(takeover_window, broker, dialogs, qtbot):
    w = takeover_window()
    panel = w.trading.takeover
    preview(qtbot, panel)
    dialogs.confirm_answers = [False]
    qtbot.mouseClick(panel.confirm_button, Qt.LeftButton)
    assert broker.placed == [] and panel.confirm_button.isEnabled()


def test_cancel_clears_the_preview(takeover_window, broker, qtbot):
    w = takeover_window()
    panel = w.trading.takeover
    preview(qtbot, panel)
    qtbot.mouseClick(panel.cancel_button, Qt.LeftButton)
    assert not panel.confirm_button.isVisible() and broker.placed == []


# ── 拒絕與過期 ───────────────────────────────────────────────────────

def test_open_order_on_the_symbol_is_refused_with_the_reason(takeover_window, broker, qtbot):
    broker.open_orders = [NS(order=NS(account="U001", action="SELL", totalQuantity=3),
                             contract=NS(symbol="XYZ"), orderStatus=NS(status="Submitted"))]
    w = takeover_window()
    panel = w.trading.takeover
    preview(qtbot, panel)
    status = panel.status_text()
    assert "不能接管" in status and "1 筆掛單（SELL 3）" in status
    assert not panel.confirm_button.isVisible()
    assert "[X] 接管預覽失敗" in w.dashboard.log_text()


def test_stale_preview_is_replaced_and_must_be_confirmed_again(
        takeover_window, broker, dialogs, qtbot):
    w = takeover_window()
    panel = w.trading.takeover
    preview(qtbot, panel)
    broker.positions["XYZ"] = 12.0                       # 預覽後在 App 又買 2 股
    confirm(qtbot, panel)
    assert broker.placed == []
    assert "預覽後變了" in panel.note_text()
    assert "IB 現有股數：12" in panel.preview_text()
    assert panel.confirm_button.text() == "確認接管並送出 BUY 238"
    assert "預覽後變了" in panel.status_text()

    confirm(qtbot, panel)
    assert len(dialogs.confirms) == 2
    assert sent(broker) == [("XYZ", "BUY", 238, "LMT", "U001")]


def test_rejected_order_keeps_the_pause_and_says_why(
        takeover_window, broker, live_files, qtbot):
    import manual_attribution as ma

    broker.behavior = "reject"
    w = takeover_window()
    panel = w.trading.takeover
    preview(qtbot, panel)
    confirm(qtbot, panel)
    assert "❌ 接管未完成" in panel.status_text() and "IB 拒單" in panel.status_text()
    assert "XYZ" in ma.paused_symbols("Taylor", path=live_files.attribution)
    assert "XYZ 接管未完成" in w.dashboard.log_text()


def test_account_without_paused_symbols_has_nothing_to_take_over(takeover_window, qtbot):
    w = takeover_window()
    panel = w.trading.takeover
    panel.account_box.setCurrentText("Rosa")
    assert panel.symbols() == []
    assert not panel.compute_button.isEnabled()
    assert "沒有手動持倉暫停" in panel.status_text()


# ── 從 Sync Pos 的暫停列進來 ─────────────────────────────────────────

def test_sync_pos_paused_row_hands_over_to_the_takeover_preview(
        takeover_window, broker, qtbot):
    w = takeover_window()
    sync = w.trading.sync
    qtbot.mouseClick(w.trading.sync_tab, Qt.LeftButton)
    sync.account_box.setCurrentText("Taylor")
    qtbot.mouseClick(sync.compute_button, Qt.LeftButton)
    qtbot.waitUntil(lambda: not sync.computing(), timeout=5000)
    assert sync.confirm_button.isEnabled()

    qtbot.mouseClick(sync.takeover_buttons["XYZ"], Qt.LeftButton)
    panel = w.trading.takeover
    assert w.trading.takeover_tab.isChecked() and panel.isVisible()
    assert not sync.confirm_button.isVisible()           # Sync 預覽作廢，不能再送
    qtbot.waitUntil(lambda: not panel.computing(), timeout=5000)
    assert panel.account_box.currentText() == "Taylor"
    assert "標的：XYZ" in panel.preview_text()


def test_busy_takeover_keeps_the_sync_preview(takeover_window, broker, qtbot):
    w = takeover_window()
    sync, panel = w.trading.sync, w.trading.takeover
    qtbot.mouseClick(w.trading.sync_tab, Qt.LeftButton)
    qtbot.mouseClick(sync.compute_button, Qt.LeftButton)
    qtbot.waitUntil(lambda: not sync.computing(), timeout=5000)
    broker.gate = threading.Event()
    qtbot.mouseClick(panel.compute_button, Qt.LeftButton)  # 接管還在算
    assert panel.computing()

    qtbot.mouseClick(sync.takeover_buttons["XYZ"], Qt.LeftButton)
    assert sync.confirm_button.isEnabled()               # Sync 預覽沒被丟掉
    assert "接管預覽還在計算" in sync.status_text()
    broker.gate.set()
    qtbot.waitUntil(lambda: not panel.computing(), timeout=5000)


def test_hand_over_matches_the_account_name_case_insensitively(takeover_window, qtbot):
    w = takeover_window()
    panel = w.trading.takeover
    panel.account_box.setCurrentText("Rosa")
    assert w.trading.open_takeover("taylor", "XYZ") == ""
    qtbot.waitUntil(lambda: not panel.computing(), timeout=5000)
    assert panel.account_box.currentText() == "Taylor"
    assert "Account：Taylor" in panel.preview_text()
    assert "Nobody" in w.trading.open_takeover("Nobody", "XYZ")
    assert "Nobody" in panel.status_text()


# ── 沒人在等的結果 ───────────────────────────────────────────────────

def test_discarded_computation_is_dropped(takeover_window, broker, qtbot):
    broker.gate = threading.Event()
    w = takeover_window()
    panel = w.trading.takeover
    qtbot.mouseClick(panel.compute_button, Qt.LeftButton)
    assert panel.computing()
    qtbot.mouseClick(panel.discard_button, Qt.LeftButton)
    assert not panel.computing() and not panel.compute_button.isEnabled()
    broker.gate.set()
    qtbot.waitUntil(panel.compute_button.isEnabled, timeout=5000)
    assert not panel.confirm_button.isVisible() and broker.placed == []


def test_shutdown_drops_a_running_computation(takeover_window, broker, qtbot):
    broker.gate = threading.Event()
    w = takeover_window()
    panel = w.trading.takeover
    qtbot.mouseClick(panel.compute_button, Qt.LeftButton)
    w.shutdown()
    broker.gate.set()
    qtbot.wait(300)
    assert not panel.confirm_button.isVisible() and broker.placed == []
