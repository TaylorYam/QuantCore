"""
Unit tests for the SP500 daily-risk-check fixes (ADR-0003).

Coverage:
  - evaluate_exits reads Reference Close = data index[-1] (the off-by-one fix):
    Fri close breaches stop → SELL; the SAME data viewed as-if Thursday were the
    reference (the old index[-2] bug) does NOT fire — proving day-selection is
    what matters.
  - gap exit, missing-bar reporting, skip/dip tickers, guard helper.
  - #2 daily_report marks at Reference Close (CSV close), IB only as fallback.
  - #3 importing rebalance_ib (a library-imported script) must not hijack the
    root logger.

No IB Gateway, no network. The CIEN numbers are the real 2026-06-05 incident:
avgCost 628.63, Thu(6/4) close 535.63 (no breach), Fri(6/5) close 488.21
(< 628.63*0.8 = 502.90 → breach).
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest


def _ts(s):
    return pd.Timestamp(s)


# ── real CIEN incident data ────────────────────────────────────────
CIEN_DF = pd.DataFrame(
    {
        "Open":  [628.51, 552.39, 512.31],
        "Close": [620.37, 535.63, 488.21],
    },
    index=pd.to_datetime(["2026-06-03", "2026-06-04", "2026-06-05"]),
)
CIEN_COST = 628.63          # avgCost from IB
THU, FRI = _ts("2026-06-04"), _ts("2026-06-05")
WED = _ts("2026-06-03")


# ── #1: stop loss reads the Reference Close (index[-1]) ─────────────

def test_stop_fires_reading_friday_reference_close():
    """Reference Close = Fri 488.21 < 628.63*0.8 → SELL (this is the fix)."""
    from sp500_daily_check import evaluate_exits
    orders, missing = evaluate_exits(
        ["CIEN"], {"CIEN": 3}, {"CIEN": CIEN_COST}, lambda t: CIEN_DF,
        ref_date=FRI, prior_date=THU,
        stop_loss_pct=0.20, gap_exit_pct=0.50,
    )
    assert missing == []
    assert len(orders) == 1
    o = orders[0]
    assert (o["symbol"], o["action"], o["qty"]) == ("CIEN", "SELL", 3)
    assert "Stop loss" in o["reason"]


def test_no_stop_when_reference_close_is_thursday():
    """The OLD bug read index[-2] (Thu 535.63 > 502.90) → no exit.

    Same data, reference pointed one day too early = the position survives.
    This locks in that the *day selection* is what makes the stop fire.
    """
    from sp500_daily_check import evaluate_exits
    orders, missing = evaluate_exits(
        ["CIEN"], {"CIEN": 3}, {"CIEN": CIEN_COST}, lambda t: CIEN_DF,
        ref_date=THU, prior_date=WED,
        stop_loss_pct=0.20, gap_exit_pct=0.50,
    )
    assert missing == []
    assert orders == []


# ── gap exit ────────────────────────────────────────────────────────

def test_gap_exit_fires_on_large_open_gap():
    df = pd.DataFrame(
        {"Open": [90.0, 40.0], "Close": [100.0, 45.0]},
        index=pd.to_datetime(["2026-06-04", "2026-06-05"]),
    )
    orders, _ = evaluate_exits_call(df, cost=10.0)  # cost low → no stop, only gap
    assert len(orders) == 1
    assert "Gap exit" in orders[0]["reason"]


def evaluate_exits_call(df, cost):
    from sp500_daily_check import evaluate_exits
    return evaluate_exits(
        ["X"], {"X": 5}, {"X": cost}, lambda t: df,
        ref_date=_ts("2026-06-05"), prior_date=_ts("2026-06-04"),
        stop_loss_pct=0.20, gap_exit_pct=0.50,
    )


def test_stop_takes_precedence_over_gap_no_double_order():
    """A ticker that breaches the stop must produce exactly one SELL."""
    df = pd.DataFrame(
        {"Open": [90.0, 40.0], "Close": [100.0, 45.0]},
        index=pd.to_datetime(["2026-06-04", "2026-06-05"]),
    )
    orders, _ = evaluate_exits_call(df, cost=100.0)  # 45 < 100*0.8=80 → stop
    assert len(orders) == 1
    assert "Stop loss" in orders[0]["reason"]


# ── missing data / skip / guards ────────────────────────────────────

def test_missing_reference_close_is_reported_not_silently_skipped():
    df = pd.DataFrame({"Close": [100.0]}, index=pd.to_datetime(["2026-06-04"]))
    orders, missing = evaluate_exits_call(df, cost=10.0)  # ref_date 6/5 absent
    assert orders == []
    assert missing == ["X"]


def test_skip_tickers_not_checked():
    from sp500_daily_check import evaluate_exits
    orders, _ = evaluate_exits(
        ["SPY"], {"SPY": 3}, {"SPY": CIEN_COST}, lambda t: CIEN_DF,
        ref_date=FRI, prior_date=THU,
        stop_loss_pct=0.20, gap_exit_pct=0.50, skip_tickers={"SPY"},
    )
    assert orders == []


def test_nonpositive_cost_and_zero_qty_skipped():
    from sp500_daily_check import evaluate_exits
    orders, missing = evaluate_exits(
        ["A", "B"], {"A": 0, "B": 5}, {"A": CIEN_COST, "B": 0.0},
        lambda t: CIEN_DF, ref_date=FRI, prior_date=THU,
        stop_loss_pct=0.20, gap_exit_pct=0.50,
    )
    assert orders == []        # A has qty 0, B has cost 0
    assert missing == []


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


# ── #3: library import must not hijack the root logger ──────────────

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
