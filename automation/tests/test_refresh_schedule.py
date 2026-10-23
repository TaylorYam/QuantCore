"""Pure tests for refresh_schedule — NYSE market-hours refresh timing."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from market_calendar import is_early_close
from refresh_schedule import (
    _SLOTS_REGULAR,
    next_refresh_time,
    refresh_slots,
)

ET = ZoneInfo("America/New_York")
# 提前收盤日（13:00 收盤）的刷新時刻：收盤時間只在 market_calendar（#234）
_SLOTS_EARLY = tuple(s for s in _SLOTS_REGULAR if s <= time(13, 0))


def _et(y, m, d, h, mi=0, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=ET)


# ── refresh_slots ────────────────────────────────────────────────────

def test_regular_trading_day_has_8_slots():
    # 2026-09-14 is a Monday, NYSE trading day
    slots = refresh_slots(date(2026, 9, 14))
    assert slots == _SLOTS_REGULAR
    assert slots[0] == time(9, 30)
    assert slots[-1] == time(16, 0)
    assert len(slots) == 8


def test_weekend_has_no_slots():
    assert refresh_slots(date(2026, 9, 12)) == ()   # Saturday
    assert refresh_slots(date(2026, 9, 13)) == ()   # Sunday


def test_nyse_holiday_has_no_slots():
    # 2026-01-19 Martin Luther King Jr. Day
    assert refresh_slots(date(2026, 1, 19)) == ()


def test_early_close_day_stops_at_13():
    # 2026-11-27 day after Thanksgiving (official registry early close)
    slots = refresh_slots(date(2026, 11, 27))
    assert slots == _SLOTS_EARLY
    assert slots[-1] == time(13, 0)
    assert len(slots) == 5


@pytest.mark.parametrize("day", [date(2027, 7, 2), date(2027, 12, 23)])
def test_dates_before_observed_holidays_are_regular_sessions(day):
    """Observed full-day holidays do not create a preceding half-day."""
    assert refresh_slots(day) == _SLOTS_REGULAR


def test_is_early_close_known_dates():
    assert is_early_close(date(2026, 11, 27))
    assert is_early_close(date(2026, 12, 24))
    assert not is_early_close(date(2026, 9, 14))


# ── next_refresh_time ────────────────────────────────────────────────

def test_before_first_slot_returns_930():
    now = _et(2026, 9, 14, 8, 0)
    nxt = next_refresh_time(now)
    assert nxt == _et(2026, 9, 14, 9, 30)


def test_at_exactly_930_returns_1000():
    now = _et(2026, 9, 14, 9, 30)
    nxt = next_refresh_time(now)
    assert nxt == _et(2026, 9, 14, 10, 0)


def test_between_slots_returns_next_slot():
    now = _et(2026, 9, 14, 11, 30)
    nxt = next_refresh_time(now)
    assert nxt == _et(2026, 9, 14, 12, 0)


def test_after_last_slot_rolls_to_next_trading_day():
    # Monday 16:01 → Tuesday 09:30
    now = _et(2026, 9, 14, 16, 1)
    nxt = next_refresh_time(now)
    assert nxt == _et(2026, 9, 15, 9, 30)


def test_friday_after_close_rolls_to_monday():
    # Friday 2026-09-18 16:30 → Monday 2026-09-21 09:30
    now = _et(2026, 9, 18, 16, 30)
    nxt = next_refresh_time(now)
    assert nxt == _et(2026, 9, 21, 9, 30)


def test_weekend_rolls_to_monday():
    now = _et(2026, 9, 19, 12, 0)  # Saturday
    nxt = next_refresh_time(now)
    assert nxt == _et(2026, 9, 21, 9, 30)


def test_holiday_skipped():
    # 2026-09-07 is Labor Day (Monday, NYSE holiday)
    # Sunday 2026-09-06 → skip Monday → Tuesday 2026-09-08 09:30
    now = _et(2026, 9, 6, 12, 0)
    nxt = next_refresh_time(now)
    assert nxt == _et(2026, 9, 8, 9, 30)


def test_early_close_day_no_slots_after_13():
    # 2026-11-27 early close: after 13:00 → next trading day
    now = _et(2026, 11, 27, 13, 1)
    nxt = next_refresh_time(now)
    assert nxt.date() == date(2026, 11, 30)  # Monday
    assert nxt.time() == time(9, 30)


def test_early_close_day_slot_at_1230_returns_1300():
    now = _et(2026, 11, 27, 12, 30)
    nxt = next_refresh_time(now)
    assert nxt == _et(2026, 11, 27, 13, 0)


def test_preserves_timezone():
    now = _et(2026, 9, 14, 8, 0)
    nxt = next_refresh_time(now)
    assert nxt.tzinfo is ET


def test_no_trading_day_within_scan_raises(monkeypatch):
    # Monkey-patch to make every day non-trading
    import refresh_schedule as mod
    monkeypatch.setattr(mod, "session_clock", lambda d: None)
    with pytest.raises(RuntimeError, match="no NYSE trading day"):
        next_refresh_time(_et(2026, 9, 14, 8, 0))
