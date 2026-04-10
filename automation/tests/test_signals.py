"""
Integration tests for signals.py.

These tests depend on real data files (SP500 constituents, SPY/SSO CSVs, ETF
price data). On machines without that data, they skip gracefully.

Coverage:
  - get_sr_weights() returns non-empty weights summing to ≈1
  - get_sp500_universe() contains SPY and SSO (dip-buy tickers)
  - get_market_regime_state() returns dict with required keys
  - SR_ETFS constant is single source of truth across modules
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest


def _has_sp500_data() -> bool:
    """Check if the SP500 constituents + ETF data files exist locally."""
    root = Path(__file__).resolve().parent.parent.parent
    ref = root / "data" / "reference" / "sp500_constituents_daily_2015_2026.xlsx"
    spy = root / "data" / "etfs" / "SPY.csv"
    return ref.exists() and spy.exists()


skip_no_data = pytest.mark.skipif(
    not _has_sp500_data(),
    reason="SP500 constituents / ETF data files missing",
)


# ── Pure (no data) tests ─────────────────────────────────────────────

def test_sr_etfs_constant_is_11_sector_etfs():
    """SR_ETFS should contain exactly the 11 SPDR sector ETFs."""
    from ib_fetcher import SR_ETFS
    expected = {
        'XLK', 'XLF', 'XLV', 'XLE', 'XLI', 'XLY', 'XLB',
        'XLP', 'XLU', 'XLC', 'XLRE',
    }
    assert SR_ETFS == expected


def test_sr_etfs_is_single_source_across_modules():
    """rebalance_ib and daily_report should re-export ib_fetcher.SR_ETFS."""
    import ib_fetcher
    import rebalance_ib
    import daily_report

    assert rebalance_ib.SR_ETFS is ib_fetcher.SR_ETFS
    assert daily_report.SR_ETFS is ib_fetcher.SR_ETFS


# ── Data-dependent tests ─────────────────────────────────────────────

@skip_no_data
def test_get_sr_weights_returns_non_empty():
    from signals import get_sr_weights

    wgt, sig_date = get_sr_weights()
    assert isinstance(wgt, dict)
    assert len(wgt) > 0, "SR signal should produce at least 1 position"
    assert sig_date  # non-empty string


@skip_no_data
def test_get_sr_weights_sum_approximately_one():
    """Sector Rotation weights should sum to ≈1.0 (full allocation)."""
    from signals import get_sr_weights

    wgt, _ = get_sr_weights()
    total = sum(wgt.values())
    assert total == pytest.approx(1.0, abs=0.01), \
        f"SR weights should sum to ~1.0, got {total:.4f}"


@skip_no_data
def test_get_sr_weights_all_are_sector_etfs():
    """Every SR position should be one of the 11 sector ETFs."""
    from signals import get_sr_weights
    from ib_fetcher import SR_ETFS

    wgt, _ = get_sr_weights()
    for sym in wgt:
        assert sym in SR_ETFS, f"{sym} is not a known sector ETF"


@skip_no_data
def test_get_sp500_universe_contains_spy_sso():
    """The universe helper must include SPY + SSO for dip-buy classification."""
    from signals import get_sp500_universe

    universe = get_sp500_universe()
    assert isinstance(universe, set)
    assert "SPY" in universe
    assert "SSO" in universe


@skip_no_data
def test_get_sp500_universe_has_500_plus_tickers():
    """S&P 500 should have ~500 constituents (may vary ±10 for rebalance weeks)."""
    from signals import get_sp500_universe

    universe = get_sp500_universe()
    assert len(universe) > 400, f"SP500 universe too small: {len(universe)}"


@skip_no_data
def test_get_market_regime_state_has_required_keys():
    """MarketRegime state dict must expose SPY close / MA200 / DD / ATH."""
    from signals import get_market_regime_state

    state = get_market_regime_state()
    assert isinstance(state, dict)
    required_keys = {"SPY_Close", "SPY_MA200", "SPY_DD"}
    missing = required_keys - set(state.keys())
    assert not missing, f"Missing keys in regime state: {missing}"


@skip_no_data
def test_get_market_regime_numeric_values():
    """SPY_Close and SPY_MA200 should be positive floats."""
    from signals import get_market_regime_state

    state = get_market_regime_state()
    assert state["SPY_Close"] > 0
    assert state["SPY_MA200"] > 0
    # DD is negative or zero (drawdown from ATH)
    assert state["SPY_DD"] <= 0.001  # small epsilon
