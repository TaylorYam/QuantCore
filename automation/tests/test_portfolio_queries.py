"""Offline concurrency tests use barriers/events, not API calls or timing guesses."""
import asyncio
from datetime import datetime, timezone
import threading
import time

from portfolio_queries import PortfolioQueries, PortfolioRefreshLoop
from portfolio_snapshot import PortfolioSnapshot

ACCOUNTS = ("Taylor", "Rosa", "Yvonne")



def snap(**kw):
    kw.setdefault("source", "IB Live [Taylor]")
    return PortfolioSnapshot(date="2026-01-01", available=True, **kw)


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
        return snap(nav=10, source=f"IB Live [{name}]")

    queries = PortfolioQueries(ACCOUNTS, fetch)
    try:
        assert queries.refresh() == ACCOUNTS
        assert all(event.wait(3) for event in entered.values())
        assert len({id(loop) for loop in loops.values()}) == 3
        assert queries.refresh() == ()
        release["Taylor"].set()
        wait_for(lambda: not queries.snapshot("Taylor").running)
        assert queries.snapshot("Taylor").portfolio.source == "IB Live [Taylor]"
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
    results = iter([snap(nav=100, manual_symbols={"XLK": "手動"}),
                    PortfolioSnapshot.unavailable(""),
                    snap(nav=200)])
    queries = PortfolioQueries(["Taylor"], lambda _: next(results), now=lambda: now)
    try:
        queries.refresh()
        wait_for(lambda: not queries.snapshot("Taylor").running)
        saved = queries.snapshot("Taylor")
        saved.portfolio.manual_symbols.clear()
        assert queries.snapshot("Taylor").portfolio.manual_symbols == {"XLK": "手動"}
        queries.refresh()
        wait_for(lambda: not queries.snapshot("Taylor").running)
        failed = queries.snapshot("Taylor")
        assert failed.error and failed.portfolio.nav == 100
        assert failed.updated_at == now
        queries.refresh()
        wait_for(lambda: not queries.snapshot("Taylor").running)
        recovered = queries.snapshot("Taylor")
        assert recovered.error is None and recovered.portfolio.nav == 200
    finally:
        queries.close()


from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _et(y, m, d, h, mi=0, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=ET)


class Scheduler:
    """Fake scheduler that works with wall-clock ET datetimes."""
    def __init__(self, start: datetime):
        self.now_dt = start
        self.jobs: dict[int, tuple[datetime, object]] = {}
        self.sequence = 0

    def now(self) -> datetime:
        return self.now_dt

    def schedule(self, ms, fn):
        self.sequence += 1
        fire_at = self.now_dt + timedelta(milliseconds=ms)
        self.jobs[self.sequence] = (fire_at, fn)
        return self.sequence

    def cancel(self, handle):
        self.jobs.pop(handle, None)

    def advance_to(self, target: datetime):
        while self.jobs:
            key = min(self.jobs, key=lambda k: self.jobs[k][0])
            when, fn = self.jobs[key]
            if when > target:
                break
            self.now_dt = when
            del self.jobs[key]
            fn()
        self.now_dt = target

    def advance(self, seconds: float):
        self.advance_to(self.now_dt + timedelta(seconds=seconds))


def _fixed_schedule(*slots):
    """Return a next_refresh_time function that cycles through fixed ET times."""
    it = iter(slots)
    def nrt(now_et):
        for s in it:
            if s > now_et:
                return s
        raise RuntimeError("no more slots")
    return nrt


