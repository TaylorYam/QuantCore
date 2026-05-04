"""
NYSE trading-day calendar + Taipei → NYSE session-date mapping.

Used by the ROSA market-window poller in control_panel:
  1. Decide whether a given Taipei datetime falls inside an active polling
     window (US RTH session that maps to a real NYSE trading day).
  2. Provide the NYSE session date that the window points at, for logging.

Behavior:
  * If `pandas_market_calendars` is importable, defer to NYSE schedule.
  * If not, fall back to a hardcoded NYSE-holiday set (warns once).

Window mapping (Taipei local time-of-day):
    21:00 – 23:59  → same Taipei calendar date (NYSE date = Taipei date)
    00:00 – 05:00  → previous Taipei calendar date (NYSE date = prev day)
    else           → outside polling window (returns None)
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

log = logging.getLogger(__name__)


# Hardcoded NYSE closures used when pandas_market_calendars unavailable.
# Source: NYSE official holiday calendar 2026 / 2027.
_HOLIDAYS_FALLBACK: frozenset[date] = frozenset({
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # Martin Luther King Jr. Day
    date(2026, 2, 16),   # Presidents Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed; July 4 is Saturday)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas Day
    # 2027
    date(2027, 1, 1),
    date(2027, 1, 18),
    date(2027, 2, 15),
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),
    date(2027, 6, 18),   # Juneteenth observed (Jun 19 is Saturday)
    date(2027, 7, 5),    # July 4 is Sunday → observed Mon
    date(2027, 9, 6),
    date(2027, 11, 25),
    date(2027, 12, 24),  # Christmas observed (Dec 25 is Saturday)
})


def _holiday_set_via_lib() -> frozenset[date] | None:
    """Try to build holiday set from pandas_market_calendars.
    Returns None if the library is unavailable / errors during load."""
    try:
        import pandas_market_calendars as mcal  # type: ignore
    except Exception:
        return None
    try:
        nyse = mcal.get_calendar("NYSE")
        # Pull a generous range so callers don't need to refresh per-year.
        sched = nyse.schedule(start_date="2026-01-01", end_date="2030-12-31")
        trading_days = {d.date() for d in sched.index}
        # Build the inverse: every weekday in range that is NOT a trading day.
        out: set[date] = set()
        cur = date(2026, 1, 1)
        end = date(2030, 12, 31)
        while cur <= end:
            if cur.weekday() < 5 and cur not in trading_days:
                out.add(cur)
            cur += timedelta(days=1)
        return frozenset(out)
    except Exception as e:
        log.warning(f"market_calendar: pandas_market_calendars load failed ({e!r})"
                    f" — falling back to hardcoded NYSE holidays")
        return None


# Resolve once at import.
_HOLIDAYS: frozenset[date] = _holiday_set_via_lib() or _HOLIDAYS_FALLBACK
if _HOLIDAYS is _HOLIDAYS_FALLBACK:
    log.info("market_calendar: using hardcoded NYSE holidays "
             "(pandas_market_calendars not installed)")


def is_nyse_trading_day(d: date) -> bool:
    """True iff NYSE has a regular session on date `d` (NY local date).
    Weekends → False. NYSE holidays → False. Half-days → True."""
    if d.weekday() >= 5:  # Sat=5, Sun=6
        return False
    return d not in _HOLIDAYS


# ── Taipei → NYSE session date mapping ─────────────────────────────

# Window edges in Taipei local time-of-day.
_WINDOW_EVENING_START = time(21, 0)   # 21:00 inclusive
_WINDOW_MORNING_END   = time(5, 0)    # 05:00 inclusive


def trading_session_date_for(now_taipei: datetime) -> date | None:
    """Map a Taipei-local datetime to the NYSE session date the ROSA poller
    cares about. Returns None when not in window OR mapped date is non-trading.

      21:00 – 23:59  → same Taipei date
      00:00 – 05:00  → previous Taipei date
      else           → None
    """
    t = now_taipei.time()
    if _WINDOW_EVENING_START <= t:                     # 21:00–23:59
        nyse_date = now_taipei.date()
    elif t <= _WINDOW_MORNING_END:                     # 00:00–05:00
        nyse_date = now_taipei.date() - timedelta(days=1)
    else:
        return None
    return nyse_date if is_nyse_trading_day(nyse_date) else None
