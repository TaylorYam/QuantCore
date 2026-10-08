"""Panel 的值班與連線身分（#154）。

Panel 值班（Rosa poll／TY check／Morning check，跑既有的 MonitorEngine），心跳用
ROLE_MONITOR、查詢用 ROLE_PANEL。從主視窗操作，Gateway 心跳、Telegram、冷連線、sleep
全部注入假的。不連 IB、不發 Telegram、不讀真實 config。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from tests.test_qt_panel import (  # noqa: F401  fixtures
    FakeGatewaySource, MutableClock, dialogs, extras, make_window, portfolio, ports, source,
)


class TickingClock:
    """從指定時間開始、跟著真實時間走的台北時鐘（值班排程到點後要往前走）。"""

    def __init__(self, start: datetime):
        self.start = start
        self.t0 = time.monotonic()

    def __call__(self) -> datetime:
        return self.start + timedelta(seconds=time.monotonic() - self.t0)


@pytest.fixture
def watch_source():
    return FakeGatewaySource()


@pytest.fixture
def telegram():
    return []


@pytest.fixture
def prod_window(make_window, watch_source, telegram):
    from qt_panel.watch import WatchPorts

    def make(now, **kw):
        cold = kw.pop("cold_connect", lambda name: None)
        kw.setdefault("gateway_source", watch_source)
        kw.setdefault("taipei_now", TickingClock(now))
        kw.setdefault("watch_ports", WatchPorts(
            telegram=telegram.append, cold_connect=cold, sleep=lambda s: None, start_delay_ms=0,
            recovery_delay_ms=kw.pop("recovery_delay_ms", 10 * 60 * 1000)))
        return make_window(**kw)
    return make


def wait_log(qtbot, w, text, timeout=3000):
    qtbot.waitUntil(lambda: text in w.dashboard.log_text(), timeout=timeout)


def wait_telegram(qtbot, telegram, count, timeout=3000):
    # worker 先 log 再 notify（背景執行緒）；看到 ALERT log 時訊息不一定已送出
    qtbot.waitUntil(lambda: len(telegram) == count, timeout=timeout)


# ── 值班卡 ───────────────────────────────────────────────────────────

def test_watch_card_says_the_panel_is_on_duty(prod_window, qtbot):
    w = prod_window(datetime(2026, 9, 29, 20, 10))
    assert "Panel 值班中" in w.dashboard.watch_text()
    assert "Tk" not in w.dashboard.watch_text()


# ── 值班啟動 ─────────────────────────────────────────────────────────

def test_panel_starts_the_three_watches(prod_window, qtbot):
    w = prod_window(datetime(2026, 9, 29, 20, 10))               # 週二晚上、窗外
    wait_log(qtbot, w, "[GW] Morning check: next at")
    log = w.dashboard.log_text()
    assert "[GW] Rosa poll: panel start outside window → scheduling" in log
    assert "[GW] Rosa poll: next tick at 2026-09-29 21:00" in log
    assert "[GW] TY check: next at 2026-09-29 20:05" not in log   # 已過，排下一次
    assert "[GW] TY check: next at 2026-09-30 20:05" in log


def test_rosa_poll_inside_the_window_alerts_when_retry_fails(
        prod_window, watch_source, telegram, qtbot):
    watch_source.online["Rosa"] = False
    w = prod_window(datetime(2026, 9, 29, 21, 10))                # 窗內 → 立刻跑一次
    wait_log(qtbot, w, "Rosa poll 21:10 → ALERT")
    wait_telegram(qtbot, telegram, 1)
    assert watch_source.restarted == ["Rosa"]
    assert len(telegram) == 1 and "Rosa" in telegram[0] and "disconnected" in telegram[0]


def test_alert_that_was_not_sent_shows_in_the_output_log(prod_window, watch_source, qtbot):
    """共用頻道沒設定或送不出去（send_system 回 False）：Output Log 看得到沒送出（#207）。"""
    from qt_panel.watch import WatchPorts

    tried = []
    watch_source.online["Rosa"] = False
    w = prod_window(datetime(2026, 9, 29, 21, 10), watch_ports=WatchPorts(
        telegram=lambda m: tried.append(m) or False, cold_connect=lambda n: None,
        sleep=lambda s: None, start_delay_ms=0))
    wait_log(qtbot, w, "[!] 值班 Telegram 沒送出")
    assert len(tried) == 1 and "Rosa" in tried[0]


