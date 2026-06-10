"""
Unit tests for utils.filter_entry_eligible (ADR-0005).

Background: holdings are SELL-flagged when price < EXIT_EMA, but the buy-candidate
step did not apply the same condition — so a just-EMA-sold, still-high-ranked name
was immediately re-selected in the same rotation (live case: CIEN 2026-06-10,
"SELL 跌破EMA50" then "BUY CIEN: rank=3" → net delta 0, the exit never happened).
The shared filter closes that hole for both signals.py and backtester.py.

Pure function — no IB, no data files.
"""

from __future__ import annotations

from strategies.sp500_momentum.utils import filter_entry_eligible


# Real numbers from the 2026-06-10 rotation log (signal_date 2026-06-09)
CIEN = {"ticker": "CIEN", "price": 439.34, "exit_ema": 508.37, "adj_slope": 1.139}
AMD  = {"ticker": "AMD",  "price": 521.54, "exit_ema": 372.51}
STX  = {"ticker": "STX",  "price": 847.47, "exit_ema": 703.98}


def test_below_ema_excluded_even_if_top_ranked():
    """The CIEN case: top-ranked but below EXIT_EMA → must not be a buy candidate."""
    out = filter_entry_eligible([CIEN, AMD], exclude=set())
    assert [x["ticker"] for x in out] == ["AMD"]


def test_kept_holdings_excluded_and_order_preserved():
    out = filter_entry_eligible([STX, CIEN, AMD], exclude={"STX"})
    assert [x["ticker"] for x in out] == ["AMD"]


def test_above_ema_names_pass_in_rank_order():
    out = filter_entry_eligible([AMD, STX], exclude=set())
    assert [x["ticker"] for x in out] == ["AMD", "STX"]


def test_missing_price_or_ema_treated_as_eligible():
    """Symmetric with the sell-side guard: can't judge EMA → don't exclude."""
    rows = [
        {"ticker": "NOEMA", "price": 10.0, "exit_ema": 0},
        {"ticker": "NOPX",  "price": 0,    "exit_ema": 50.0},
    ]
    out = filter_entry_eligible(rows, exclude=set())
    assert [x["ticker"] for x in out] == ["NOEMA", "NOPX"]


def test_entry_ema_filter_default_off():
    """ADR-0005: WF rejected the entry-EMA filter — the deployed default is OFF.

    Walk-forward (N=8 + N=7, 2018-2026) showed the no-filter variant wins
    fold-mode 5/8 & 5/7 and cumulative OOS NP; flipping this to True is a
    strategy change that requires a fresh WF run.
    """
    from strategies.sp500_momentum import config
    assert config.ENTRY_EMA_FILTER is False
