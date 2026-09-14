"""Account-scoped portfolio queries; no Tk, config, trading or file I/O.

Workers own their event loops. Consumers read detached snapshots or drain changed
accounts on their own thread, so workers never call a GUI after it is destroyed.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import threading
import time


@dataclass(frozen=True)
class QueryState:
    portfolio: dict | None = None
    updated_at: datetime | None = None
    running: bool = False
    error: str | None = None


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

    def refresh(self):
        started = []
        for name in self.accounts:
            with self._lock:
                previous = self._states[name]
                if self._closed or previous.running:
                    continue
                self._states[name] = QueryState(
                    previous.portfolio, previous.updated_at, True, previous.error)
                self._changed.add(name)
            try:
                threading.Thread(target=self._run, args=(name,), daemon=True,
                                 name=f"portfolio-{name}").start()
                started.append(name)
            except Exception as exc:
                self._finish(name, None, str(exc) or type(exc).__name__)
        return tuple(started)

    def _run(self, name):
        loop = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = self._fetch(name)
            if not result.get("available"):
                raise RuntimeError(result.get("error") or "Query failed (no message)")
            self._finish(name, result, None)
        except Exception as exc:
            self._finish(name, None, str(exc) or type(exc).__name__)
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

    def _finish(self, name, result, error):
        with self._lock:
            if self._closed:
                return
            previous = self._states[name]
            self._states[name] = QueryState(
                deepcopy(result) if result is not None else previous.portfolio,
                self._now() if result is not None else previous.updated_at,
                False, error)
            self._changed.add(name)

    def close(self):
        """Stop accepting work. In-flight workers clean up their own connections."""
        with self._lock:
            self._closed = True
            self._changed.clear()


class PortfolioRefreshLoop:
    """Main-thread adapter: startup + hourly refresh, independent manual refresh.

    schedule(ms, callback) and cancel(handle) can be Tk after/after_cancel or a
    fake scheduler. Polling publishes each finished Account without a batch barrier.
    """
    def __init__(self, queries, schedule, cancel, on_change, *, clock=time.monotonic):
        self.queries = queries
        self._schedule, self._cancel = schedule, cancel
        self._on_change, self._clock = on_change, clock
        self._active = False
        self._hour_timer = self._poll_timer = None

    def start(self):
        if self._active:
            return
        self._active = True
        self._next_hour = self._clock() + 3600
        self._arm_hour()
        self.refresh()
        self._poll()

    def refresh(self):
        return self.queries.refresh() if self._active else ()

    def _arm_hour(self):
        delay = max(1, math.ceil((self._next_hour - self._clock()) * 1000))
        self._hour_timer = self._schedule(delay, self._hourly)

    def _hourly(self):
        if not self._active:
            return
        now = self._clock()
        if now >= self._next_hour:
            self._next_hour += (math.floor((now - self._next_hour) / 3600) + 1) * 3600
            self.refresh()
        self._arm_hour()

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
        for timer in (self._hour_timer, self._poll_timer):
            if timer is not None:
                self._cancel(timer)
        self._hour_timer = self._poll_timer = None
        self.queries.close()
