"""
Characterization tests for strategies.sp500_momentum.signal.get_latest_weights_live.

These pin the SP500 live-weight state machine at fixed historical dates, so the
behaviour survives the candidate-1 extraction (and any future change). They are
deterministic because the regime branches key off SPY Close vs MA200 / drawdown,
which is stable history — and because the function is now PURE w.r.t. state
(state in → new_state out, no file I/O), which is exactly what makes it testable
through its own interface.

On machines without the SP500 data files they skip gracefully (same guard as
test_signals.py).
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest


def _has_sp500_data() -> bool:
    root = Path(__file__).resolve().parent.parent.parent
    ref = root / "data" / "reference" / "sp500_constituents_daily.xlsx"
    spy = root / "data" / "etfs" / "SPY.csv"
    return ref.exists() and spy.exists()


skip_no_data = pytest.mark.skipif(
    not _has_sp500_data(),
    reason="SP500 constituents / ETF data files missing",
)


def _fresh() -> dict:
    from strategies.sp500_momentum.state import DEFAULT_STATE
    return copy.deepcopy(DEFAULT_STATE)


# ── Purity / idempotency (the candidate-1 payoff) ─────────────────────

@skip_no_data
def test_does_not_mutate_caller_state():
    """The function deep-copies state; the caller's dict must be untouched."""
    from strategies.sp500_momentum.signal import get_latest_weights_live

    state = _fresh()
    snapshot = copy.deepcopy(state)
    get_latest_weights_live(["AAPL"], state, as_of_date="2022-06-17")
    assert state == snapshot, "input state dict was mutated (purity violated)"


@skip_no_data
def test_idempotent_same_inputs_same_outputs():
    """Same (holdings, state, date) → same weights and same new_state."""
    from strategies.sp500_momentum.signal import get_latest_weights_live

    w1, d1, s1 = get_latest_weights_live(["AAPL"], _fresh(), as_of_date="2022-06-17")
    w2, d2, s2 = get_latest_weights_live(["AAPL"], _fresh(), as_of_date="2022-06-17")
    assert w1 == w2 and d1 == d2 and s1 == s2


@skip_no_data
def test_signal_date_is_timestamp_at_or_before_as_of():
    import pandas as pd
    from strategies.sp500_momentum.signal import get_latest_weights_live

    _, sig_date, _ = get_latest_weights_live([], _fresh(), as_of_date="2024-06-07")
    assert isinstance(sig_date, pd.Timestamp)
    assert sig_date <= pd.Timestamp("2024-06-07")


# ── Bear / dip-buy branch (deterministic on SPY drawdown) ─────────────

@skip_no_data
def test_bear_three_dip_levels_covid():
    """2020-03 COVID crash: DD ≥ 25% → all 3 levels → full dip allocation."""
    from strategies.sp500_momentum.signal import get_latest_weights_live
    from strategies.sp500_momentum import config as sp_cfg

    A = sp_cfg.DIP_BUY_ALLOCATIONS
    expected = A[0.15] + A[0.20] + A[0.25]

    wgt, _, ns = get_latest_weights_live(["AAPL", "MSFT"], _fresh(), as_of_date="2020-03-27")
    assert wgt == {sp_cfg.DIP_BUY_TICKER: expected}
    assert ns["dip_state"] == {"0.15": True, "0.20": True, "0.25": True}
    assert ns["bull_weeks_counter"] == 0


@skip_no_data
def test_bear_two_dip_levels():
    """2022-06: DD ≥ 20% (not 25%) → first two levels."""
    from strategies.sp500_momentum.signal import get_latest_weights_live
    from strategies.sp500_momentum import config as sp_cfg

    A = sp_cfg.DIP_BUY_ALLOCATIONS
    expected = A[0.15] + A[0.20]

    wgt, _, ns = get_latest_weights_live(["AAPL"], _fresh(), as_of_date="2022-06-17")
    assert wgt == {sp_cfg.DIP_BUY_TICKER: expected}
    assert ns["dip_state"] == {"0.15": True, "0.20": True, "0.25": False}


