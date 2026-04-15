"""
Gateway Daily Restart — IBC CommandServer RESTART workaround.

Runs via Windows Task Scheduler at 06:00 TW daily (configured in
control_panel.py's TASK_SCHTASK_ARGS → TASK_GW_RESTART). Sends a
`RESTART` command to every IBC-managed account's CommandServer, causing
IB Gateway to warm-restart without 2FA.

Why this script exists
----------------------
IBC 3.23.0's UI scraper broke on Gateway build 10.41 — it tries to find
the "Auto Logoff / Auto Restart Time" controls in the Configuration dialog
and fails silently. Evidence in `automation/logs/ibc_taylor_YYYYMMDD.log`:

    IBC: Setting Auto restart time
    IBC: could not find auto logoff/restart time controls

Symptom: no daily auto-restart happens. Between 2026-04-10 23:45 and
2026-04-14 13:15 there were *four days* with zero Gateway restart events,
which is how we ended up with a stale session on Tuesday 04-14 and a
scheduled-task failure at 21:00.

Workaround: bypass the broken UI scraper entirely and use IBC's
CommandServer (a simple ASCII TCP protocol listening on loopback).

What this script does NOT do
----------------------------
- Does not touch Rosa — she runs a native Gateway (no IBC, no CommandServer).
  Native Gateway already handles its own daily logout prompt; Rosa is
  considered manual.
- Does not fall back to restarting IBC itself — if the CommandServer
  refuses a connection, IBC is down and needs a full re-launch which
  requires 2FA. That escalation path is Panel's "Start Gateway" button.

Usage
-----
    python gateway_daily_restart.py               # restart all IBC accounts
    python gateway_daily_restart.py --only TAYLOR # restart one account
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).parent
LOG_DIR  = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Ensure automation/ is on the path when run from Task Scheduler
sys.path.insert(0, str(BASE_DIR))

from ibc_cmd import IBCCommandError, restart_gateway

# IBC-managed accounts and their CommandServer ports.
# Keep in sync with C:\Jts\gateway_<x>\ibc\config.ini (CommandServerPort).
IBC_PORTS: dict[str, int] = {
    "TAYLOR": 7462,
    "YVONNE": 7464,
    # Rosa is native Gateway — no IBC, no CommandServer.
}


def _setup_logging() -> logging.Logger:
    log_path = LOG_DIR / f"gw_restart_{date.today():%Y%m%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s  %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,  # overwrite any root-logger config inherited at import
    )
    return logging.getLogger("gw_daily_restart")


def _restart_one(log: logging.Logger, name: str, port: int) -> bool:
    """Send RESTART to one account. Returns True on success."""
    log.info(f">>> {name}  (CommandServer :{port})")
    try:
        resp = restart_gateway("127.0.0.1", port, timeout=10.0)
    except IBCCommandError as e:
        log.error(f"[{name}] {e}")
        return False
    except Exception as e:
        log.error(f"[{name}] unexpected error: {e}", exc_info=True)
        return False

    # RESTART may return empty string if the server closes the socket
    # before sending ACK. Treat empty as success.
    upper = resp.upper()
    if resp == "" or upper.startswith("OK") or "RESTART" in upper:
        log.info(f"[{name}] accepted (response={resp!r})")
        return True

    log.warning(f"[{name}] unexpected response: {resp!r}")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        metavar="ACCOUNT",
        help="Restart just one account (TAYLOR / YVONNE); default is all.",
    )
    args = parser.parse_args(argv)

    log = _setup_logging()
    log.info("=" * 60)
    log.info(f"Gateway Daily Restart — {date.today()}")
    log.info("=" * 60)

    if args.only:
        target = args.only.upper()
        if target not in IBC_PORTS:
            log.error(f"Unknown account {target!r}; valid: {sorted(IBC_PORTS)}")
            return 2
        targets = {target: IBC_PORTS[target]}
    else:
        targets = dict(IBC_PORTS)

    failures = 0
    for name, port in targets.items():
        if not _restart_one(log, name, port):
            failures += 1

    if failures:
        log.error(f"Finished with {failures}/{len(targets)} failure(s)")
        return 1
    log.info(f"All {len(targets)} IBC-managed Gateway(s) restarted OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
