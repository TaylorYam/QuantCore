"""新 Panel 正式模式（#154）：值班、連線身分。

正式模式下新 Panel 接手 Tk Panel 的值班（Rosa poll／TY check／Morning check，跑既有的
MonitorEngine）與原有的 clientId role；並行模式下不值班。從主視窗操作，Gateway 心跳、
Telegram、冷連線、sleep 全部注入假的。不連 IB、不發 Telegram、不讀真實 config。
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


class WatchSource(FakeGatewaySource):
    """可以回答心跳狀態、重連的假 Gateway 來源（值班用）。"""

    def __init__(self):
        super().__init__()
        self.online = {"Taylor": True, "Rosa": True, "Yvonne": True}
        self.restarted = []

    def is_online(self, name):
        return self.online.get(name, False)

    def restart(self, name):
        self.restarted.append(name)
        return True

    def ready(self):
        return self.on_state is not None


@pytest.fixture
def watch_source():
    return WatchSource()


@pytest.fixture
def telegram():
    return []


@pytest.fixture
def prod_window(make_window, watch_source, telegram):
    from qt_panel.mode import PanelMode
    from qt_panel.watch import WatchPorts

    def make(now, **kw):
        cold = kw.pop("cold_connect", lambda name: None)
        kw.setdefault("mode", PanelMode.PRODUCTION)
        kw.setdefault("gateway_source", watch_source)
        kw.setdefault("taipei_now", TickingClock(now))
        kw.setdefault("watch_ports", WatchPorts(
            telegram=telegram.append, cold_connect=cold, sleep=lambda s: None, start_delay_ms=0,
            recovery_delay_ms=kw.pop("recovery_delay_ms", 10 * 60 * 1000)))
        return make_window(**kw)
    return make


def wait_log(qtbot, w, text, timeout=3000):
    qtbot.waitUntil(lambda: text in w.dashboard.log_text(), timeout=timeout)


# ── 模式 ─────────────────────────────────────────────────────────────

def test_production_mode_setting_is_recognised():
    from qt_panel.mode import PanelMode, qt_panel_owns_live, resolve_mode

    assert resolve_mode({"qt_panel_mode": "production"}) == (PanelMode.PRODUCTION, "")
    assert qt_panel_owns_live({"qt_panel_mode": "production"})
    assert not qt_panel_owns_live({"qt_panel_mode": "parallel"})
    assert not qt_panel_owns_live({})
    assert not qt_panel_owns_live({"qt_panel_mode": "yolo"})


def test_header_and_watch_card_say_the_new_panel_is_on_duty(prod_window, qtbot):
    w = prod_window(datetime(2026, 9, 29, 20, 10))
    assert "正式模式" in w.header_text() and "並行模式" not in w.header_text()
    assert "新 Panel" in w.dashboard.watch_note()
    assert "新 Panel 值班中" in w.dashboard.watch_text()


# ── 值班啟動 ─────────────────────────────────────────────────────────

def test_production_mode_starts_the_three_watches(prod_window, qtbot):
    w = prod_window(datetime(2026, 9, 29, 20, 10))               # 週二晚上、窗外
    wait_log(qtbot, w, "[GW] Morning check: next at")
    log = w.dashboard.log_text()
    assert "[GW] Rosa poll: panel start outside window → scheduling" in log
    assert "[GW] Rosa poll: next tick at 2026-09-29 21:00" in log
    assert "[GW] TY check: next at 2026-09-29 20:05" not in log   # 已過，排下一次
    assert "[GW] TY check: next at 2026-09-30 20:05" in log


def test_parallel_mode_starts_no_watch(make_window, qtbot):
    w = make_window(taipei_now=MutableClock(datetime(2026, 9, 29, 20, 10)))
    qtbot.wait(200)
    assert "Rosa poll:" not in w.dashboard.log_text()
    assert "TY check:" not in w.dashboard.log_text()


def test_rosa_poll_inside_the_window_alerts_when_retry_fails(
        prod_window, watch_source, telegram, qtbot):
    watch_source.online["Rosa"] = False
    w = prod_window(datetime(2026, 9, 29, 21, 10))                # 窗內 → 立刻跑一次
    wait_log(qtbot, w, "Rosa poll 21:10 → ALERT")
    assert watch_source.restarted == ["Rosa"]
    assert len(telegram) == 1 and "Rosa" in telegram[0] and "disconnected" in telegram[0]


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
    w = prod_window(datetime(2026, 9, 29, 20, 4, 59, 900_000), cold_connect=cold)
    wait_log(qtbot, w, "TY check Yvonne → ALERT")
    wait_log(qtbot, w, "TY check Taylor → ok")
    assert tried == ["Yvonne"]                       # 心跳在線的不冷連線
    assert len(telegram) == 1 and "Yvonne" in telegram[0]


def test_morning_check_rechecks_gateways_and_alerts_offline_accounts(
        prod_window, watch_source, telegram, monkeypatch, qtbot):
    import morning_check

    monkeypatch.setattr(morning_check, "constituents_staleness_warning", lambda *a: None)
    watch_source.online["Taylor"] = False
    w = prod_window(datetime(2026, 9, 30, 6, 59, 59, 900_000))    # 週三（交易日）
    wait_log(qtbot, w, "Morning check → ALERT")
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
    w = prod_window(datetime(2026, 9, 29, 20, 4, 59, 700_000),
                    cold_connect=lambda name: (_ for _ in ()).throw(OSError("down")))
    w.shutdown()
    qtbot.wait(600)
    assert telegram == [] and "TY check Yvonne" not in w.dashboard.log_text()


# ── 連線身分 ─────────────────────────────────────────────────────────

def test_production_heartbeat_uses_the_panel_watch_identity():
    from account_utils import ROLE_MONITOR
    from qt_panel.gateway_status import gateway_source_for
    from qt_panel.mode import PanelMode
    from tests.test_qt_panel import FakeMonitor

    FakeMonitor.instances = []
    src = gateway_source_for(PanelMode.PRODUCTION, ["Taylor", "Rosa"], monitor_cls=FakeMonitor)
    src.start(lambda *args: None)
    mon = FakeMonitor.instances[0]
    assert (mon.role, mon.readonly) == (ROLE_MONITOR, False)   # 同 Tk Panel 的值班心跳
    assert src.restart("Rosa") and mon.restarted == ["Rosa"]


def test_production_portfolio_queries_use_the_panel_identity(monkeypatch):
    import ib_fetcher
    from account_ctx import AccountCtx
    from account_utils import ROLE_PANEL
    from qt_panel import portfolio_source
    from qt_panel.mode import PanelMode

    seen = []
    monkeypatch.setattr(AccountCtx, "from_name", classmethod(
        lambda cls, name, role: seen.append((name, role)) or _ctx(name, 20 + role)))
    monkeypatch.setattr(ib_fetcher, "fetch_portfolio",
                        lambda ctx, readonly: seen.append((ctx.client_id, readonly))
                        or {"available": False})
    portfolio_source.portfolio_fetch_for(PanelMode.PRODUCTION)("Taylor")
    assert seen == [("Taylor", ROLE_PANEL), (25, True)]            # Tk 的 fetch_ib_portfolio 同款


def _ctx(name, client_id):
    from types import SimpleNamespace
    return SimpleNamespace(name=name, client_id=client_id)


# ── 值班與實盤送單只歸一邊（切換開關＋值班鎖）────────────────────────

def test_new_panel_takes_duty_only_when_it_gets_the_lock():
    from qt_panel.mode import PanelMode, qt_panel_duty

    prod = {"qt_panel_mode": "production"}
    assert qt_panel_duty(prod, claim=lambda: True) == (PanelMode.PRODUCTION, "")
    mode, note = qt_panel_duty(prod, claim=lambda: False)        # Tk 還在值班
    assert mode is PanelMode.PARALLEL and "Tk Panel" in note and "重開" in note
    claimed = []
    assert qt_panel_duty({}, claim=lambda: claimed.append(1) or True) == (PanelMode.PARALLEL, "")
    assert claimed == []                                          # 並行模式不拿鎖


def test_tk_panel_watches_only_when_switch_and_lock_allow():
    from qt_panel.mode import tk_panel_duty

    assert tk_panel_duty({}, claim=lambda: True) == (False, "")    # 預設：照舊值班
    view_only, why = tk_panel_duty({"qt_panel_mode": "production"},
                                   claim=lambda: (_ for _ in ()).throw(AssertionError))
    assert view_only and "qt_panel_mode" in why                    # 正式模式：不拿鎖、只看
    view_only, why = tk_panel_duty({}, claim=lambda: False)        # 新 Panel 還在正式模式
    assert view_only and "新 Panel" in why and "重開" in why


def test_duty_lock_is_held_by_one_live_process(tmp_path):
    import os
    from qt_panel.mode import claim_live_duty, release_live_duty

    lock = tmp_path / "duty.lock"
    assert claim_live_duty(lock)
    lock.write_text(f"{os.getppid()}\n", encoding="utf-8")         # 另一個活著的行程拿著
    assert not claim_live_duty(lock)
    release_live_duty(lock, held=False)                            # 沒拿到就不能刪別人的
    assert lock.exists()
    lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
    release_live_duty(lock, held=True)
    assert not lock.exists()


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


def test_watch_starts_a_moment_after_the_heartbeat_like_tk(prod_window, qtbot):
    from qt_panel.watch import WatchPorts

    w = prod_window(datetime(2026, 9, 29, 21, 10), watch_ports=WatchPorts(
        telegram=lambda m: None, cold_connect=lambda n: None, sleep=lambda s: None,
        start_delay_ms=300))
    assert "Rosa poll" not in w.dashboard.log_text()
    wait_log(qtbot, w, "Rosa poll 21:10 → ok")
