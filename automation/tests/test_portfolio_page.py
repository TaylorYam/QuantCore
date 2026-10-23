"""Portfolio 頁的讀數來源（qt_panel.portfolio_page，#231）：不開 Qt。

假的讀取函式（成功／失敗／慢）、同步或手動控制的 post、假的計時器；斷言讀取順序、
alloc 只讀一次、變更通知、失敗留上次讀數、出入金問題去重、時段排程。
No IB, no network.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from portfolio_snapshot import PortfolioSnapshot
from qt_panel import portfolio_page as pp
from qt_panel.portfolio_page import (
    EQUITY, FLOWS, PARAMS, SNAPSHOT, TRADES, PageChange, PageFetchers, PortfolioPageSource,
)

NAMES = ("Taylor", "Rosa")
SLOT_MS = 3_600_000                                               # make_world 的排程：一小時後


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "background read did not finish"
        threading.Event().wait(0.001)


class Flows:
    def __init__(self, error=""):
        self.error = error


class Main:
    """主執行緒：post 進來的函式排隊，測試呼叫 run() 才執行（像 Qt 的事件迴圈）。"""

    def __init__(self):
        self.queue, self.lock = [], threading.Lock()

    def post(self, fn):
        with self.lock:
            self.queue.append(fn)

    def run(self):
        with self.lock:
            todo, self.queue = self.queue, []
        for fn in todo:
            fn()
        return len(todo)


class Timers:
    """假的單次計時器：記下 (ms, fn)，測試自己觸發。"""

    def __init__(self):
        self.armed, self.cancelled = [], []

    def schedule(self, ms, fn):
        handle = (ms, fn)
        self.armed.append(handle)
        return handle

    def cancel(self, handle):
        self.cancelled.append(handle)

    def poll(self):
        """觸發最近一個 100 ms 的 poll（快照變更從這裡送出）。"""
        handle = next(h for h in reversed(self.armed) if h[0] == 100)
        self.armed.remove(handle)
        handle[1]()

    def slots(self):
        """排好的時段計時器（假的排程固定排一小時後）。"""
        return [h for h in self.armed if h[0] == SLOT_MS]

    def fire_slot(self):
        """時段到了：觸發排好的那一個。"""
        handle, = self.slots()
        self.armed.remove(handle)
        handle[1]()


class Reads:
    def __init__(self):
        self.calls = []
        self.params = {"Taylor": {"alloc_sr": 0.0, "alloc_sp500": 0.5, "alloc_skewls": 1.0},
                       "Rosa": {"alloc_sr": 0.5, "alloc_sp500": 0.3, "alloc_skewls": 0.2}}
        self.flows = {n: Flows() for n in NAMES}
        self.snapshots = {n: PortfolioSnapshot(date="2026-10-22", source=f"IB Live [{n}]",
                                               available=True, nav=1.0) for n in NAMES}

    def _get(self, kind, table, name):
        self.calls.append((kind, name))
        value = table[name]
        if isinstance(value, Exception):
            raise value
        return value

    def fetchers(self):
        return PageFetchers(
            snapshot=lambda n: self._get(SNAPSHOT, self.snapshots, n),
            params=lambda n: self._get(PARAMS, self.params, n),
            trades=lambda n, alloc: self.calls.append((TRADES, n, alloc)) or [f"{n} trade"],
            equity=lambda n: self.calls.append((EQUITY, n)) or f"{n} nav",
            flows=lambda n: self._get(FLOWS, self.flows, n))


def make_world(now=lambda: datetime(2026, 10, 22, 10, 0), **fetchers):
    reads, main, timers = Reads(), Main(), Timers()
    changes, logs = [], []
    source = PortfolioPageSource(
        NAMES, replace(reads.fetchers(), **fetchers), schedule=timers.schedule,
        cancel=timers.cancel, post=main.post, on_change=changes.append, log=logs.append,
        next_refresh_time=lambda now: now + timedelta(hours=1), now=now)
    return reads, main, timers, source, changes, logs


@pytest.fixture
def world():
    w = make_world()
    yield w
    w[3].stop()


def _settle_snapshots(timers, changes, rounds=1):
    """觸發 poll 直到每個帳戶都收到第 rounds 次「讀完」的快照通知。"""
    def done():
        finished = [c.name for c in changes if c.kind == SNAPSHOT and not c.data.running]
        return all(finished.count(n) >= rounds for n in NAMES)
    deadline = time.monotonic() + 3
    while not done():
        assert time.monotonic() < deadline, "snapshot read did not finish"
        timers.poll()
        threading.Event().wait(0.001)


def _drain_extras(main, source):
    """跑主執行緒直到這一輪讀完（讀完的通知排在所有讀數之後）。"""
    def done():
        main.run()
        return not source.extras_loading()
    wait_for(done)


def _extras(changes):
    return [(c.kind, c.name) for c in changes if c.kind != SNAPSHOT]


def test_extras_are_read_in_order_and_trades_get_the_allocation_read_in_the_same_pass(world):
    reads, main, timers, source, changes, logs = world
    source.start()
    _drain_extras(main, source)
    extra_calls = [c for c in reads.calls if c[0] != SNAPSHOT]
    assert extra_calls == [
        (PARAMS, "Taylor"), (PARAMS, "Rosa"),
        (TRADES, "Taylor", (0.0, 1.0)), (TRADES, "Rosa", (0.5, 0.2)),
        (EQUITY, "Taylor"), (EQUITY, "Rosa"),
        (FLOWS, "Taylor"), (FLOWS, "Rosa"),
    ]
    assert _extras(changes) == [(k, n) for k in pp.EXTRA_ORDER for n in NAMES]
    assert logs[0] == "[GW] Portfolio refresh: next at 2026-10-22 11:00 "
    # 其他讀數全部讀到、沒有失敗：不寫 log（快照讀完的 [OK] 看執行緒快慢，可能已經有）
    assert [l for l in logs[1:] if "權益／持倉已更新" not in l] == []


def test_trades_label_with_no_allocation_when_the_params_read_fails(world):
    reads, main, timers, source, changes, logs = world
    reads.params["Rosa"] = OSError("accounts.json missing")
    source.start()
    _drain_extras(main, source)
    assert (TRADES, "Rosa", None) in reads.calls and (TRADES, "Taylor", (0.0, 1.0)) in reads.calls
    rosa = next(c for c in changes if c.kind == PARAMS and c.name == "Rosa")
    assert rosa == PageChange("Rosa", PARAMS, None, "accounts.json missing")
    assert "[X] [Rosa] 策略設定讀取失敗：accounts.json missing" in logs


def test_changes_reach_the_page_only_on_the_main_thread(world):
    reads, main, timers, source, changes, logs = world
    source.start()
    wait_for(lambda: len(main.queue) == len(pp.EXTRA_ORDER) * len(NAMES) + 1)   # 讀數 + 讀完
    assert _extras(changes) == [] and source.extras_loading()
    main.run()
    assert len(_extras(changes)) == 8 and not source.extras_loading()


def test_extras_are_not_read_twice_at_once_and_stop_drops_late_results():
    release = threading.Event()

    def flows(name):
        assert release.wait(3)                                    # 出入金下載很慢
        return Flows()

    reads, main, timers, source, changes, logs = make_world(flows=flows)
    source.start()
    assert source.refresh_extras() is False                       # 上一輪還沒讀完
    source.stop()
    release.set()
    wait_for(lambda: len(main.queue) == len(pp.EXTRA_ORDER) * len(NAMES) + 1)
    main.run()
    assert _extras(changes) == [] and source.refresh_extras() is False


def test_a_failed_snapshot_keeps_the_last_reading_and_only_that_account_is_marked(world):
    reads, main, timers, source, changes, logs = world
    source.start()
    _settle_snapshots(timers, changes)
    reads.snapshots["Rosa"] = TimeoutError()
    logs.clear()
    assert source.refresh() == NAMES
    _settle_snapshots(timers, changes, rounds=2)
    finished = {c.name: c.data for c in changes if c.kind == SNAPSHOT and not c.data.running}
    assert finished["Taylor"].error is None
    assert finished["Rosa"].error == "TimeoutError" and finished["Rosa"].portfolio.nav == 1.0
    assert logs[0] == "[>] 讀取帳戶權益與持倉：Taylor、Rosa"
    assert "[OK] [Taylor] 權益／持倉已更新" in logs
    assert "[X] [Rosa] 權益／持倉讀取失敗：TimeoutError" in logs


def test_refresh_while_queries_run_or_after_stop_starts_nothing(world):
    reads, main, timers, source, changes, logs = world
    assert source.refresh() == ()                                 # 還沒開始
    assert logs == ["[>] 帳戶查詢還在進行中"]
    source.start()
    source.stop()
    assert source.refresh() == () and source.refresh_extras() is False


def test_the_same_flow_problem_is_logged_once_per_account(world):
    reads, main, timers, source, changes, logs = world
    reads.flows["Taylor"] = Flows("Flex 逾時")
    reads.flows["Rosa"] = OSError("no token")
    source.start()
    _drain_extras(main, source)
    for _ in range(2):
        source.refresh_extras()
        _drain_extras(main, source)
    flow_logs = [l for l in logs if "出入金" in l]
    assert flow_logs == ["[!] [Taylor] 出入金下載失敗，改用快取：Flex 逾時",
                         "[X] [Rosa] 出入金讀取失敗：no token"]
    reads.flows["Taylor"] = Flows()                               # 好了 → 再壞要再記
    source.refresh_extras()
    _drain_extras(main, source)
    reads.flows["Taylor"] = Flows("Flex 逾時")
    source.refresh_extras()
    _drain_extras(main, source)
    assert [l for l in logs if "Taylor] 出入金" in l] == [flow_logs[0]] * 2


@pytest.mark.parametrize("error, result, expected", [
    ("boom", None, "[X] [Taylor] 出入金讀取失敗：boom"),
    ("", Flows("timeout"), "[!] [Taylor] 出入金下載失敗，改用快取：timeout"),
    ("", Flows(), ""),
])
def test_flow_problem_text(error, result, expected):
    assert pp.flow_problem("Taylor", error, result) == expected


@pytest.mark.parametrize("params, expected", [
    ({"alloc_sr": 0.8, "alloc_sp500": 0.5}, (0.8, 0.0)),
    ({}, (0.0, 0.0)),
    (None, None),
    ({"alloc_sr": "x"}, None),
])
def test_trade_alloc_is_the_sr_and_skewls_pair(params, expected):
    assert pp.trade_alloc(params) == expected


def test_read_params_publishes_on_the_main_thread_and_raises_on_failure(world):
    reads, main, timers, source, changes, logs = world
    source.start()
    _drain_extras(main, source)
    changes.clear()
    assert source.read_params("Taylor") == reads.params["Taylor"]
    assert changes == [PageChange("Taylor", PARAMS, reads.params["Taylor"])]
    reads.params["Taylor"] = OSError()
    with pytest.raises(OSError):
        source.read_params("Taylor")
    assert changes[-1] == PageChange("Taylor", PARAMS, None, "OSError")
    assert logs[-1] == "[X] [Taylor] 策略設定讀取失敗：OSError"


def test_snapshots_follow_the_market_hours_schedule(world):
    reads, main, timers, source, changes, logs = world
    source.start()
    assert len(timers.slots()) == 1                               # 下一個時段：一小時後
    _settle_snapshots(timers, changes)
    reads.calls.clear()
    timers.fire_slot()                                            # 時段到了
    wait_for(lambda: sorted(c[1] for c in reads.calls if c[0] == SNAPSHOT) == sorted(NAMES))


def test_other_readings_are_read_again_at_each_scheduled_slot(world):
    """Panel 開著跨日：排程時段到時權益紀錄等也重讀，跟按「重新讀取 IB」一樣（#241）。"""
    reads, main, timers, source, changes, logs = world
    source.start()
    _drain_extras(main, source)
    for _ in range(2):
        reads.calls.clear()
        changes.clear()
        timers.fire_slot()
        assert source.extras_loading()                            # 時段當下就開始這一輪
        _drain_extras(main, source)
        assert [c for c in reads.calls if c[0] != SNAPSHOT] == [
            (PARAMS, "Taylor"), (PARAMS, "Rosa"),
            (TRADES, "Taylor", (0.0, 1.0)), (TRADES, "Rosa", (0.5, 0.2)),
            (EQUITY, "Taylor"), (EQUITY, "Rosa"),
            (FLOWS, "Taylor"), (FLOWS, "Rosa"),
        ]
        assert _extras(changes) == [(k, n) for k in pp.EXTRA_ORDER for n in NAMES]