@skip_no_data
def test_bear_one_dip_level():
    """2025-04 tariff selloff: DD ≥ 15% (not 20%) → first level only."""
    from strategies.sp500_momentum.signal import get_latest_weights_live
    from strategies.sp500_momentum import config as sp_cfg

    expected = sp_cfg.DIP_BUY_ALLOCATIONS[0.15]

    wgt, _, ns = get_latest_weights_live(["AAPL"], _fresh(), as_of_date="2025-04-11")
    assert wgt == {sp_cfg.DIP_BUY_TICKER: expected}
    assert ns["dip_state"] == {"0.15": True, "0.20": False, "0.25": False}


# ── Waiting branch (bull pending) ─────────────────────────────────────

@skip_no_data
def test_waiting_branch_returns_empty_and_advances_counter():
    """SPY ≥ MA200 but bull not yet confirmed (counter 0→1) → hold, no positions."""
    from strategies.sp500_momentum.signal import get_latest_weights_live

    wgt, _, ns = get_latest_weights_live(["AAPL"], _fresh(), as_of_date="2023-03-17")
    assert wgt == {}
    assert ns["bull_weeks_counter"] == 1  # advanced once, < 2 so not confirmed


# ── Bull confirmed rotation (structural invariants) ───────────────────

@skip_no_data
def test_bull_confirmed_rotation_structure():
    """Confirmed bull + rotation week → ≤ TARGET_HOLDINGS names, weights sum ≈ 1."""
    from strategies.sp500_momentum.signal import get_latest_weights_live
    from strategies.sp500_momentum import config as sp_cfg

    state = _fresh()
    state["bull_weeks_counter"] = 2          # already confirmed
    state["last_rebal_date"] = "2000-01-01"  # force a fresh count this call

    wgt, _, ns = get_latest_weights_live([], state, as_of_date="2024-06-07")
    assert 0 < len(wgt) <= sp_cfg.TARGET_HOLDINGS
    assert all(w > 0 for w in wgt.values())
    assert sum(wgt.values()) == pytest.approx(1.0, abs=1e-6)
    assert ns["bull_weeks_counter"] >= 2     # still confirmed


@skip_no_data
def test_already_counted_does_not_advance_counter():
    """If last_rebal_date == computed signal_date, the counter is not re-advanced."""
    from strategies.sp500_momentum.signal import get_latest_weights_live

    # first call to discover the signal_date for this as_of
    _, sig_date, _ = get_latest_weights_live([], _fresh(), as_of_date="2024-06-07")

    state = _fresh()
    state["bull_weeks_counter"] = 1
    state["last_rebal_date"] = str(sig_date.date())  # mark as already counted

    _, _, ns = get_latest_weights_live([], state, as_of_date="2024-06-07")
    assert ns["bull_weeks_counter"] == 1  # unchanged


# ── get_preview_weights (state-free preview core, shared by 3 UI tools) ──

@skip_no_data
def test_preview_bull_rotation_structure():
    """Bull date, no holdings → ≤ TARGET_HOLDINGS new picks, weights sum ≈ 1."""
    from strategies.sp500_momentum.signal import get_preview_weights
    from strategies.sp500_momentum import config as sp_cfg

    weights, info = get_preview_weights([], as_of_date="2024-06-07")
    assert 0 < len(weights) <= sp_cfg.TARGET_HOLDINGS
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-6)
    assert all(info[t]["status"] == "new" for t in weights)


@skip_no_data
def test_preview_bear_returns_sentinel():
    """Bear date → no positions and a _BEAR sentinel (no state machine, no dip-buy)."""
    from strategies.sp500_momentum.signal import get_preview_weights

    weights, info = get_preview_weights(["AAPL"], as_of_date="2022-06-17")
    assert weights == {}
    assert info == {"_BEAR": {"status": "bear"}}


@skip_no_data
def test_preview_not_constituent_flagged():
    """A holding that isn't an S&P 500 constituent is flagged, not silently dropped."""
    from strategies.sp500_momentum.signal import get_preview_weights

    _, info = get_preview_weights(["FOOBARXYZ"], as_of_date="2024-06-07")
    assert info["FOOBARXYZ"]["status"] == "not_constituent"


@skip_no_data
def test_preview_is_state_free():
    """The preview takes no state and mutates nothing — pure (holdings, date) -> view."""
    from strategies.sp500_momentum.signal import get_preview_weights

    w1, i1 = get_preview_weights(["AMD"], as_of_date="2024-06-07")
    w2, i2 = get_preview_weights(["AMD"], as_of_date="2024-06-07")
    assert w1 == w2 and i1 == i2