def test_watch_alerts_go_to_the_shared_channel_by_default():
    import notify
    from qt_panel.watch import WatchPorts

    assert WatchPorts().telegram is notify.send_system


def test_rosa_poll_healthy_sends_nothing(prod_window, telegram, qtbot):
    w = prod_window(datetime(2026, 9, 29, 21, 10))
    wait_log(qtbot, w, "Rosa poll 21:10 → ok")
    assert telegram == []


def test_ty_check_at_20_05_cold_connects_offline_accounts_and_alerts(
        prod_window, watch_source, telegram, qtbot):
    tried = []

    def cold(name):
        tried.append(name)
        raise ConnectionRefusedError("no gateway")

    watch_source.online["Yvonne"] = False
    clock = MutableClock(datetime(2026, 9, 29, 20, 4, 59, 500_000))
    w = prod_window(None, cold_connect=cold, taipei_now=clock)
    # 計時器已排好（0.5 秒後）；事件迴圈跑起來前把時鐘撥過 20:05，觸發後排的是明天
    clock.now = datetime(2026, 9, 29, 20, 5, 1)
    wait_log(qtbot, w, "TY check Yvonne → ALERT")
    wait_log(qtbot, w, "TY check Taylor → ok")
    wait_telegram(qtbot, telegram, 1)
    assert tried == ["Yvonne"]                       # 心跳在線的不冷連線
    assert len(telegram) == 1 and "Yvonne" in telegram[0]


def test_morning_check_rechecks_gateways_and_alerts_offline_accounts(
        prod_window, watch_source, telegram, monkeypatch, qtbot):
    import morning_check

    monkeypatch.setattr(morning_check, "constituents_staleness_warning", lambda *a: None)
    watch_source.online["Taylor"] = False
    clock = MutableClock(datetime(2026, 9, 30, 6, 59, 59, 500_000))   # 週三（交易日）
    w = prod_window(None, taipei_now=clock)
    clock.now = datetime(2026, 9, 30, 7, 0, 1)
    wait_log(qtbot, w, "Morning check → ALERT")
    wait_telegram(qtbot, telegram, 1)
    assert watch_source.rechecks == 1                # Check Now 等效重新探測
    assert len(telegram) == 1 and "Taylor" in telegram[0]
    w.tick_minute()                                  # 並行模式的 07:00 重新檢查不再重複做
    assert watch_source.rechecks == 1


def test_rosa_going_offline_schedules_a_one_shot_recovery(
        prod_window, watch_source, qtbot):
    w = prod_window(datetime(2026, 9, 29, 15, 0), recovery_delay_ms=50)
    watch_source.on_state("Rosa", "online", "")
    watch_source.online["Rosa"] = False
    watch_source.on_state("Rosa", "offline", "timeout")
    wait_log(qtbot, w, "Rosa one-shot recovery: attempting reconnect")
    qtbot.waitUntil(lambda: watch_source.restarted == ["Rosa"], timeout=2000)


def test_shutdown_stops_the_watch(prod_window, telegram, watch_source, qtbot):
    watch_source.online["Yvonne"] = False
    clock = MutableClock(datetime(2026, 9, 29, 20, 4, 59, 700_000))
    w = prod_window(None, taipei_now=clock,
                    cold_connect=lambda name: (_ for _ in ()).throw(OSError("down")))
    clock.now = datetime(2026, 9, 29, 20, 5, 1)
    w.shutdown()
    qtbot.wait(600)
    assert telegram == [] and "TY check Yvonne" not in w.dashboard.log_text()


# ── 連線身分 ─────────────────────────────────────────────────────────

