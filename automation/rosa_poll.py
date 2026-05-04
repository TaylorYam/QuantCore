"""
Pure logic for the ROSA market-window poller.

Two functions, both pure (no Tk, no IB, no real time access except where the
caller injects it). The Tk wrapper in control_panel reads `next_rosa_poll_time`
to schedule itself and runs `rosa_poll_decision` inside each tick, in a daemon
thread so the wait-for-restart sleep doesn't freeze the UI.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from market_calendar import trading_session_date_for

# Cap how far we'll scan when looking for the next valid tick. 4 days (= 192
# half-hour steps) is enough to clear a long weekend + holiday combo and still
# refuse to spin forever if the calendar somehow returns nothing valid.
_MAX_SCAN_TICKS = 192

# Time to wait between firing β-retry and re-reading is_online state. Has to
# exceed ib_insync's connect timeout (CONNECT_TIMEOUT_SEC=10 in
# gateway_monitor) so the new thread has a chance to either succeed or give up.
RETRY_WAIT_SEC = 12.0


def _floor_half_hour(dt: datetime) -> datetime:
    """Round a datetime down to the previous XX:00 / XX:30 mark."""
    return dt.replace(minute=(dt.minute // 30) * 30, second=0, microsecond=0)


def next_rosa_poll_time(now_taipei: datetime) -> datetime:
    """Return the next clock-aligned tick (XX:00 / XX:30, Taipei time) at
    which we should run a ROSA poll.

    Always strictly future. Skips ticks that fall outside the 21:00–05:00
    window or that map to non-trading NYSE sessions; in either case scans
    forward until the first valid tick is found.
    """
    # Start at the next strictly-future half-hour from `now_taipei`.
    candidate = _floor_half_hour(now_taipei) + timedelta(minutes=30)
    for _ in range(_MAX_SCAN_TICKS):
        if trading_session_date_for(candidate) is not None:
            return candidate
        candidate += timedelta(minutes=30)
    raise RuntimeError(
        f"next_rosa_poll_time: no valid tick found within {_MAX_SCAN_TICKS} "
        f"half-hour steps from {now_taipei.isoformat()}"
    )


def rosa_poll_decision(
    now_taipei: datetime,
    is_online: Callable[[], bool],
    restart: Callable[[], bool],
    sleep: Callable[[float], None],
) -> str | None:
    """Run one polling cycle. Returns the Telegram message to send, or None
    if the system is healthy / outside window.

    Sequence:
      1. If `now` is outside the polling window → return None (no-op).
      2. If `is_online()` → return None.
      3. Otherwise fire a β retry (`restart()`), wait `RETRY_WAIT_SEC` for
         the new monitor thread to settle, re-check `is_online()`.
      4. If still offline → return red alert message. Otherwise → None.

    Parameters are injected so this stays a pure function — the production
    wrapper passes `gw_monitor.is_online`, `gw_monitor.restart_account_thread`,
    and `time.sleep`.
    """
    if trading_session_date_for(now_taipei) is None:
        return None

    if is_online():
        return None

    restart()  # return value (True/False) doesn't change our decision —
               # we still need to verify state after the wait.
    sleep(RETRY_WAIT_SEC)

    if is_online():
        return None

    return ("\U0001F534 <b>Rosa</b> Gateway disconnected\n"
            "<code>polling check + retry failed</code>")
