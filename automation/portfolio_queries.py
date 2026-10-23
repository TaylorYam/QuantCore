"""Account-scoped portfolio queries; no GUI, config, trading or file I/O.

Workers own their event loops. Consumers read detached snapshots or drain changed
accounts on their own thread, so workers never call a GUI after it is destroyed.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from portfolio_snapshot import PortfolioSnapshot


@dataclass(frozen=True)
class QueryState:
    portfolio: PortfolioSnapshot | None = None
    updated_at: datetime | None = None
    running: bool = False
    error: str | None = None
    refresh_source: str | None = None


class PortfolioQueries:
    def __init__(self, accounts, fetch, *, now=lambda: datetime.now(timezone.utc)):
        self.accounts = tuple(dict.fromkeys(accounts))
        self._fetch = fetch
        self._now = now
        self._lock = threading.Lock()
        self._states = {name: QueryState() for name in self.accounts}
        self._changed = set()
        self._closed = False

    def snapshot(self, name):
        with self._lock:
            return deepcopy(self._states[name])

    def drain_changes(self):
        with self._lock:
            changes = {name: deepcopy(self._states[name])
                       for name in self.accounts if name in self._changed}
            self._changed.clear()
            return changes

    def refresh(self, *, source="manual"):
        started = []
        for name in self.accounts:
            with self._lock:
                previous = self._states[name]
                if self._closed or previous.running:
                    continue
                self._states[name] = QueryState(
                    previous.portfolio, previous.updated_at, True, previous.error,
                    previous.refresh_source)
                self._changed.add(name)
            try:
                threading.Thread(target=self._run, args=(name, source),
                                 daemon=True,
                                 name=f"portfolio-{name}").start()
                started.append(name)
            except Exception as exc:
                self._finish(name, None, str(exc) or type(exc).__name__, source)
        return tuple(started)

    def _run(self, name, source="manual"):
        loop = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = self._fetch(name)
            if not result.available:
                raise RuntimeError(result.error or "Query failed (no message)")
            self._finish(name, result, None, source)
        except Exception as exc:
            self._finish(name, None, str(exc) or type(exc).__name__, source)
        finally:
            if loop is not None:
                try:
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    loop.run_until_complete(loop.shutdown_asyncgens())
                finally:
                    loop.close()
                    asyncio.set_event_loop(None)

    def _finish(self, name, result, error, source=None):
        with self._lock:
            if self._closed:
                return
            previous = self._states[name]
            self._states[name] = QueryState(
                deepcopy(result) if result is not None else previous.portfolio,
                self._now() if result is not None else previous.updated_at,
                False, error,
                source if result is not None else previous.refresh_source)
            self._changed.add(name)

    def close(self):
        """Stop accepting work. In-flight workers clean up their own connections."""
        with self._lock:
            self._closed = True
            self._changed.clear()


class PortfolioRefreshLoop:
    """Main-thread adapter: startup + market-hours scheduled refresh.

    Refresh fires at fixed NYSE trading-hour slots (09:30, 10:00, …, 16:00 ET)
    instead of a monotonic hourly interval.  Startup still does one immediate
    refresh.  Manual refresh is independent of the schedule.
    on_scheduled_refresh() runs after each on-time slot refresh (not at
    startup, not for manual or late slots) so callers can re-read other data
    on the same schedule.

    schedule(ms, callback) and cancel(handle) can be single-shot QTimers or a
    fake scheduler.  Polling publishes each finished Account without a batch
    barrier.
    """
    _LATE_THRESHOLD_SEC = 300

    def __init__(self, queries, schedule, cancel, on_change, *,
                 next_refresh_time=None, now=None, log=None,
                 on_scheduled_refresh=None):
        self.queries = queries
        self._schedule, self._cancel = schedule, cancel
        self._on_change = on_change
        self._on_scheduled_refresh = on_scheduled_refresh
        self._log = log
        self._next_refresh_time = next_refresh_time
        self._now = now
        self._active = False
        self._sched_timer = self._poll_timer = None
        self._armed_for: datetime | None = None

    def _ensure_defaults(self):
        if self._next_refresh_time is None:
            from refresh_schedule import next_refresh_time
            self._next_refresh_time = next_refresh_time
        if self._now is None:
            from zoneinfo import ZoneInfo
            _ET = ZoneInfo("America/New_York")
            self._now = lambda: datetime.now(_ET)

    def start(self):
        if self._active:
            return
        self._ensure_defaults()
        self._active = True
        self._arm_next()
        self.refresh(source="auto")
        self._poll()

    def refresh(self, *, source="manual"):
        return self.queries.refresh(source=source) if self._active else ()

    def _arm_next(self):
        now = self._now()
        try:
            nxt = self._next_refresh_time(now)
        except Exception as exc:
            # 排不出下一個時段（例如 NYSE 假日表用完，#234）：只停自動刷新，
            # 手動刷新照常；告警由值班（Morning check 提醒／停止排程通知）負責。
            self._armed_for = self._sched_timer = None
            if self._log is not None:
                self._log(f"[GW] Portfolio refresh: cannot schedule next refresh — {exc}")
            return
        self._armed_for = nxt
        delay_ms = max(1, int((nxt - now).total_seconds() * 1000))
        self._sched_timer = self._schedule(delay_ms, self._scheduled_tick)
        if self._log is not None:
            self._log(f"[GW] Portfolio refresh: next at "
                      f"{nxt.strftime('%Y-%m-%d %H:%M %Z')}")

    def _scheduled_tick(self):
        if not self._active:
            return
        now = self._now()
        if self._armed_for is not None:
            late_sec = (now - self._armed_for).total_seconds()
            if late_sec <= self._LATE_THRESHOLD_SEC:
                self.refresh(source="auto")
                if self._on_scheduled_refresh is not None:
                    self._on_scheduled_refresh()
        self._arm_next()

    def _poll(self):
        if not self._active:
            return
        try:
            for name, state in self.queries.drain_changes().items():
                if not self._active:
                    break
                self._on_change(name, state)
        finally:
            if self._active:
                self._poll_timer = self._schedule(100, self._poll)

    def stop(self):
        self._active = False
        for timer in (self._sched_timer, self._poll_timer):
            if timer is not None:
                self._cancel(timer)
        self._sched_timer = self._poll_timer = None
        self.queries.close()
