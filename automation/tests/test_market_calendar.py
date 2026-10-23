"""
Unit tests for market_calendar — the one NYSE Sessions module (#234).

Coverage:
  - is_nyse_trading_day: weekend / weekday / holiday classification；與
    official_sessions 在 2014–2027 每一天一致；超出登錄表 raise；
    裝了 pandas_market_calendars 也不會改變答案
  - previous_session（16:00 日度風控的 Reference Close 預期日）
  - 盤中時鐘：session_clock／session_phase（DST 切換、提前收盤、週末）
  - last_completed_session（手動單的前收）
  - trading_session_date_for: Taipei → NYSE session-date mapping with window

No network / no IB needed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


TAIPEI = ZoneInfo("Asia/Taipei")
EASTERN = ZoneInfo("America/New_York")
AUTOMATION = Path(__file__).resolve().parent.parent


# ── is_nyse_trading_day ────────────────────────────────────────────

def test_weekday_non_holiday_is_trading_day():
    from market_calendar import is_nyse_trading_day
    # Mon May 4 2026, no NYSE holiday
    assert is_nyse_trading_day(date(2026, 5, 4)) is True


def test_saturday_is_not_trading_day():
    from market_calendar import is_nyse_trading_day
    assert is_nyse_trading_day(date(2026, 5, 9)) is False  # Saturday


def test_sunday_is_not_trading_day():
    from market_calendar import is_nyse_trading_day
    assert is_nyse_trading_day(date(2026, 5, 10)) is False  # Sunday


def test_memorial_day_is_not_trading_day():
    from market_calendar import is_nyse_trading_day
    assert is_nyse_trading_day(date(2026, 5, 25)) is False


def test_christmas_is_not_trading_day():
    from market_calendar import is_nyse_trading_day
    assert is_nyse_trading_day(date(2026, 12, 25)) is False


def test_new_year_is_not_trading_day():
    from market_calendar import is_nyse_trading_day
    assert is_nyse_trading_day(date(2026, 1, 1)) is False


def test_thanksgiving_is_not_trading_day():
    from market_calendar import is_nyse_trading_day
    assert is_nyse_trading_day(date(2026, 11, 26)) is False


def test_july_4_observed_2026_is_not_trading_day():
    """July 4 2026 is a Saturday → NYSE observes the holiday on Fri Jul 3."""
    from market_calendar import is_nyse_trading_day
    assert is_nyse_trading_day(date(2026, 7, 3)) is False
    # Saturday is non-trading anyway
    assert is_nyse_trading_day(date(2026, 7, 4)) is False


# ── 一個答案：登錄表 ────────────────────────────────────────────────

def test_trading_day_agrees_with_official_sessions_every_day_of_the_registry():
    import market_calendar as mc
    sessions = set(mc.official_sessions(mc.REGISTRY_FIRST, mc.REGISTRY_LAST))
    d = mc.REGISTRY_FIRST
    while d <= mc.REGISTRY_LAST:
        assert mc.is_nyse_trading_day(d) is (d in sessions), d
        d += timedelta(days=1)


def test_mourning_closures_are_not_trading_days():
    from market_calendar import is_nyse_trading_day
    assert is_nyse_trading_day(date(2018, 12, 5)) is False
    assert is_nyse_trading_day(date(2025, 1, 9)) is False


@pytest.mark.parametrize("day", [date(2013, 12, 31), date(2028, 1, 3), date(2030, 12, 25)])
def test_trading_day_refuses_to_guess_outside_the_registry(day):
    from market_calendar import is_nyse_trading_day
    with pytest.raises(ValueError, match="registry covers"):
        is_nyse_trading_day(day)


def test_registry_horizon_is_the_last_registered_day():
    import market_calendar as mc
    assert mc.registry_horizon() == date(2027, 12, 31) == mc.REGISTRY_LAST


def test_installed_pandas_market_calendars_is_never_consulted(tmp_path):
    """就算環境裝了 pandas_market_calendars（這裡放一個一被 import 就失敗的假套件），
    交易日仍只看登錄表。另開行程跑，避免 reload 影響其他測試。"""
    (tmp_path / "pandas_market_calendars.py").write_text(
        "raise AssertionError('market_calendar must not import pandas_market_calendars')\n",
        encoding="utf-8")
    code = ("import datetime as dt, market_calendar as mc\n"
            "assert mc.is_nyse_trading_day(dt.date(2026, 12, 25)) is False\n"
            "assert mc.is_nyse_trading_day(dt.date(2015, 7, 3)) is False\n")
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ,
                              "PYTHONPATH": os.pathsep.join([str(tmp_path), str(AUTOMATION)])})
    assert run.returncode == 0, run.stderr


def test_requirements_do_not_list_pandas_market_calendars():
    for req in AUTOMATION.parent.glob("requirements*"):
        text = req.read_text(encoding="utf-8").lower().replace("-", "_")
        assert "pandas_market_calendars" not in text, req


# ── previous_session ───────────────────────────────────────────────

def test_previous_session_monday_returns_friday():
    from market_calendar import previous_session
    assert previous_session(date(2026, 6, 8)) == date(2026, 6, 5)  # Mon → Fri


def test_previous_session_skips_weekend_and_juneteenth():
    # Mon 2026-06-22: back past Sun/Sat and Fri 6/19 (Juneteenth) → Thu 6/18
    from market_calendar import previous_session
    assert previous_session(date(2026, 6, 22)) == date(2026, 6, 18)


def test_previous_session_refuses_to_leave_the_registry():
    from market_calendar import previous_session
    with pytest.raises(ValueError):
        previous_session(date(2014, 1, 2))      # 1/1 休市 → 會走到 2013


# ── 盤中時鐘 ────────────────────────────────────────────────────────

def _et(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=EASTERN)


def test_session_clock_regular_early_close_and_closed_days():
    import market_calendar as mc
    regular = mc.session_clock(date(2026, 9, 14))
    assert (regular.pre_market_open, regular.regular_open, regular.regular_close,
            regular.after_hours_end) == (time(4), time(9, 30), time(16), time(20))
    early = mc.session_clock(date(2026, 11, 27))          # 感恩節翌日
    assert (early.regular_close, early.after_hours_end) == (time(13), time(17))
    assert mc.session_clock(date(2026, 9, 12)) is None    # 週六
    assert mc.session_clock(date(2026, 9, 7)) is None     # Labor Day
    with pytest.raises(ValueError):
        mc.session_clock(date(2028, 1, 3))


@pytest.mark.parametrize("now, phase", [
    (_et(2026, 3, 9, 9, 29), "PRE_MARKET"),   # DST 生效後第一個交易日（3/8 週日切換）
    (_et(2026, 3, 9, 9, 30), "REGULAR"),
    (_et(2026, 11, 2, 15, 59), "REGULAR"),    # DST 結束後第一個交易日（11/1 週日切換）
    (_et(2026, 11, 2, 16, 0), "AFTER_HOURS"),
    (_et(2026, 11, 27, 12, 59), "REGULAR"),   # 提前收盤日
    (_et(2026, 11, 27, 13, 0), "AFTER_HOURS"),
    (_et(2026, 11, 27, 17, 0), None),
    (_et(2026, 9, 14, 3, 59), None),
    (_et(2026, 9, 14, 20, 0), None),
    (_et(2026, 9, 12, 10, 0), None),          # 週六
    (datetime(2026, 9, 14, 10, 0), None),     # naive
    (_et(2028, 1, 4, 10, 0), None),           # 超出登錄表：fail closed，不 raise
])
def test_session_phase(now, phase):
    from market_calendar import session_phase
    assert session_phase(now) == phase


def test_session_phase_follows_eastern_time_across_dst():
    """同一個台北時間 21:30：夏令時 = 美東 09:30 開盤，冬令時 = 08:30 盤前。"""
    from market_calendar import session_phase
    assert session_phase(datetime(2026, 9, 14, 21, 30, tzinfo=TAIPEI)) == "REGULAR"
    assert session_phase(datetime(2026, 12, 14, 21, 30, tzinfo=TAIPEI)) == "PRE_MARKET"


def test_last_completed_session():
    from market_calendar import last_completed_session
    assert last_completed_session(_et(2026, 9, 14, 15, 59)) == date(2026, 9, 11)   # 週一盤中 → 上週五
    assert last_completed_session(_et(2026, 9, 14, 16, 0)) == date(2026, 9, 14)    # 收盤後 → 當天
    assert last_completed_session(_et(2026, 11, 27, 13, 0)) == date(2026, 11, 27)  # 提前收盤
    assert last_completed_session(_et(2026, 9, 13, 12, 0)) == date(2026, 9, 11)    # 週日
    assert last_completed_session(datetime(2026, 9, 14, 17, 0)) is None            # naive
    assert last_completed_session(_et(2028, 1, 4, 17, 0)) is None                  # 超出登錄表


# ── trading_session_date_for: window mapping ───────────────────────

def _taipei(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TAIPEI)


def test_session_evening_maps_to_same_date():
    """22:00 Taipei on a trading-day Mon → that Mon's NYSE session."""
    from market_calendar import trading_session_date_for
    assert trading_session_date_for(_taipei(2026, 5, 4, 22, 0)) == date(2026, 5, 4)


