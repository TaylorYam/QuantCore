"""
NYSE Sessions：哪天開盤、盤中時鐘、最近收完的 session — 全專案只問這裡（#234）。

日層只有一個答案：下面的官方登錄表（2014–2027，逐年附 NYSE／ICE 公告）。
不看環境裝了什麼套件；超出登錄表範圍一律 raise ValueError，不猜「應該是交易日」。
登錄表快用完時 Morning check 會提醒（registry_horizon）。

  is_nyse_trading_day(d)        日期 d 有沒有 session（提前收盤日也算）
  official_sessions(start, end) [start, end] 內的 session 清單
  previous_session(d)           d 之前（不含 d）最近的 session
  session_clock(d)              d 的盤前／開盤／收盤／盤後結束時間（美東；提前收盤日 13:00）
  session_phase(now)            now 是 PRE_MARKET／REGULAR／AFTER_HOURS 或 None
  last_completed_session(now)   收盤時間已過的最近 session（手動單的 [[前收 (Reference Close)]]）
  trading_session_date_for(now) 台北時間 → Rosa 輪詢窗對應的 NYSE session 日

吃 aware datetime 的 session_phase／last_completed_session 是下單用的閘門：
naive、非交易日、超出登錄表都回 None（fail closed），不 raise。

Rosa 輪詢窗（台北時間）：
    21:00 – 23:59  → 同一個台北日期（NYSE date = Taipei date）
    00:00 – 05:00  → 前一個台北日期（NYSE date = prev day）
    else           → 不在輪詢窗（回 None）

另外兩個 session 來源 data/reference/us_trading_days.xlsx 與 SPY.csv 的 index
由 data/trading_calendar.py check／Constituents refresh 對照這份登錄表核對。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


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


def registry_horizon() -> date:
    """登錄表涵蓋的最後一天；之後的日期一律 raise，要先補上 NYSE 公告的年度假日表。"""
    return REGISTRY_LAST


def _check_covered(start: date, end: date) -> None:
    if start < REGISTRY_FIRST or end > REGISTRY_LAST:
        raise ValueError(f"official NYSE registry covers {REGISTRY_FIRST}..{REGISTRY_LAST}, "
                         f"not {start}..{end}; add the announced calendar first")


def is_nyse_trading_day(d: date) -> bool:
    """True iff NYSE has a session on date `d` (NY local date).
    Weekends → False. NYSE holidays → False. Half-days → True.
    Raises ValueError outside the registry years — never guesses."""
    _check_covered(d, d)
    return d.weekday() < 5 and d not in NYSE_FULL_DAY_CLOSURES


def official_sessions(start: date, end: date) -> list[date]:
    """NYSE sessions in [start, end] per the official registry (half days
    included). Raises ValueError outside the registry years — never guesses."""
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    _check_covered(start, end)
    out, cur = [], start
    while cur <= end:
        if is_nyse_trading_day(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


def previous_session(d: date) -> date:
    """Most recent NYSE session strictly before date `d`.
    Raises ValueError when that would reach outside the registry."""
    cur = d - timedelta(days=1)
    while not is_nyse_trading_day(cur):
        cur -= timedelta(days=1)
    return cur


# ── 盤中時鐘（美東）─────────────────────────────────────────────────

ET = ZoneInfo("America/New_York")

REGULAR = "REGULAR"
PRE_MARKET = "PRE_MARKET"
AFTER_HOURS = "AFTER_HOURS"


@dataclass(frozen=True)
class SessionClock:
    """一個 session 日的美東時鐘：盤前 [pre_market_open, regular_open)、
    一般時段 [regular_open, regular_close)、盤後 [regular_close, after_hours_end)。"""
    pre_market_open: time
    regular_open: time
    regular_close: time
    after_hours_end: time


_REGULAR_DAY = SessionClock(time(4), time(9, 30), time(16), time(20))
_EARLY_CLOSE_DAY = SessionClock(time(4), time(9, 30), time(13), time(17))


def is_early_close(d: date) -> bool:
    """官方 1:00 p.m. 提前收盤日（仍是 session）。"""
    return d in NYSE_EARLY_CLOSES


def session_clock(d: date) -> SessionClock | None:
    """d 的美東時鐘；非 session 日回 None。超出登錄表 raise ValueError。"""
    if not is_nyse_trading_day(d):
        return None
    return _EARLY_CLOSE_DAY if is_early_close(d) else _REGULAR_DAY


def _eastern_clock(now: datetime) -> tuple[datetime, SessionClock] | None:
    """(美東時間, 當天時鐘)；naive、非 session 日、超出登錄表 → None（fail closed）。"""
    if now.tzinfo is None:
        return None
    eastern = now.astimezone(ET)
    try:
        clock = session_clock(eastern.date())
    except ValueError:
        return None
    return (eastern, clock) if clock is not None else None


def session_phase(now: datetime) -> str | None:
    """PRE_MARKET／REGULAR／AFTER_HOURS（含提前收盤日）；其餘時間、naive、
    非 session 日、超出登錄表 → None。隔夜時段由 IB 交易時間決定，不在這裡。"""
    found = _eastern_clock(now)
    if found is None:
        return None
    eastern, clock = found
    t = eastern.time()
    if clock.pre_market_open <= t < clock.regular_open:
        return PRE_MARKET
    if clock.regular_open <= t < clock.regular_close:
        return REGULAR
    if clock.regular_close <= t < clock.after_hours_end:
        return AFTER_HOURS
    return None


def last_completed_session(now: datetime) -> date | None:
    """The latest NYSE session whose regular close is at or before now.

    Today once it has closed (after hours), otherwise the previous session.
    None for naive times and dates beyond the official registry.
    """
    found = _eastern_clock(now)
    if found is not None:
        eastern, clock = found
        if eastern.time() >= clock.regular_close:
            return eastern.date()
    if now.tzinfo is None:
        return None
    try:
        return previous_session(now.astimezone(ET).date())
    except ValueError:
        return None


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
