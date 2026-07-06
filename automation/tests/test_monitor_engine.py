"""
值班監控引擎（monitor_engine）接線測試 — 候選 5。

panel 的排程+派工邏輯首次有考卷：
  - Engine：tick → pre → spawn workers → re-arm 順序、run_at_start 兩分支、
    delay 計算、stop_all（含 cancel 例外吞噬）、schedule_once
  - Rosa poll spec：ok 不發 TG、ALERT 發一次、GatewayMonitor 未備妥跳過
  - Rosa one-shot recovery：已回線跳過、離線才 spawn restart
  - TY check spec：hybrid（心跳 ONLINE 跳過冷連線）、冷連線失敗 → 紅色警報
  - Morning check spec：全綠靜默、離線發報、constituents staleness 第二封、
    未備妥時不 sleep 不發報

fake timer + fake env = MonitorEngine 的第二個 adapter（第一個是 panel 的 Tk）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

TAIPEI = ZoneInfo("Asia/Taipei")
# 2026-07-01（週三）22:00 台北 = NYSE 交易日、rosa poll 窗口內（21:00–05:00）
IN_WINDOW = datetime(2026, 7, 1, 22, 0, tzinfo=TAIPEI)


# ── fakes ─────────────────────────────────────────────────────────────

class FakeTimer:
    """記錄 schedule/cancel 的假計時器；fire(handle) 手動觸發。"""

    def __init__(self, events: list | None = None):
        self.entries: list[dict] = []
        self.events = events if events is not None else []

    def schedule(self, delay_ms, fn):
        self.events.append(("schedule", delay_ms))
        self.entries.append({"delay": delay_ms, "fn": fn, "alive": True})
        return len(self.entries) - 1

    def cancel(self, handle):
        self.entries[handle]["alive"] = False

    def fire(self, handle):
        e = self.entries[handle]
        assert e["alive"], "fired a cancelled timer"
        e["fn"]()


def make_env(*, ready=True, online=None, now=IN_WINDOW,
             cold_connect_error=None, accounts=("Taylor", "Rosa", "Yvonne")):
    """建假 MonitorEnv。online: dict[name → bool]（預設全 True）。
    回傳 (env, rec)；rec 記錄 logs / notes / restarts / sleeps / refreshes。"""
    from monitor_engine import MonitorEnv

    state = {"online": dict(online or {})}
    rec = {"logs": [], "notes": [], "restarts": [], "sleeps": [],
           "refreshes": 0, "cold": []}

    def _cold(name):
        rec["cold"].append(name)
        if cold_connect_error is not None:
            raise cold_connect_error

    env = MonitorEnv(
        monitor_ready=lambda: ready,
        is_online=lambda n: state["online"].get(n, True),
        restart=lambda n: (rec["restarts"].append(n), True)[1],
        cold_connect=_cold,
        accounts=lambda: list(accounts),
        refresh_ui=lambda: rec.__setitem__("refreshes", rec["refreshes"] + 1),
        log=rec["logs"].append,
        notify=rec["notes"].append,
        now=lambda: now,
        sleep=lambda s: rec["sleeps"].append(s),
    )
    return env, rec, state


def dummy_spec(events: list, *, pre=False, n_workers=1, label="Dummy",
               run_at_start=None, interval_s=300):
    from monitor_engine import MonitorSpec

    def worker(i):
        return lambda: events.append(("work", i))

    return MonitorSpec(
        label=label,
        next_time=lambda now: now + timedelta(seconds=interval_s),
        workers=lambda: [worker(i) for i in range(n_workers)],
        pre=(lambda: events.append(("pre",))) if pre else None,
        run_at_start=run_at_start,
        start_log_immediate="IMMEDIATE",
        start_log_waiting="WAITING",
    )


def make_engine(env, events=None):
    from monitor_engine import MonitorEngine
    timer = FakeTimer(events)
    spawned = []

    def spawn(fn):
        # 測試同步執行 worker（真 panel 是 daemon thread）
        if events is not None:
            events.append(("spawn",))
        spawned.append(fn)
        fn()

    engine = MonitorEngine(env, schedule=timer.schedule,
                           cancel=timer.cancel, spawn=spawn)
    return engine, timer, spawned


# ── Engine 行為 ───────────────────────────────────────────────────────

def test_start_arms_with_computed_delay():
    env, rec, _ = make_env()
    engine, timer, _ = make_engine(env)
    engine.start(dummy_spec([], interval_s=90))
    assert timer.entries[0]["delay"] == 90_000
    assert any("Dummy: next at" in l for l in rec["logs"])


def test_tick_order_pre_then_spawn_then_rearm():
    events: list = []
    env, rec, _ = make_env()
    engine, timer, _ = make_engine(env, events)
    engine.start(dummy_spec(events, pre=True, n_workers=2))
    events.clear()

    timer.fire(0)   # 觸發 tick
    kinds = [e[0] for e in events]
    # pre → spawn×2（同步執行 work）→ re-arm（schedule）
    assert kinds[0] == "pre"
    assert kinds.count("spawn") == 2
    assert kinds[-1] == "schedule", "re-arm 必須在派工之後"
    assert ("work", 0) in events and ("work", 1) in events


def test_run_at_start_true_spawns_immediately_with_log():
    events: list = []
    env, rec, _ = make_env()
    engine, timer, _ = make_engine(env, events)
    engine.start(dummy_spec(events, run_at_start=lambda now: True))
    assert "IMMEDIATE" in rec["logs"]
    assert ("work", 0) in events, "窗口內啟動必須立跑一次"
    assert len(timer.entries) == 1, "立跑之外照樣 arm 下一次"


def test_run_at_start_false_only_arms():
    events: list = []
    env, rec, _ = make_env()
    engine, timer, _ = make_engine(env, events)
    engine.start(dummy_spec(events, run_at_start=lambda now: False))
    assert "WAITING" in rec["logs"]
    assert ("work", 0) not in events


def test_stop_all_cancels_pending_and_swallows_cancel_error():
    env, rec, _ = make_env()
    engine, timer, _ = make_engine(env)
    engine.start(dummy_spec([]))
    engine.schedule_once(1000, lambda: None)
    assert all(e["alive"] for e in timer.entries)

    engine.stop_all()
    assert all(not e["alive"] for e in timer.entries)

    # cancel 丟例外不往外冒（Tk after_cancel 對已觸發 id 可能 raise）
    from monitor_engine import MonitorEngine
    eng2 = MonitorEngine(env, schedule=lambda ms, fn: object(),
                         cancel=lambda h: (_ for _ in ()).throw(RuntimeError()),
                         spawn=lambda fn: fn())
    eng2.start(dummy_spec([]))
    eng2.stop_all()   # 不應 raise


def test_past_due_next_time_clamps_to_1ms():
    env, rec, _ = make_env()
    engine, timer, _ = make_engine(env)
    engine.start(dummy_spec([], interval_s=-10))
    assert timer.entries[0]["delay"] == 1


# ── Rosa poll spec ────────────────────────────────────────────────────

def test_rosa_ok_logs_without_telegram():
    from monitor_engine import rosa_poll_spec
    env, rec, _ = make_env(online={"Rosa": True}, now=IN_WINDOW)
    spec = rosa_poll_spec(env)
    for w in spec.workers():
        w()
    assert any("Rosa poll" in l and "→ ok" in l for l in rec["logs"])
    assert rec["notes"] == [], "全綠不得發 TG（防洪設計）"


def test_rosa_offline_alerts_once_and_restarts():
    from monitor_engine import rosa_poll_spec
    env, rec, _ = make_env(online={"Rosa": False}, now=IN_WINDOW)
    spec = rosa_poll_spec(env)
    for w in spec.workers():
        w()
    assert rec["restarts"] == ["Rosa"], "離線必須先嘗試 restart"
    assert len(rec["notes"]) == 1, "警報恰好一封"
    assert any("ALERT" in l for l in rec["logs"])


def test_rosa_skips_when_monitor_not_ready():
    from monitor_engine import rosa_poll_spec
    env, rec, _ = make_env(ready=False, now=IN_WINDOW)
    spec = rosa_poll_spec(env)
    for w in spec.workers():
        w()
    assert rec["logs"] == [] and rec["notes"] == []


def test_rosa_recovery_skips_if_online():
    from monitor_engine import rosa_recovery_worker
    env, rec, _ = make_env(online={"Rosa": True})
    spawned = []
    rosa_recovery_worker(env, spawned.append)()
    assert any("already online, skip" in l for l in rec["logs"])
    assert spawned == []


def test_rosa_recovery_restarts_if_offline():
    from monitor_engine import rosa_recovery_worker
    env, rec, _ = make_env(online={"Rosa": False})
    spawned = []
    rosa_recovery_worker(env, spawned.append)()
    assert any("attempting reconnect" in l for l in rec["logs"])
    assert len(spawned) == 1
    spawned[0]()   # 執行被 spawn 的 restart thunk
    assert rec["restarts"] == ["Rosa"]


# ── TY check spec ─────────────────────────────────────────────────────

def test_ty_heartbeat_online_skips_cold_connect():
    from monitor_engine import ty_check_spec
    env, rec, _ = make_env(online={"Taylor": True, "Yvonne": True})
    for w in ty_check_spec(env).workers():
        w()
    assert rec["cold"] == [], "心跳 ONLINE = 活體證明，不得冷連線（clientId 撞號）"
    assert len(rec["notes"]) == 2, "成敗都要回報，每帳戶一封"
    assert all("✅" in m for m in rec["notes"])


def test_ty_cold_connect_failure_sends_alert():
    from monitor_engine import ty_check_spec
    env, rec, _ = make_env(online={"Taylor": False, "Yvonne": True},
                           cold_connect_error=ConnectionError("refused"))
    for w in ty_check_spec(env).workers():
        w()
    assert rec["cold"] == ["Taylor"], "只有離線帳戶走冷連線"
    alerts = [m for m in rec["notes"] if "✅" not in m]
    assert len(alerts) == 1 and "Taylor" in alerts[0]
    assert any("TY check Taylor → ALERT" in l for l in rec["logs"])


def test_ty_monitor_not_ready_falls_back_to_cold_connect():
    from monitor_engine import ty_check_spec
    env, rec, _ = make_env(ready=False)
    for w in ty_check_spec(env, accounts=("Taylor",)).workers():
        w()
    assert rec["cold"] == ["Taylor"], "GatewayMonitor 未備妥 → 冷連線 fallback（非跳過）"


# ── Morning check spec ────────────────────────────────────────────────

def _patch_constituents(monkeypatch, last_date):
    import morning_check
    monkeypatch.setattr(morning_check, "read_constituents_last_date",
                        lambda p: last_date)


def test_morning_all_green_stays_silent(monkeypatch):
    import monitor_engine as me
    _patch_constituents(monkeypatch, IN_WINDOW.date())   # 新鮮
    env, rec, _ = make_env(online={})                    # 預設全 online
    spec = me.morning_check_spec(env)
    for w in spec.workers():
        w()
    assert any("all online" in l for l in rec["logs"])
    assert rec["notes"] == [], "全綠必須靜默（平日不洗版）"
    assert rec["sleeps"], "必須等 re-probe 沉澱"


def test_morning_offline_alerts():
    import monitor_engine as me
    env, rec, _ = make_env(online={"Rosa": False})
    # constituents 新鮮：用 monkeypatch 以外的方式 — 這裡直接依賴真檔案
    # 會引入環境依賴，所以照樣 patch。
    import morning_check
    orig = morning_check.read_constituents_last_date
    morning_check.read_constituents_last_date = lambda p: IN_WINDOW.date()
    try:
        for w in me.morning_check_spec(env).workers():
            w()
    finally:
        morning_check.read_constituents_last_date = orig
    assert len(rec["notes"]) == 1
    assert any("ALERT" in l and "Rosa" in l for l in rec["logs"])


def test_morning_stale_constituents_second_notify(monkeypatch):
    import monitor_engine as me
    _patch_constituents(monkeypatch, IN_WINDOW.date() - timedelta(days=30))
    env, rec, _ = make_env(online={})
    for w in me.morning_check_spec(env).workers():
        w()
    assert len(rec["notes"]) == 1, "staleness 警告獨立發送（gateway 全綠仍要提醒）"
    assert any("constituents stale" in l for l in rec["logs"])


def test_morning_pre_logs_then_refreshes():
    import monitor_engine as me
    env, rec, _ = make_env()
    spec = me.morning_check_spec(env)
    spec.pre()
    assert any("re-probing all Gateways" in l for l in rec["logs"])
    assert rec["refreshes"] == 1, "pre 必須跑 Check Now 等效 re-probe"


def test_morning_not_ready_skips_everything(monkeypatch):
    import monitor_engine as me
    _patch_constituents(monkeypatch, None)
    env, rec, _ = make_env(ready=False)
    for w in me.morning_check_spec(env).workers():
        w()
    assert rec["sleeps"] == [] and rec["notes"] == [] and rec["logs"] == []