def test_heartbeat_uses_the_watch_identity():
    from account_utils import ROLE_MONITOR
    from qt_panel.gateway_status import MonitorGatewaySource
    from tests.test_qt_panel import FakeMonitor

    FakeMonitor.instances = []
    src = MonitorGatewaySource(["Taylor", "Rosa"], monitor_cls=FakeMonitor)
    seen = []
    src.start(lambda *args: seen.append(args))
    mon = FakeMonitor.instances[0]
    assert mon.account_names == ["Taylor", "Rosa"] and mon.started
    assert (mon.role, mon.readonly) == (ROLE_MONITOR, False)   # 值班心跳、一般連線
    assert src.restart("Rosa") and mon.restarted == ["Rosa"]

    mon.on_state_change("Taylor", "online")
    mon.on_disconnected("Rosa", "timeout")
    assert seen == [("Taylor", "online", ""), ("Rosa", "offline", "timeout")]
    mon.restarted.clear()
    src.recheck()
    assert mon.restarted == ["Taylor", "Rosa"]
    assert seen[-1] == ("Rosa", "connecting", "")      # 重新開始連的帳戶先顯示連線中
    assert ("Taylor", "connecting", "") not in seen     # 還連著的不動
    src.stop()
    assert mon.stopped


def test_portfolio_queries_use_the_panel_identity(monkeypatch):
    import portfolio_snapshot
    from account_ctx import AccountCtx
    from account_utils import ROLE_PANEL
    from qt_panel import portfolio_source

    seen = []
    monkeypatch.setattr(AccountCtx, "from_name", classmethod(
        lambda cls, name, role: seen.append((name, role)) or _ctx(name, 20 + role)))
    monkeypatch.setattr(portfolio_snapshot, "fetch_snapshot",
                        lambda ctx, readonly: seen.append((ctx.client_id, readonly))
                        or portfolio_snapshot.PortfolioSnapshot.unavailable("x"))
    portfolio_source.fetch_panel_portfolio("Taylor")
    assert seen == [("Taylor", ROLE_PANEL), (25, True)]


def _ctx(name, client_id):
    from types import SimpleNamespace
    return SimpleNamespace(name=name, client_id=client_id)


def test_rosa_state_recolours_the_tray(prod_window, watch_source, qtbot):
    states = []

    class StateTray:
        def __init__(self, on_show, on_quit):
            pass

        def show(self):
            pass

        def hide(self):
            pass

        def update_state(self, state, when):
            states.append(state)

    prod_window(datetime(2026, 9, 29, 15, 0), tray_factory=StateTray)
    watch_source.on_state("Rosa", "online", "")
    watch_source.on_state("Taylor", "offline", "x")
    watch_source.on_state("Rosa", "offline", "timeout")
    assert states == ["online", "offline"]


def test_real_tray_shows_rosa_state_in_colour_and_tooltip(qtbot, tmp_path, monkeypatch):
    from PySide6.QtGui import QIcon
    from qt_panel import tray

    made = []
    monkeypatch.setattr(tray, "_state_icon", lambda color: made.append(color) or QIcon())
    t = tray.QtTray(on_show=lambda: None, on_quit=lambda: None, icon=QIcon(),
                    tooltip="QuantCore Panel")
    t.update_state("offline", datetime(2026, 9, 29, 21, 5))
    t.update_state("weird", datetime(2026, 9, 29, 21, 6))
    assert made == ["red", "gray"]
    assert t._icon.toolTip() == "QuantCore Panel\nRosa: WEIRD (last 21:06)"


def test_watch_starts_a_moment_after_the_heartbeat(prod_window, qtbot):
    from qt_panel.watch import WatchPorts

    w = prod_window(datetime(2026, 9, 29, 21, 10), watch_ports=WatchPorts(
        telegram=lambda m: None, cold_connect=lambda n: None, sleep=lambda s: None,
        start_delay_ms=300))
    assert "Rosa poll" not in w.dashboard.log_text()
    wait_log(qtbot, w, "Rosa poll 21:10 → ok")
