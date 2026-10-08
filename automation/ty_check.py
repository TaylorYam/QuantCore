"""
Pure logic for the TAYLOR/YVONNE Tue/Wed 20:05 login verification.

Three pieces:
  * next_ty_check_time(now)           — next Taipei datetime when we should
                                          run the verification (always Tue or
                                          Wed at 20:05, NYSE holidays skipped)
  * format_ty_message(name, ok, err)  — Telegram message string
  * verify_one_account(name, connect) — orchestrates one connect attempt and
                                          turns success / failure into a message

Side effects (real IB connect, timers, Telegram send) live in the Panel's watch
(monitor_engine + qt_panel.watch). This module stays pure so the scheduling and message logic can be exercised
in milliseconds without any infrastructure.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Callable

from market_calendar import is_nyse_trading_day

# Trigger time-of-day in Taipei. 20:05 (not 20:00) gives a 5-minute buffer
# for IBC to finish authenticating right before the user expects to be live.
_TRIGGER_TIME = time(20, 5)

# Tue=1, Wed=2 in Python weekday() (Mon=0).
_TY_WEEKDAYS = (1, 2)

# Cap forward scan so a corrupt calendar can't infinite-loop us.
_MAX_SCAN_DAYS = 14


def next_ty_check_time(now_taipei: datetime) -> datetime:
    """Return the next Taipei datetime (strictly future) at which we should
    run TAYLOR/YVONNE verification. Always lands on a Tue or Wed at 20:05,
    skipping any day that is a NYSE holiday."""
    candidate = now_taipei.replace(
        hour=_TRIGGER_TIME.hour, minute=_TRIGGER_TIME.minute,
        second=0, microsecond=0,
    )
    if candidate <= now_taipei:
        candidate = candidate + timedelta(days=1)
        candidate = candidate.replace(
            hour=_TRIGGER_TIME.hour, minute=_TRIGGER_TIME.minute,
            second=0, microsecond=0,
        )

    for _ in range(_MAX_SCAN_DAYS):
        if (candidate.weekday() in _TY_WEEKDAYS
                and is_nyse_trading_day(candidate.date())):
            return candidate
        candidate = candidate + timedelta(days=1)

    raise RuntimeError(
        f"next_ty_check_time: no valid Tue/Wed found within {_MAX_SCAN_DAYS} "
        f"days from {now_taipei.isoformat()}"
    )


def format_ty_message(name: str, success: bool, error: str | None = None) -> str:
    """Telegram message body for one TAYLOR/YVONNE verification outcome."""
    if success:
        return f"✅ <b>{name}</b> ready for trading"
    body = f"\U0001F534 <b>{name}</b> not logged in by 20:05"
    if error:
        body += f"\n<code>{error}</code>"
    return body


def verify_one_account(name: str, connect_fn: Callable[[], None]) -> str:
    """Invoke `connect_fn` and translate its outcome into a Telegram message.

    Always returns a string — never propagates an exception. The caller runs
    this on a daemon thread and has no exception handler in scope, so a leak
    here would be silent and the alert would never reach Telegram.
    """
    try:
        connect_fn()
        return format_ty_message(name, success=True)
    except Exception as e:
        err = str(e) or type(e).__name__
        return format_ty_message(name, success=False, error=err)
