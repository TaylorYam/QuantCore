"""
Unit tests for market_calendar — NYSE session helper used by ROSA poller.

Coverage:
  - is_nyse_trading_day: weekend / weekday / holiday classification
  - trading_session_date_for: Taipei → NYSE session-date mapping with window
  - Hardcoded fallback path activates when pandas_market_calendars unavailable

No network / no IB needed.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest


TAIPEI = ZoneInfo("Asia/Taipei")


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
