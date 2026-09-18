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


# ── Official NYSE calendar registry (2014–2027) ─────────────────────
#
# Transcribed from the NYSE / ICE announcements below. Reference data checks
# (data/trading_calendar.py, the Wednesday Constituents refresh) compare the
# local us_trading_days.xlsx against this registry. An unscheduled closure
# (e.g. a national day of mourning) must be added here with its announcement.
# Early closes are 1:00 p.m. half days: they ARE sessions.
NYSE_SOURCES: dict[str, str] = {
    "nyse-2014-2015": "https://ir.theice.com/press/news-details/2013/NYSE-Euronext-Announces-2015-Holiday-Calendar-and-Early-Closings/default.aspx",
    "nyse-2015-2016": "https://ir.theice.com/press/news-details/2014/NYSE-Group-2016-Holiday-Calendar-and-Early-Closings/default.aspx",
    "nyse-2016-2017": "https://ir.theice.com/press/news-details/2016/NYSE-Group-Announces-2017-Holiday-and-Early-Closings-Calendar/default.aspx",
    "nyse-2018-2020": "https://ir.theice.com/press/news-details/2017/NYSE-Group-Announces-2018-2019-and-2020-Holiday-and-Early-Closings-Calendar/default.aspx",
    "nyse-2019-2021": "https://ir.theice.com/press/news-details/2018/NYSE-Group-Announces-2019-2020-and-2021-Holiday-and-Early-Closings-Calendar/default.aspx",
    "nyse-2022-2024": "https://ir.theice.com/press/news-details/2021/NYSE-Group-Announces-2022-2023-and-2024-Holiday-and-Early-Closings-Calendar/default.aspx",
    "nyse-2025-2027": "https://ir.theice.com/press/news-details/2024/NYSE-Group-Announces-2025-2026-and-2027-Holiday-and-Early-Closings-Calendar/default.aspx",
    "nyse-mourning-2018": "https://ir.theice.com/press/news-details/2018/New-York-Stock-Exchange-to-Honor-President-George-H-W-Bush/default.aspx",
    "nyse-mourning-2025": "https://ir.theice.com/press/news-details/2024/The-New-York-Stock-Exchange-Will-Close-Markets-on-January-9-to-Honor-the-Passing-of-Former-President-Jimmy-Carter-on-National-Day-of-Mourning/default.aspx",
}

_NY, _MLK, _WB, _GF = ("New Year's Day", "Martin Luther King, Jr. Day",
                       "Washington's Birthday", "Good Friday")
_MEM, _JUN, _IND = "Memorial Day", "Juneteenth", "Independence Day"
_LAB, _THX, _XMAS = "Labor Day", "Thanksgiving Day", "Christmas Day"
_OBS = " (observed)"

