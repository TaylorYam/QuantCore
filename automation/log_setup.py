"""QuantCore — shared logging setup for standalone scripts.

Why this exists
---------------
Each automation script writes to its own ``logs/{prefix}_{date}.log`` via
``logging.basicConfig(..., force=True)``. When that call sits at *module level*
it fires on **import**, not just when the script is the entry point — so a
script imported as a library (e.g. ``rebalance_ib`` imported by
``sp500_daily_check`` for ``place_orders`` / ``check_fills``) silently hijacks
the importer's root logger and redirects its output into the wrong file. That
is how the SP500 daily-risk-check output ended up inside ``rebalance_*.log``
while ``daily_check_*.log`` looked empty.

Rule: scripts call ``setup_logging()`` from their entry point
(``if __name__ == "__main__":``), never at module import. Imported modules then
inherit the entry point's root logger instead of clobbering it.
"""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

_LOG_DIR = Path(__file__).parent / "logs"


def setup_logging(prefix: str, *, level: int = logging.INFO,
                  to_stdout: bool = True) -> Path:
    """Point the root logger at ``logs/{prefix}_{YYYYMMDD}.log``.

    Call ONCE from a script's entry point. Returns the log file path.
    Uses ``force=True`` because an entry point legitimately owns root logging;
    the bug was calling this at import time, not the ``force`` itself.
    """
    _LOG_DIR.mkdir(exist_ok=True)
    log_file = _LOG_DIR / f"{prefix}_{date.today().strftime('%Y%m%d')}.log"
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    if to_stdout:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)s  %(message)s",
        handlers=handlers,
        force=True,
    )
    return log_file


def quiet_ib_console(level: int = logging.WARNING) -> None:
    """Quiet ib_insync's console chatter WITHOUT losing INFO file logs.

    ``ib_insync.util.logToConsole(level)`` lowers the ROOT logger to ``level``,
    which suppresses the INFO records our FileHandler is meant to capture
    (this regressed daily_report on 2026-06-08). Restore root to INFO after.
    Call AFTER setup_logging(), from a script's entry point.
    """
    from ib_insync import util
    util.logToConsole(level)
    logging.root.setLevel(logging.INFO)
