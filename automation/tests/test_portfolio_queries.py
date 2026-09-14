"""Offline concurrency tests use barriers/events, not API calls or timing guesses."""
import asyncio
from datetime import datetime, timezone
import threading
import time

from portfolio_queries import PortfolioQueries, PortfolioRefreshLoop

ACCOUNTS = ("Taylor", "Rosa", "Yvonne")


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, "worker did not complete"
        threading.Event().wait(0.001)


def test_parallel_partial_results_dedup_and_close():
    entered = {n: threading.Event() for n in ACCOUNTS}
    release = {n: threading.Event() for n in ACCOUNTS}
    loops = {}
    calls = []

    def fetch(name):
        calls.append(name)
        loops[name] = asyncio.get_event_loop()
        entered[name].set()
        assert release[name].wait(3)
        if name == "Rosa":
            raise TimeoutError()
        return {"available": True, "nav": 10, "account": name}

    queries = PortfolioQueries(ACCOUNTS, fetch)
    try:
        assert queries.refresh() == ACCOUNTS
        assert all(event.wait(3) for event in entered.values())
        assert len({id(loop) for loop in loops.values()}) == 3
        assert queries.refresh() == ()
        release["Taylor"].set()
        wait_for(lambda: not queries.snapshot("Taylor").running)
        assert queries.snapshot("Taylor").portfolio["account"] == "Taylor"
        assert queries.snapshot("Rosa").running
        assert queries.snapshot("Yvonne").running
        release["Rosa"].set()
        wait_for(lambda: not queries.snapshot("Rosa").running)
        assert queries.snapshot("Rosa").error == "TimeoutError"
        assert queries.snapshot("Rosa").portfolio is None
        # In-flight Yvonne is skipped; the two idle Accounts can be refreshed.
        assert queries.refresh() == ("Taylor", "Rosa")
        wait_for(lambda: not queries.snapshot("Taylor").running)
        wait_for(lambda: not queries.snapshot("Rosa").running)
        queries.close()
        release["Yvonne"].set()
        wait_for(lambda: loops["Yvonne"].is_closed())
        assert queries.drain_changes() == {}
        assert queries.refresh() == ()
        assert calls.count("Yvonne") == 1
    finally:
        for event in release.values():
            event.set()
        queries.close()


def test_failure_retains_success_timestamp_and_detached_data():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    results = iter([{"available": True, "nav": 100, "holdings": ["XLK"]},
                    {"available": False, "error": ""},
                    {"available": True, "nav": 200}])
    queries = PortfolioQueries(["Taylor"], lambda _: next(results), now=lambda: now)
    try:
        queries.refresh()
        wait_for(lambda: not queries.snapshot("Taylor").running)
        saved = queries.snapshot("Taylor")
        saved.portfolio["holdings"].clear()
        assert queries.snapshot("Taylor").portfolio["holdings"] == ["XLK"]
        queries.refresh()
        wait_for(lambda: not queries.snapshot("Taylor").running)
        failed = queries.snapshot("Taylor")
        assert failed.error and failed.portfolio["nav"] == 100
        assert failed.updated_at == now
        queries.refresh()
        wait_for(lambda: not queries.snapshot("Taylor").running)
        recovered = queries.snapshot("Taylor")
        assert recovered.error is None and recovered.portfolio["nav"] == 200
    finally:
        queries.close()


class Scheduler:
    def __init__(self):
        self.now = 0
        self.jobs = {}
        self.sequence = 0

    def schedule(self, ms, fn):
        self.sequence += 1
        self.jobs[self.sequence] = (self.now + ms / 1000, fn)
        return self.sequence

    def cancel(self, handle):
        self.jobs.pop(handle, None)

    def advance(self, seconds):
        target = self.now + seconds
        while self.jobs:
            key = min(self.jobs, key=lambda k: self.jobs[k][0])
            when, fn = self.jobs[key]
            if when > target:
                break
            self.now = when
            del self.jobs[key]
            fn()
        self.now = target


def test_hourly_manual_and_shutdown_without_wall_clock_wait():
    from types import SimpleNamespace
    scheduler = Scheduler()
    calls, published = [], []
    changes = {"Taylor": "ready"}
    queries = SimpleNamespace(
        refresh=lambda: calls.append(scheduler.now),
        drain_changes=lambda: dict(changes), close=lambda: changes.clear())
    loop = PortfolioRefreshLoop(queries, scheduler.schedule, scheduler.cancel,
                                lambda *args: published.append(args),
                                clock=lambda: scheduler.now)
    loop.start()
    loop.start()
    assert calls == [0] and published == [("Taylor", "ready")]
    changes.clear()
    scheduler.advance(1200)
    loop.refresh()
    scheduler.advance(2400)
    assert calls == [0, 1200, 3600]
    scheduler.advance(3600)
    assert calls == [0, 1200, 3600, 7200]
    loop.stop()
    assert not scheduler.jobs
    loop._hourly()  # even a late timer is inert
    loop._poll()
    loop.refresh()
    assert calls == [0, 1200, 3600, 7200]


def test_resume_after_sleep_skips_missed_hours():
    from types import SimpleNamespace
    scheduler = Scheduler()
    calls = []
    queries = SimpleNamespace(refresh=lambda: calls.append(scheduler.now),
                              drain_changes=lambda: {}, close=lambda: None)
    loop = PortfolioRefreshLoop(queries, scheduler.schedule, scheduler.cancel,
                                lambda *_: None, clock=lambda: scheduler.now)
    loop.start()
    scheduler.now = 3 * 3600 + 100
    loop._hourly()
    assert calls == [0, 10900]
    assert scheduler.jobs[loop._hour_timer][0] == 14400
    loop.stop()