# year → (source, {MM-DD: closure}, [MM-DD early closes])
_REGISTRY: dict[int, tuple[str, dict[str, str], list[str]]] = {
    2014: ("nyse-2014-2015", {"01-01": _NY, "01-20": _MLK, "02-17": _WB, "04-18": _GF,
           "05-26": _MEM, "07-04": _IND, "09-01": _LAB, "11-27": _THX,
           "12-25": _XMAS}, ["07-03", "11-28", "12-24"]),
    2015: ("nyse-2014-2015", {"01-01": _NY, "01-19": _MLK, "02-16": _WB, "04-03": _GF,
           "05-25": _MEM, "07-03": _IND + _OBS, "09-07": _LAB, "11-26": _THX,
           "12-25": _XMAS}, ["11-27", "12-24"]),
    2016: ("nyse-2015-2016", {"01-01": _NY, "01-18": _MLK, "02-15": _WB, "03-25": _GF,
           "05-30": _MEM, "07-04": _IND, "09-05": _LAB, "11-24": _THX,
           "12-26": _XMAS + _OBS}, ["11-25"]),
    2017: ("nyse-2016-2017", {"01-02": _NY + _OBS, "01-16": _MLK, "02-20": _WB,
           "04-14": _GF, "05-29": _MEM, "07-04": _IND, "09-04": _LAB, "11-23": _THX,
           "12-25": _XMAS}, ["07-03", "11-24"]),
    2018: ("nyse-2018-2020", {"01-01": _NY, "01-15": _MLK, "02-19": _WB, "03-30": _GF,
           "05-28": _MEM, "07-04": _IND, "09-03": _LAB, "11-22": _THX,
           "12-25": _XMAS}, ["07-03", "11-23", "12-24"]),
    2019: ("nyse-2019-2021", {"01-01": _NY, "01-21": _MLK, "02-18": _WB, "04-19": _GF,
           "05-27": _MEM, "07-04": _IND, "09-02": _LAB, "11-28": _THX,
           "12-25": _XMAS}, ["07-03", "11-29", "12-24"]),
    2020: ("nyse-2019-2021", {"01-01": _NY, "01-20": _MLK, "02-17": _WB, "04-10": _GF,
           "05-25": _MEM, "07-03": _IND + _OBS, "09-07": _LAB, "11-26": _THX,
           "12-25": _XMAS}, ["11-27", "12-24"]),
    2021: ("nyse-2019-2021", {"01-01": _NY, "01-18": _MLK, "02-15": _WB, "04-02": _GF,
           "05-31": _MEM, "07-05": _IND + _OBS, "09-06": _LAB, "11-25": _THX,
           "12-24": _XMAS + _OBS}, ["11-26"]),
    # 2022: Jan 1 is a Saturday → no New Year's holiday observed (NYSE Rule 7.2).
    2022: ("nyse-2022-2024", {"01-17": _MLK, "02-21": _WB, "04-15": _GF,
           "05-30": _MEM, "06-20": _JUN + _OBS, "07-04": _IND, "09-05": _LAB,
           "11-24": _THX, "12-26": _XMAS + _OBS}, ["11-25"]),
    2023: ("nyse-2022-2024", {"01-02": _NY + _OBS, "01-16": _MLK, "02-20": _WB,
           "04-07": _GF, "05-29": _MEM, "06-19": _JUN, "07-04": _IND, "09-04": _LAB,
           "11-23": _THX, "12-25": _XMAS}, ["07-03", "11-24"]),
    2024: ("nyse-2022-2024", {"01-01": _NY, "01-15": _MLK, "02-19": _WB, "03-29": _GF,
           "05-27": _MEM, "06-19": _JUN, "07-04": _IND, "09-02": _LAB, "11-28": _THX,
           "12-25": _XMAS}, ["07-03", "11-29", "12-24"]),
    2025: ("nyse-2025-2027", {"01-01": _NY, "01-20": _MLK, "02-17": _WB, "04-18": _GF,
           "05-26": _MEM, "06-19": _JUN, "07-04": _IND, "09-01": _LAB, "11-27": _THX,
           "12-25": _XMAS}, ["07-03", "11-28", "12-24"]),
    2026: ("nyse-2025-2027", {"01-01": _NY, "01-19": _MLK, "02-16": _WB, "04-03": _GF,
           "05-25": _MEM, "06-19": _JUN, "07-03": _IND + _OBS, "09-07": _LAB,
           "11-26": _THX, "12-25": _XMAS}, ["11-27", "12-24"]),
    2027: ("nyse-2025-2027", {"01-01": _NY, "01-18": _MLK, "02-15": _WB, "03-26": _GF,
           "05-31": _MEM, "06-18": _JUN + _OBS, "07-05": _IND + _OBS, "09-06": _LAB,
           "11-25": _THX, "12-24": _XMAS + _OBS}, ["11-26"]),
}
_SPECIAL_CLOSURES = {
    date(2018, 12, 5): ("National Day of Mourning (George H. W. Bush)", "nyse-mourning-2018"),
    date(2025, 1, 9): ("National Day of Mourning (Jimmy Carter)", "nyse-mourning-2025"),
}

REGISTRY_FIRST = date(min(_REGISTRY), 1, 1)
REGISTRY_LAST = date(max(_REGISTRY), 12, 31)


def _day(year: int, mm_dd: str) -> date:
    return date(year, int(mm_dd[:2]), int(mm_dd[3:]))


# date → (reason, source key)
NYSE_FULL_DAY_CLOSURES: dict[date, tuple[str, str]] = {
    **{_day(y, d): (reason, src) for y, (src, closed, _) in _REGISTRY.items()
       for d, reason in closed.items()},
    **_SPECIAL_CLOSURES,
}
# date → source key (1:00 p.m. close; still a session)
NYSE_EARLY_CLOSES: dict[date, str] = {
    _day(y, d): src for y, (src, _, early) in _REGISTRY.items() for d in early}


def official_sessions(start: date, end: date) -> list[date]:
    """NYSE sessions in [start, end] per the official registry (half days
    included). Raises ValueError outside the registry years — never guesses."""
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    if start < REGISTRY_FIRST or end > REGISTRY_LAST:
        raise ValueError(f"official NYSE registry covers {REGISTRY_FIRST}..{REGISTRY_LAST}, "
                         f"not {start}..{end}; add the announced calendar first")
    out, cur = [], start
    while cur <= end:
        if cur.weekday() < 5 and cur not in NYSE_FULL_DAY_CLOSURES:
            out.append(cur)
        cur += timedelta(days=1)
    return out


# NYSE closures used when pandas_market_calendars unavailable.
_HOLIDAYS_FALLBACK: frozenset[date] = frozenset(NYSE_FULL_DAY_CLOSURES)


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
