"""
Unit tests for sp500_daily_check helpers + related logging/report fixes.

Scope note (ADR-0004): the fixed 20% stop-loss and 50% gap-exit were removed —
8-year backtest showed the stop added nothing (and the gap never fired), and
they created a live-only stop→stale-rebuy whipsaw. So `evaluate_exits` is gone;
daily_check now only does the bear dip-buy check. What remains here:

  - _prev_trading_day  : staleness guard helper (still used for dip-buy freshness)
  - daily_report mark_at_reference_close : EOD mark = CSV close, IB fallback (ADR-0003)
  - logging: import must not hijack root; quiet_ib_console keeps INFO file logs

No IB Gateway, no network.
"""

from __future__ import annotations

from datetime import date

import pytest


# ── staleness guard helper ──────────────────────────────────────────

def test_prev_trading_day_monday_returns_friday():
    from sp500_daily_check import _prev_trading_day
    assert _prev_trading_day(date(2026, 6, 8)) == date(2026, 6, 5)  # Mon → Fri


def test_prev_trading_day_skips_weekend_and_juneteenth():
    # Mon 2026-06-22: back past Sun/Sat and Fri 6/19 (Juneteenth) → Thu 6/18
    from sp500_daily_check import _prev_trading_day
    assert _prev_trading_day(date(2026, 6, 22)) == date(2026, 6, 18)


# ── #2: daily_report marks at Reference Close, IB only as fallback ──

def test_daily_report_marks_at_reference_close_then_ib_fallback(monkeypatch):
    pytest.importorskip("ib_insync")
    import price_utils
    import daily_report
    # CIEN present in CSV (official close), STX absent → must fall back to IB
    monkeypatch.setattr(price_utils, "load_csv_close",
                        lambda s: {"CIEN": 488.21}.get(s, 0.0))
    monkeypatch.setattr(daily_report, "get_prices",
                        lambda ib, syms, ctx=None: {"STX": 912.0})
    marks = daily_report.mark_at_reference_close(
        ib=None, symbols=["CIEN", "STX"], ctx=None)
    assert marks["CIEN"] == 488.21    # official close, NOT a stale weekend tick
    assert marks["STX"] == 912.0      # CSV missing → IB fallback


# ── #3: logging — library import must not hijack the root logger ────

def test_importing_rebalance_ib_does_not_hijack_root_logger():
    import logging
    import importlib
    pytest.importorskip("ib_insync")
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    saved = root.handlers[:]
    root.handlers[:] = [sentinel]
    try:
        import rebalance_ib
        importlib.reload(rebalance_ib)   # re-run module-level code
        assert sentinel in root.handlers, "import hijacked root logger"
        assert not any(isinstance(h, logging.FileHandler) for h in root.handlers), \
            "import added a FileHandler to root"
    finally:
        root.handlers[:] = saved


def test_quiet_ib_console_preserves_info_file_logging(tmp_path):
    """quiet_ib_console must NOT suppress INFO in files (regression 2026-06-08).

    ib_insync.util.logToConsole(WARNING) lowers root to WARNING; the helper
    restores INFO so daily_report / moo_fill / rebalance keep full audit logs.
    """
    import logging
    pytest.importorskip("ib_insync")
    from log_setup import quiet_ib_console
    logf = tmp_path / "t.log"
    saved = logging.root.handlers[:]
    saved_level = logging.root.level
    try:
        logging.basicConfig(level=logging.INFO,
                            handlers=[logging.FileHandler(logf, encoding="utf-8")],
                            force=True)
        quiet_ib_console()
        assert logging.root.level == logging.INFO
        logging.getLogger("audit").info("position-dump-should-be-logged")
        for h in logging.root.handlers:
            h.flush()
        assert "position-dump-should-be-logged" in logf.read_text(encoding="utf-8")
    finally:
        for h in logging.root.handlers:
            try:
                h.close()
            except Exception:
                pass
        logging.root.handlers[:] = saved
        logging.root.setLevel(saved_level)
