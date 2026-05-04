"""
Unit tests for ty_check — TAYLOR/YVONNE Tue/Wed 20:05 verification.

Three pieces under test:
  * next_ty_check_time(now)            → next Tue or Wed 20:05 Taipei
                                          (skips NYSE holidays)
  * format_ty_message(name, ok, err)   → Telegram message string
  * verify_one_account(name, connect)  → invokes connect_fn, returns msg

All pure (no IB, no Tk, no real time).
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

TAIPEI = ZoneInfo("Asia/Taipei")


def _t(y, mo, d, h, mi=5):
    """Convenience: Taipei datetime defaulting to :05 (the trigger minute)."""
    return datetime(y, mo, d, h, mi, tzinfo=TAIPEI)


# ── next_ty_check_time: weekday math ───────────────────────────────

def test_next_check_from_monday_evening_returns_tue_2005():
    from ty_check import next_ty_check_time
    # Mon May 4 2026 18:00 Taipei → Tue May 5 20:05 Taipei
    assert next_ty_check_time(_t(2026, 5, 4, 18, 0)) == _t(2026, 5, 5, 20, 5)


def test_next_check_from_tue_morning_returns_same_day():
    """If we ask before 20:05 on a trading Tue, the answer is today 20:05."""
    from ty_check import next_ty_check_time
    assert next_ty_check_time(_t(2026, 5, 5, 10, 0)) == _t(2026, 5, 5, 20, 5)


def test_next_check_from_tue_2004_returns_same_day():
    """One minute before trigger → still today 20:05."""
    from ty_check import next_ty_check_time
    assert next_ty_check_time(_t(2026, 5, 5, 20, 4)) == _t(2026, 5, 5, 20, 5)


def test_next_check_from_tue_2005_exact_returns_wednesday():
    """Strictly future — exactly on the trigger means we've already fired
    (the after() callback consumed it), so the next one is tomorrow."""
    from ty_check import next_ty_check_time
    assert next_ty_check_time(_t(2026, 5, 5, 20, 5)) == _t(2026, 5, 6, 20, 5)


def test_next_check_from_tue_2006_returns_wednesday():
    from ty_check import next_ty_check_time
    assert next_ty_check_time(_t(2026, 5, 5, 20, 6)) == _t(2026, 5, 6, 20, 5)


def test_next_check_from_wed_2006_returns_next_tuesday():
    from ty_check import next_ty_check_time
    # Wed May 6 2026 20:06 → Tue May 12 2026 20:05
    assert next_ty_check_time(_t(2026, 5, 6, 20, 6)) == _t(2026, 5, 12, 20, 5)


def test_next_check_from_thursday_returns_next_tuesday():
    from ty_check import next_ty_check_time
    # Thu May 7 → Tue May 12
    assert next_ty_check_time(_t(2026, 5, 7, 9, 0)) == _t(2026, 5, 12, 20, 5)


def test_next_check_from_friday_returns_next_tuesday():
    from ty_check import next_ty_check_time
    assert next_ty_check_time(_t(2026, 5, 8, 23, 59)) == _t(2026, 5, 12, 20, 5)


def test_next_check_from_saturday_returns_next_tuesday():
    from ty_check import next_ty_check_time
    assert next_ty_check_time(_t(2026, 5, 9, 12, 0)) == _t(2026, 5, 12, 20, 5)


def test_next_check_from_sunday_returns_next_tuesday():
    from ty_check import next_ty_check_time
    assert next_ty_check_time(_t(2026, 5, 10, 23, 0)) == _t(2026, 5, 12, 20, 5)


# ── next_ty_check_time: NYSE holiday skipping ──────────────────────

def test_next_check_skips_holiday_tuesday(monkeypatch):
    """Mock Tue May 5 as a NYSE holiday → next check rolls to Wed May 6."""
    import ty_check

    real = ty_check.is_nyse_trading_day

    def mock_is_trading(d):
        if d == date(2026, 5, 5):
            return False
        return real(d)

    monkeypatch.setattr(ty_check, "is_nyse_trading_day", mock_is_trading)

    assert ty_check.next_ty_check_time(_t(2026, 5, 4, 18, 0)) == _t(2026, 5, 6, 20, 5)


def test_next_check_skips_both_tue_and_wed_holidays(monkeypatch):
    """If both Tue and Wed are NYSE holidays this week, advance to next week."""
    import ty_check

    real = ty_check.is_nyse_trading_day

    def mock_is_trading(d):
        if d in (date(2026, 5, 5), date(2026, 5, 6)):
            return False
        return real(d)

    monkeypatch.setattr(ty_check, "is_nyse_trading_day", mock_is_trading)

    assert ty_check.next_ty_check_time(_t(2026, 5, 4, 18, 0)) == _t(2026, 5, 12, 20, 5)


# ── format_ty_message: structure and content ───────────────────────

def test_message_success_contains_check_and_ready():
    from ty_check import format_ty_message
    msg = format_ty_message("Taylor", success=True)
    assert "Taylor" in msg
    assert "✅" in msg          # ✅
    assert "ready" in msg.lower()


def test_message_failure_contains_red_dot_and_reason():
    from ty_check import format_ty_message
    msg = format_ty_message("Yvonne", success=False, error="connect timeout")
    assert "Yvonne" in msg
    assert "\U0001F534" in msg       # 🔴
    assert "not logged in" in msg.lower()
    assert "connect timeout" in msg


def test_message_failure_without_error_still_alerts():
    """Error string is optional — message should still be a red alert."""
    from ty_check import format_ty_message
    msg = format_ty_message("Taylor", success=False)
    assert "Taylor" in msg
    assert "\U0001F534" in msg


# ── verify_one_account: side-effect orchestration ──────────────────

def test_verify_returns_green_when_connect_succeeds():
    from ty_check import verify_one_account

    calls = []

    def fake_connect():
        calls.append(1)
        # success: returns normally

    msg = verify_one_account("Taylor", fake_connect)

    assert calls == [1]
    assert "✅" in msg
    assert "Taylor" in msg


def test_verify_returns_red_when_connect_raises():
    from ty_check import verify_one_account

    def fake_connect():
        raise TimeoutError("API connection failed: timeout")

    msg = verify_one_account("Yvonne", fake_connect)

    assert "\U0001F534" in msg
    assert "Yvonne" in msg
    assert "API connection failed" in msg


def test_verify_swallows_exception_does_not_propagate():
    """The function must always return a message — never bubble exceptions
    up to the daemon thread (caller has no exception handler in scope)."""
    from ty_check import verify_one_account

    def fake_connect():
        raise RuntimeError("boom")

    # Must not raise.
    msg = verify_one_account("Taylor", fake_connect)
    assert "boom" in msg
