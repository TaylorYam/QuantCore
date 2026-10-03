"""新 Panel 的 Sync Pos 畫面（#152）。

從主視窗操作，跑真的 sync_position（計算、差額、送單）與 manual_attribution，
只把 IB 換成假的 Broker、訊號換成固定值。驗證畫面流程：閘門拒絕時畫面說明原因、
預覽 → 確認 → 送出、碰撞暫停原因顯示、沒人在等的計算結果被丟棄。paper／實盤的
身分、紀錄、state、通知差異在 test_qt_order_target。
不連 IB、不發 Telegram、不讀真實 config。
"""
from __future__ import annotations

import threading
from types import SimpleNamespace as NS

import pytest

from tests.test_qt_panel import (  # noqa: F401  fixtures
    dialogs, extras, make_window, portfolio, ports, source,
)
from tests.test_qt_order_target import (  # noqa: F401  fixtures
    PAPER, SyncBroker, base_ctx, broker, live_files, paper_files, pause_xyz,
    sync_env,
)

from PySide6.QtCore import Qt


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
        sync_window, broker, paper_files, qtbot):
    # paper 帳戶先有策略外的 XYZ 10 股（尚未加入 S&P 500 時就有）→ 暫停
    pause_xyz(paper_files)
    w = sync_window()
    sync = w.trading.sync
    compute(qtbot, sync)
    text = sync.preview_text()
    assert "手動持倉暫停" in text and "XYZ" in text and "手動持倉 10 股" in text
    assert [row[1] for row in sync.trade_table()] == ["AAPL"]
    qtbot.waitUntil(lambda: "XYZ 策略單暫停" in w.dashboard.log_text(), timeout=2000)


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
    # 舊的計算還連著 paper（同一個 clientId）：結束前不能重算
    assert not sync.compute_button.isEnabled()
    broker.gate.set()
    qtbot.waitUntil(sync.compute_button.isEnabled, timeout=5000)
    assert sync.trade_table() == []
    assert not sync.confirm_button.isVisible()
    assert broker.placed == []


def test_changing_the_account_clears_the_preview(sync_window, broker, qtbot):
    w = sync_window()
    sync = w.trading.sync
    compute(qtbot, sync, "Taylor")
    assert sync.confirm_button.isEnabled()
    sync.account_box.setCurrentText("Rosa")
    assert sync.trade_table() == []
    assert not sync.confirm_button.isVisible()
    assert "重新計算" in sync.status_text()


def test_pause_notice_is_logged_once_like_tk(sync_window, paper_files, qtbot):
    pause_xyz(paper_files)
    w = sync_window()
    sync = w.trading.sync
    compute(qtbot, sync)
    compute(qtbot, sync)
    qtbot.wait(100)
    assert w.dashboard.log_text().count("[手動持倉·paper]") == 1


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