def test_a_slot_while_the_last_pass_is_still_reading_starts_no_second_pass():
    release = threading.Event()
    flow_calls = []

    def flows(name):
        flow_calls.append(name)
        assert release.wait(3)                                    # 出入金下載很慢
        return Flows()

    reads, main, timers, source, changes, logs = make_world(flows=flows)
    try:
        source.start()
        timers.fire_slot()                                        # 開啟時那一輪還沒讀完
        assert source.extras_loading() and source.refresh_extras() is False
        release.set()
        _drain_extras(main, source)
        assert flow_calls == list(NAMES)                          # 只讀了一輪
    finally:
        release.set()
        source.stop()


def test_a_late_slot_starts_no_other_readings():
    """電腦睡眠醒來、時段晚到超過 5 分鐘：其他讀數跟快照一樣略過這一輪，只排下一個時段。
    （快照略過的規則在 test_portfolio_queries 的 late timer 題。）"""
    clock = {"now": datetime(2026, 10, 22, 10, 0)}
    reads, main, timers, source, changes, logs = make_world(now=lambda: clock["now"])
    try:
        source.start()
        _drain_extras(main, source)
        clock["now"] = datetime(2026, 10, 22, 11, 6)              # 11:00 的時段晚了 6 分鐘
        timers.fire_slot()
        assert not source.extras_loading() and main.queue == []   # 沒有開始新的一輪
        assert len(timers.slots()) == 1                           # 下一個時段照排
    finally:
        source.stop()


def test_the_page_rules_do_not_need_qt():
    """讀數來源、Allocation 流程、Gateway 轉換規則都不 import PySide6。"""
    automation = Path(pp.__file__).resolve().parent.parent
    code = (f"import sys; sys.path.insert(0, {str(automation)!r}); "
            "import qt_panel.portfolio_page, qt_panel.allocation_flow, qt_panel.gateway_status; "
            "print(any(m.startswith('PySide6') for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"
