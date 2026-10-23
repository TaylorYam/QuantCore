"""Panel 的 Sync Pos 畫面（#152）。

從主視窗操作，跑真的 sync_position（計算、差額、送單）與 manual_attribution，
只把 IB 換成假的 Broker、訊號換成固定值、Telegram 與 trade_log 換成假的。驗證畫面
流程：預覽 → 確認 → 送出、碰撞暫停原因顯示、沒人在等的計算結果被丟棄。帳戶身分、
紀錄、state、通知在 test_qt_order_target。不連 IB、不讀真實 config。
"""
from __future__ import annotations

import threading
from types import SimpleNamespace as NS

import pytest

from tests.test_qt_panel import (  # noqa: F401  fixtures
    dialogs, extras, make_window, portfolio, ports, source,
)
from tests.test_qt_order_target import (  # noqa: F401  fixtures
    broker, live_ctx, live_files, pause_xyz, sent, sync_env, telegram, trade_log,
)

from PySide6.QtCore import Qt


@pytest.fixture
def sync_window(make_window, broker, sync_env, live_files, telegram, trade_log, qtbot):
    def make(**kw):
        kw.setdefault("broker", broker)
        kw.setdefault("live_files", live_files)
        kw.setdefault("account_ctx_loader", live_ctx)
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


# ── 計算 → 預覽 → 確認 → 送出 ─────────────────────────────────────────

def test_sync_previews_then_sends_limit_orders_to_the_account(
        sync_window, broker, dialogs, trade_log, qtbot):
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
    assert "送往：實盤帳戶（各帳戶自己的 Gateway）" in prompt
    assert sent(broker) == [("AAPL", "BUY", 150, "LMT", "U001"),
                             ("XYZ", "BUY", 250, "LMT", "U001")]
    qtbot.waitUntil(lambda: "Placed 2 orders" in w.dashboard.log_text(), timeout=2000)
    assert [r["account_name"] for r in trade_log] == ["Taylor", "Taylor"]   # 每筆單一列
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
        sync_window, broker, live_files, qtbot):
    # 帳戶先有策略外的 XYZ 10 股（尚未加入 S&P 500 時就有）→ 暫停
    pause_xyz(live_files)
    w = sync_window()
    sync = w.trading.sync
    compute(qtbot, sync)
    text = sync.preview_text()
    assert "手動持倉暫停" in text and "XYZ" in text and "手動持倉 10 股" in text
    assert [row[1] for row in sync.trade_table()] == ["AAPL"]
    qtbot.waitUntil(lambda: "XYZ 策略單暫停" in w.dashboard.log_text(), timeout=2000)


def test_unreadable_state_file_is_shown_in_the_preview_and_the_log(
        sync_window, broker, monkeypatch, tmp_path, qtbot):
    """#233：SP500 state 讀不出來 → SP500 持股這次不動，預覽與 Output Log 都告警。"""
    import account_state
    monkeypatch.setattr(account_state, "STATE_DIR", tmp_path)
    (tmp_path / "sp500_state_Taylor.json").write_bytes(b'{"bull_weeks_counter": 2,')
    broker.positions = {"AAPL": 100.0}
    w = sync_window()
    sync = w.trading.sync
    compute(qtbot, sync)
    text = sync.preview_text()
    assert "State 檔讀不出來（需要人工處理）" in text
    assert "SP500 state 讀不出來：sp500_state_Taylor.json" in text
    assert "Already on target" in text                     # AAPL 維持 100、不買 XYZ
    assert "[!] [Taylor] ⚠️ SP500 state 讀不出來" in w.dashboard.log_text()


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
    # 舊的計算還連著 Gateway（同一個 clientId）：結束前不能重算
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


def test_pause_notice_is_logged_once(sync_window, live_files, telegram, qtbot):
    pause_xyz(live_files)
    w = sync_window()
    sync = w.trading.sync
    compute(qtbot, sync)
    compute(qtbot, sync)
    qtbot.wait(100)
    assert w.dashboard.log_text().count("[手動持倉·實盤]") == 1
    assert len(telegram.messages) == 1                   # 送達才記已通知，只發一次


def test_shutdown_drops_a_running_computation(sync_window, broker, qtbot):
    broker.gate = threading.Event()
    w = sync_window()
    sync = w.trading.sync
    qtbot.mouseClick(sync.compute_button, Qt.LeftButton)
    w.shutdown()
    broker.gate.set()
    qtbot.wait(300)
    assert sync.trade_table() == [] and broker.placed == []