def test_scheduled_refresh_fires_at_market_slots():
    from types import SimpleNamespace
    # Mon 2026-09-14 trading day: slots at 09:30, 10:00, 11:00
    t0 = _et(2026, 9, 14, 9, 0)
    slots = [_et(2026, 9, 14, 9, 30),
             _et(2026, 9, 14, 10, 0),
             _et(2026, 9, 14, 11, 0),
             _et(2026, 9, 14, 12, 0)]
    scheduler = Scheduler(t0)
    calls, published = [], []
    changes = {"Taylor": "ready"}
    queries = SimpleNamespace(
        refresh=lambda **kw: calls.append(scheduler.now()),
        drain_changes=lambda: dict(changes), close=lambda: changes.clear())
    loop = PortfolioRefreshLoop(
        queries, scheduler.schedule, scheduler.cancel,
        lambda *args: published.append(args),
        next_refresh_time=_fixed_schedule(*slots),
        now=scheduler.now)
    loop.start()
    loop.start()  # idempotent
    # Startup immediate refresh
    assert len(calls) == 1 and calls[0] == t0
    assert published == [("Taylor", "ready")]
    changes.clear()
    # Advance to 09:30 → scheduled tick fires
    scheduler.advance_to(_et(2026, 9, 14, 9, 30))
    assert len(calls) == 2 and calls[1] == slots[0]
    # Manual refresh at 09:45 — independent of schedule
    scheduler.advance_to(_et(2026, 9, 14, 9, 45))
    loop.refresh()
    assert len(calls) == 3 and calls[2] == _et(2026, 9, 14, 9, 45)
    # 10:00 fires
    scheduler.advance_to(_et(2026, 9, 14, 10, 0))
    assert len(calls) == 4 and calls[3] == slots[1]
    loop.stop()
    assert not scheduler.jobs
    loop._scheduled_tick()  # inert after stop
    loop._poll()
    loop.refresh()
    assert len(calls) == 4


def test_late_timer_skips_refresh():
    """Timer fires >5 min late (system slept) → skip refresh, just re-arm."""
    from types import SimpleNamespace
    t0 = _et(2026, 9, 14, 9, 0)
    # Schedule 09:30, then 14:00 (we'll simulate sleep skipping several slots)
    slots = [_et(2026, 9, 14, 9, 30),
             _et(2026, 9, 14, 14, 0),
             _et(2026, 9, 14, 15, 0)]
    scheduler = Scheduler(t0)
    calls = []
    queries = SimpleNamespace(
        refresh=lambda **kw: calls.append(scheduler.now()),
        drain_changes=lambda: {}, close=lambda: None)
    loop = PortfolioRefreshLoop(
        queries, scheduler.schedule, scheduler.cancel,
        lambda *_: None,
        next_refresh_time=_fixed_schedule(*slots),
        now=scheduler.now)
    loop.start()
    assert len(calls) == 1  # startup
    # Simulate system sleep: jump from 09:00 straight to 13:50
    # The 09:30 timer fires at 13:50 — 4h20m late → skip refresh
    scheduler.now_dt = _et(2026, 9, 14, 13, 50)
    # Manually fire the pending 09:30 timer
    pending = list(scheduler.jobs.items())
    for key, (when, fn) in pending:
        if when <= _et(2026, 9, 14, 9, 31):  # the 09:30 timer
            del scheduler.jobs[key]
            fn()
    assert len(calls) == 1  # no catch-up refresh
    # Now armed for 14:00; advance normally
    scheduler.advance_to(_et(2026, 9, 14, 14, 0))
    assert len(calls) == 2  # fires on time
    loop.stop()


def test_late_timer_within_threshold_still_fires():
    """Timer fires 2 min late → within threshold → refresh happens."""
    from types import SimpleNamespace
    t0 = _et(2026, 9, 14, 9, 0)
    slots = [_et(2026, 9, 14, 9, 30), _et(2026, 9, 14, 10, 0)]
    scheduler = Scheduler(t0)
    calls = []
    queries = SimpleNamespace(
        refresh=lambda **kw: calls.append(scheduler.now()),
        drain_changes=lambda: {}, close=lambda: None)
    loop = PortfolioRefreshLoop(
        queries, scheduler.schedule, scheduler.cancel,
        lambda *_: None,
        next_refresh_time=_fixed_schedule(*slots),
        now=scheduler.now)
    loop.start()
    assert len(calls) == 1
    # Timer fires 2 min late
    scheduler.now_dt = _et(2026, 9, 14, 9, 32)
    pending = list(scheduler.jobs.items())
    for key, (when, fn) in pending:
        if when <= _et(2026, 9, 14, 9, 31):
            del scheduler.jobs[key]
            fn()
    assert len(calls) == 2  # refresh happened (within 5-min threshold)
    loop.stop()