def test_session_early_morning_maps_to_previous_date():
    """03:00 Taipei Tue → previous day's NYSE session (Mon)."""
    from market_calendar import trading_session_date_for
    assert trading_session_date_for(_taipei(2026, 5, 5, 3, 0)) == date(2026, 5, 4)


def test_session_after_close_returns_none():
    """06:00 Taipei is past the 05:00 window edge → outside polling window."""
    from market_calendar import trading_session_date_for
    assert trading_session_date_for(_taipei(2026, 5, 5, 6, 0)) is None


def test_session_afternoon_returns_none():
    """14:00 Taipei is well outside the window."""
    from market_calendar import trading_session_date_for
    assert trading_session_date_for(_taipei(2026, 5, 4, 14, 0)) is None


def test_session_window_edges_inclusive():
    """21:00 and 05:00 are inside the window (inclusive endpoints)."""
    from market_calendar import trading_session_date_for
    # Mon 21:00 Taipei → Mon NYSE session
    assert trading_session_date_for(_taipei(2026, 5, 4, 21, 0)) == date(2026, 5, 4)
    # Tue 05:00 Taipei → Mon NYSE session (still in window)
    assert trading_session_date_for(_taipei(2026, 5, 5, 5, 0)) == date(2026, 5, 4)


def test_session_saturday_evening_returns_none():
    """Sat 22:00 Taipei in window time-wise but Sat NYSE = non-trading."""
    from market_calendar import trading_session_date_for
    assert trading_session_date_for(_taipei(2026, 5, 9, 22, 0)) is None


def test_session_sunday_morning_returns_none():
    """Sun 03:00 Taipei → Sat NYSE session (non-trading)."""
    from market_calendar import trading_session_date_for
    assert trading_session_date_for(_taipei(2026, 5, 10, 3, 0)) is None


def test_session_holiday_evening_returns_none():
    """Memorial Day Mon 22:00 Taipei → Mon May 25 NYSE session, but holiday."""
    from market_calendar import trading_session_date_for
    assert trading_session_date_for(_taipei(2026, 5, 25, 22, 0)) is None
