"""
NYSE market-hours portfolio refresh schedule.

Pure function: given the current ET datetime, return the next scheduled
refresh time.  No I/O, no Tk, no IB — testable in milliseconds.

Schedule on each NYSE trading day:
    09:30, 10:00, 11:00, 12:00, 13:00, 14:00, 15:00, 16:00

Early-close days (13:00 ET close) trim the tail:
    09:30, 10:00, 11:00, 12:00, 13:00

Non-trading days (weekends + NYSE holidays) are skipped entirely.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from market_calendar import NYSE_EARLY_CLOSES, is_nyse_trading_day

_MAX_SCAN_DAYS = 14

_REGULAR_CLOSE = time(16, 0)
_EARLY_CLOSE = time(13, 0)

_SLOTS_REGULAR = (
    time(9, 30),
    time(10, 0),
    time(11, 0),
    time(12, 0),
    time(13, 0),
    time(14, 0),
    time(15, 0),
    time(16, 0),
)

_SLOTS_EARLY = tuple(s for s in _SLOTS_REGULAR if s <= _EARLY_CLOSE)

# ── Early-close detection ───────────────────────────────────────────
#
# Keep the schedule tied to the official registry in market_calendar.  That
# module owns the NYSE holiday and early-close dates, including its offline
# fallback, so this caller cannot drift from the rest of the application.
_EARLY_CLOSE_DATES: frozenset[date] = frozenset(NYSE_EARLY_CLOSES)


def is_early_close(d: date) -> bool:
    return d in _EARLY_CLOSE_DATES


def refresh_slots(d: date) -> tuple[time, ...]:
    """Scheduled refresh times for date *d*. Empty if non-trading."""
    if not is_nyse_trading_day(d):
        return ()
    return _SLOTS_EARLY if is_early_close(d) else _SLOTS_REGULAR


def next_refresh_time(now_et: datetime) -> datetime:
    """Next strictly-future scheduled refresh time (ET-aware datetime).

    Scans remaining slots today, then forward through the next trading
    days.  Raises RuntimeError if no trading day is found within
    ``_MAX_SCAN_DAYS``.
    """
    today = now_et.date()
    tz = now_et.tzinfo
    for slot in refresh_slots(today):
        candidate = now_et.replace(
            hour=slot.hour, minute=slot.minute, second=0, microsecond=0)
        if candidate > now_et:
            return candidate

    d = today + timedelta(days=1)
    for _ in range(_MAX_SCAN_DAYS):
        slots = refresh_slots(d)
        if slots:
            return datetime.combine(d, slots[0], tzinfo=tz)
        d += timedelta(days=1)

    raise RuntimeError(
        f"next_refresh_time: no NYSE trading day within "
        f"{_MAX_SCAN_DAYS} days of {now_et.isoformat()}"
    )
