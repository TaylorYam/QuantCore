"""
Unit tests for rebalance_ib.combine_targets.

Coverage:
  - NAV × alloc × weight / price share calculation
  - Ctx allocs override (not module globals)
  - Invalid price handling (zero, negative, NaN, None)
  - SR / SP500 universe separation
  - Overlap (same symbol in both strategies)
"""

from __future__ import annotations

import math

import pytest


def _make_ctx(alloc_sr: float = 0.80, alloc_sp500: float = 0.50):
    """Build an AccountCtx directly without going through from_name()."""
    from account_ctx import AccountCtx
    return AccountCtx(
        name="Test",
        account="U00000000",
        host="127.0.0.1",
        port=4001,
        client_id=20,
        dry_run=True,
        order_type="MOO",
        outside_rth=False,
        wait_fill_sec=30,
        alloc_sr=alloc_sr,
        alloc_sp500=alloc_sp500,
        price_feed_port=4002,
        tg_token="",
        tg_chat_id="",
    )


def test_sr_target_shares_simple():
    from rebalance_ib import combine_targets
    ctx = _make_ctx(alloc_sr=0.80, alloc_sp500=0.50)

    sr_wgt = {"XLK": 1.0}
    sp500_wgt = {}
    prices = {"XLK": 100.0}

    targets = combine_targets(nav=100_000, sr_wgt=sr_wgt, sp500_wgt=sp500_wgt,
                              prices=prices, ctx=ctx)

    # NAV $100k × 0.80 × 1.0 / $100 = 800 shares
    assert "XLK" in targets
    assert targets["XLK"]["shares"] == 800
    assert targets["XLK"]["strategy"] == "SR"


def test_sp500_target_shares_simple():
    from rebalance_ib import combine_targets
    ctx = _make_ctx(alloc_sr=0.80, alloc_sp500=0.50)

    sr_wgt = {}
    sp500_wgt = {"AAPL": 1.0}
    prices = {"AAPL": 150.0}

    targets = combine_targets(nav=100_000, sr_wgt=sr_wgt, sp500_wgt=sp500_wgt,
                              prices=prices, ctx=ctx)

    # $100k × 0.50 × 1.0 / $150 = 333 shares (int truncation)
    assert targets["AAPL"]["shares"] == 333
    assert targets["AAPL"]["strategy"] == "SP500"


def test_ctx_allocs_override():
    """Verify combine_targets reads from ctx, not from module globals.
    If ctx says 0.70/0.30, shares should use those, not the config.json 0.80/0.50."""
    from rebalance_ib import combine_targets

    ctx_custom = _make_ctx(alloc_sr=0.70, alloc_sp500=0.30)
    sr_wgt = {"XLK": 1.0}
    sp500_wgt = {"AAPL": 1.0}
    prices = {"XLK": 100.0, "AAPL": 150.0}

    targets = combine_targets(nav=100_000, sr_wgt=sr_wgt, sp500_wgt=sp500_wgt,
                              prices=prices, ctx=ctx_custom)

    # $100k × 0.70 × 1.0 / $100 = 700 (not 800)
    assert targets["XLK"]["shares"] == 700
    # $100k × 0.30 × 1.0 / $150 = 200 (not 333)
    assert targets["AAPL"]["shares"] == 200


def test_zero_price_is_skipped():
    from rebalance_ib import combine_targets
    ctx = _make_ctx()

    targets = combine_targets(
        nav=100_000,
        sr_wgt={"XLK": 1.0},
        sp500_wgt={},
        prices={"XLK": 0},
        ctx=ctx,
    )
    assert "XLK" not in targets


def test_negative_price_is_skipped():
    from rebalance_ib import combine_targets
    ctx = _make_ctx()

    targets = combine_targets(
        nav=100_000,
        sr_wgt={"XLK": 1.0},
        sp500_wgt={},
        prices={"XLK": -10},
        ctx=ctx,
    )
    assert "XLK" not in targets


def test_nan_price_is_skipped():
    """This is the bug we hit when Yvonne had no market data — NaN price
    was flowing into int(NAV * alloc / NaN) and crashing."""
    from rebalance_ib import combine_targets
    ctx = _make_ctx()

    targets = combine_targets(
        nav=100_000,
        sr_wgt={"XLK": 1.0},
        sp500_wgt={},
        prices={"XLK": float('nan')},
        ctx=ctx,
    )
    assert "XLK" not in targets


def test_none_price_is_skipped():
    from rebalance_ib import combine_targets
    ctx = _make_ctx()

    targets = combine_targets(
        nav=100_000,
        sr_wgt={"XLK": 1.0},
        sp500_wgt={},
        prices={"XLK": None},
        ctx=ctx,
    )
    assert "XLK" not in targets


def test_missing_symbol_is_skipped():
    """If a symbol has no entry in prices dict, it's treated as 0 → skipped."""
    from rebalance_ib import combine_targets
    ctx = _make_ctx()

    targets = combine_targets(
        nav=100_000,
        sr_wgt={"XLK": 1.0},
        sp500_wgt={},
        prices={},  # no XLK
        ctx=ctx,
    )
    assert "XLK" not in targets


def test_sr_sp500_overlap_merged():
    """Rare case: same symbol in both SR and SP500 weights (e.g., SPY dip-buy
    while also being in SR somehow). Should sum shares + mark as 'SR+SP500'."""
    from rebalance_ib import combine_targets
    ctx = _make_ctx(alloc_sr=0.80, alloc_sp500=0.50)

    targets = combine_targets(
        nav=100_000,
        sr_wgt={"SPY": 0.5},      # 50% of SR allocation
        sp500_wgt={"SPY": 0.3},    # 30% of SP500 allocation
        prices={"SPY": 500.0},
        ctx=ctx,
    )
    # SR: 100k × 0.80 × 0.5 / 500 = 80
    # SP500: 100k × 0.50 × 0.3 / 500 = 30
    # Total: 110
    assert targets["SPY"]["shares"] == 110
    assert targets["SPY"]["strategy"] == "SR+SP500"


def test_full_portfolio_multiple_tickers():
    """4 SR ETFs at real-world weights (XLB 35/XLE 25/XLU 20/XLRE 20)."""
    from rebalance_ib import combine_targets
    ctx = _make_ctx(alloc_sr=0.80, alloc_sp500=0.50)

    targets = combine_targets(
        nav=100_000,
        sr_wgt={"XLB": 0.35, "XLE": 0.25, "XLU": 0.20, "XLRE": 0.20},
        sp500_wgt={},
        prices={"XLB": 50.0, "XLE": 60.0, "XLU": 45.0, "XLRE": 42.0},
        ctx=ctx,
    )

    # All 4 should be present
    assert set(targets.keys()) == {"XLB", "XLE", "XLU", "XLRE"}
    # XLB: 100k × 0.80 × 0.35 / 50 = 560
    assert targets["XLB"]["shares"] == 560
    # XLE: 100k × 0.80 × 0.25 / 60 = 333 (int trunc from 333.33)
    assert targets["XLE"]["shares"] == 333
    # All tagged as SR
    for sym in targets:
        assert targets[sym]["strategy"] == "SR"
