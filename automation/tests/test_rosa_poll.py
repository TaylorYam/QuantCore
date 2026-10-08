"""
Unit tests for rosa_poll — pure logic for the ROSA market-window poller.

Two functions under test:
  * next_rosa_poll_time(now) → datetime  (next clock-aligned tick we should fire)
  * rosa_poll_decision(now, is_online, restart, sleep, account=) → str | None
      (the Telegram message to send, or None if all is well)

Both are pure (no Tk, no IB, no real time) so we can pin every branch.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

TAIPEI = ZoneInfo("Asia/Taipei")


def _t(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=TAIPEI)


# ── next_rosa_poll_time: clock-alignment within window ─────────────

def test_next_tick_within_window_advances_to_half_hour():
    from rosa_poll import next_rosa_poll_time
    assert next_rosa_poll_time(_t(2026, 5, 4, 21, 17)) == _t(2026, 5, 4, 21, 30)


def test_next_tick_on_exact_half_hour_advances_to_next():
    """Exactly on a tick → return strictly future tick (not the same one)."""
    from rosa_poll import next_rosa_poll_time
    assert next_rosa_poll_time(_t(2026, 5, 4, 21, 30)) == _t(2026, 5, 4, 22, 0)


def test_next_tick_on_exact_hour_advances_to_half():
    from rosa_poll import next_rosa_poll_time
    assert next_rosa_poll_time(_t(2026, 5, 4, 22, 0)) == _t(2026, 5, 4, 22, 30)


def test_next_tick_at_2255_jumps_to_2300():
    from rosa_poll import next_rosa_poll_time
    assert next_rosa_poll_time(_t(2026, 5, 4, 22, 55)) == _t(2026, 5, 4, 23, 0)


def test_next_tick_just_past_half_minute_seconds():
    """Sub-minute precision: 21:30:00.001 → 22:00, not 21:30."""
    from rosa_poll import next_rosa_poll_time
    assert next_rosa_poll_time(_t(2026, 5, 4, 21, 30, 1)) == _t(2026, 5, 4, 22, 0)


# ── next_rosa_poll_time: window edges ───────────────────────────────

def test_next_tick_at_0455_advances_to_0500():
    """05:00 is still inside the polling window (inclusive end)."""
    from rosa_poll import next_rosa_poll_time
    assert next_rosa_poll_time(_t(2026, 5, 5, 4, 55)) == _t(2026, 5, 5, 5, 0)


def test_next_tick_at_0501_jumps_to_next_window_open():
    """Past 05:00 → out of window → schedule next 21:00 of a trading-day eve."""
    from rosa_poll import next_rosa_poll_time
    # Tue May 5 05:01 → next valid window open = Tue May 5 21:00
    # (Tue 21:00 Taipei → Tue NYSE date which is a trading day)
    assert next_rosa_poll_time(_t(2026, 5, 5, 5, 1)) == _t(2026, 5, 5, 21, 0)


def test_next_tick_at_afternoon_jumps_to_2100_same_day():
    from rosa_poll import next_rosa_poll_time
    assert next_rosa_poll_time(_t(2026, 5, 4, 14, 0)) == _t(2026, 5, 4, 21, 0)


def test_next_tick_skips_non_trading_session_dates():
    """Sat 23:00 Taipei → maps to Sat NYSE (non-trading) — skip ahead.
    Continues until first valid: Mon 21:00 Taipei (Mon NYSE = trading)."""
    from rosa_poll import next_rosa_poll_time
    # Sat May 9 2026 23:00 Taipei → next valid tick = Mon May 11 21:00
    assert next_rosa_poll_time(_t(2026, 5, 9, 23, 0)) == _t(2026, 5, 11, 21, 0)


def test_next_tick_skips_holiday_evening():
    """Mon May 25 2026 (Memorial Day) 22:00 Taipei → maps to non-trading Mon NYSE.
    Next tick should land on Tue May 26 21:00 (regular trading)."""
    from rosa_poll import next_rosa_poll_time
    assert next_rosa_poll_time(_t(2026, 5, 25, 22, 0)) == _t(2026, 5, 26, 21, 0)


# ── rosa_poll_decision: outcome semantics ──────────────────────────

def _never_call(*a, **k):
    raise AssertionError("should not have been called")


def test_decision_online_returns_none():
    """ROSA is online → no alert, no retry attempted."""
    from rosa_poll import rosa_poll_decision

    result = rosa_poll_decision(
        now_taipei=_t(2026, 5, 4, 22, 30),
        is_online=lambda: True,
        restart=_never_call,
        sleep=_never_call,
        account="Rosa",
    )
    assert result is None


def test_decision_outside_window_returns_none():
    """Tick fired outside window (race condition / system clock jump) → no-op."""
    from rosa_poll import rosa_poll_decision

    result = rosa_poll_decision(
        now_taipei=_t(2026, 5, 4, 14, 0),  # afternoon — out of window
        is_online=_never_call,              # must short-circuit before checking
        restart=_never_call,
        sleep=_never_call,
        account="Rosa",
    )
    assert result is None


def test_decision_offline_then_retry_succeeds_returns_none():
    """ROSA offline → β retry → online → no Telegram."""
    from rosa_poll import rosa_poll_decision

    states = iter([False, True])  # first check: offline; after retry: online
    sleeps: list[float] = []
    restarts: list[int] = []

    def is_online():
        return next(states)

    def restart():
        restarts.append(1)
        return True  # spawn succeeded

    result = rosa_poll_decision(
        now_taipei=_t(2026, 5, 4, 22, 30),
        is_online=is_online,
        restart=restart,
        sleep=sleeps.append,
        account="Rosa",
    )

    assert result is None
    assert restarts == [1]
    assert len(sleeps) == 1, "should sleep exactly once between restart and re-check"
    assert sleeps[0] >= 5, "should wait long enough for connect timeout"


def test_decision_offline_retry_fails_returns_alert_message():
    """ROSA offline → β retry → still offline → red Telegram."""
    from rosa_poll import rosa_poll_decision

    result = rosa_poll_decision(
        now_taipei=_t(2026, 5, 4, 22, 30),
        is_online=lambda: False,  # always offline
        restart=lambda: True,     # spawn fired
        sleep=lambda s: None,
        account="Rosa",
    )

    assert result is not None
    assert "Rosa" in result
    assert "disconnected" in result.lower() or "offline" in result.lower()


def test_decision_offline_restart_refused_still_alerts():
    """If restart is refused (e.g. old thread still hanging), we should still
    send the alert because the connection state is offline."""
    from rosa_poll import rosa_poll_decision

    result = rosa_poll_decision(
        now_taipei=_t(2026, 5, 4, 22, 30),
        is_online=lambda: False,
        restart=lambda: False,    # refused (existing thread alive)
        sleep=lambda s: None,
        account="Rosa",
    )
    assert result is not None
    assert "Rosa" in result


def test_alert_names_the_remote_gateway_account():
    """警報文字由帳戶名組成（#211）；Rosa 的文字和改動前逐字相同。"""
    from rosa_poll import rosa_poll_decision
    def alert(account):
        return rosa_poll_decision(now_taipei=_t(2026, 5, 4, 22, 30), is_online=lambda: False,
                                  restart=lambda: True, sleep=lambda s: None, account=account)
    assert alert("Rosa") == ("\U0001F534 <b>Rosa</b> Gateway disconnected\n"
                             "<code>polling check + retry failed</code>")
    assert alert("Zoe").startswith("\U0001F534 <b>Zoe</b> Gateway disconnected")
