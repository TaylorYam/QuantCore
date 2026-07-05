"""
Pure logic for the weekday-morning 07:00 gateway Check Now.

Two pieces:
  * next_morning_check_time(now)        — next Taipei 07:00 that lands on an
                                          NYSE trading day (strictly future;
                                          weekends + NYSE holidays skipped)
  * format_morning_check_message(names) — Telegram alert body, or None when
                                          nothing is offline (all gateways
                                          healthy → caller stays silent)

Side effects (real IB re-probe, Tk after, Telegram send) live in
control_panel. This module stays pure so the scheduling and message logic
can be exercised in milliseconds without any infrastructure.

Note on the calendar test: we feed `is_nyse_trading_day` the *Taipei*
calendar date (same convention as ty_check). At 07:00 Taipei that maps to
the previous US session's evening, but the intent here is simply "run a
connection health-check on each Taipei trading weekday, skip weekends and
US holidays" — not to align with a specific NYSE session — so the Taipei
date is the right and simplest key.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from market_calendar import is_nyse_trading_day

# Trigger time-of-day in Taipei.
_TRIGGER_TIME = time(7, 0)

# Cap forward scan so a corrupt calendar can't infinite-loop us.
_MAX_SCAN_DAYS = 14

# Seconds to wait after firing Check Now before reading is_online state.
# Must exceed gateway_monitor.CONNECT_TIMEOUT_SEC (10) so a re-spawned
# monitor thread's fresh connect attempt has time to either succeed or give
# up before we decide whether to alert. Mirrors rosa_poll.RETRY_WAIT_SEC.
MORNING_SETTLE_SEC = 12.0


def next_morning_check_time(now_taipei: datetime) -> datetime:
    """Return the next Taipei datetime (strictly future) at which we should
    run the morning gateway check. Always 07:00, skipping any day that is a
    weekend or NYSE holiday."""
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
        if is_nyse_trading_day(candidate.date()):
            return candidate
        candidate = candidate + timedelta(days=1)

    raise RuntimeError(
        f"next_morning_check_time: no NYSE trading day found within "
        f"{_MAX_SCAN_DAYS} days from {now_taipei.isoformat()}"
    )


def format_morning_check_message(offline_names: list[str]) -> str | None:
    """Telegram alert body for the morning check, or None when the offline
    list is empty (all gateways healthy → caller sends nothing).

    Listing the offline accounts (not the healthy ones) keeps the alert
    actionable: it names exactly which Gateways need a manual restart."""
    if not offline_names:
        return None
    lines = "\n".join(
        f"\U0001F534 <b>{name}</b> Gateway offline" for name in offline_names
    )
    return (
        f"⚠️ <b>Morning check 07:00</b>\n{lines}\n"
        f"<code>Check Now re-probe failed — manual Gateway restart needed</code>"
    )
