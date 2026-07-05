"""
Unit tests for the rotation decision core (strategies/sp500_momentum/rotation.py).

One brain (2026-07-05): live (signal.py) and backtest (backtester.py, plus the
frac_sizing_study subclass) all delegate the rotation decision here. These
tests pin the decision semantics; caller equivalence is covered by
characterization (live byte-diff / backtest bit-diff) and test_sp500_signal.py.

Pure functions — no IB, no data files.
"""

from __future__ import annotations

from strategies.sp500_momentum import config
from strategies.sp500_momentum import rotation


def row(ticker, adj_slope, price=100.0, exit_ema=90.0, atr_pct=0.02):
    return {"ticker": ticker, "adj_slope": adj_slope, "price": price,
            "exit_ema": exit_ema, "atr_pct": atr_pct}


def make_ranked(n=25, start_slope=1.4):
    """n rows, slopes descending from start_slope, all above EMA, none overheated."""
    return [row(f"T{i:02d}", start_slope - i * 0.02) for i in range(n)]


# ── rotation_sells ───────────────────────────────────────────────────────


def test_sell_when_rank_out_of_top_threshold():
    ranked = make_ranked(25)          # top 20 = T00..T19
    sells = rotation.rotation_sells(["T24"], ranked)
    assert sells == [("T24", f"排名跌出前{config.SELL_RANK_THRESHOLD}名")]


def test_sell_when_below_exit_ema_even_if_top_ranked():
    """Real numbers from the 2026-06-10 rotation log (CIEN)."""
    cien = row("CIEN", 1.139, price=439.34, exit_ema=508.37)
    ranked = [row("AMD", 1.30, price=521.54, exit_ema=372.51), cien] \
        + make_ranked(10, 1.0)
    sells = rotation.rotation_sells(["CIEN"], ranked)
    assert sells == [("CIEN", f"跌破EMA{config.EXIT_EMA}")]


def test_holding_missing_from_ranked_sold_as_rank_out():
    """Gap-filtered / ex-constituent holdings rank as absent → rank-out sell."""
    sells = rotation.rotation_sells(["GONE"], make_ranked(25))
    assert sells == [("GONE", f"排名跌出前{config.SELL_RANK_THRESHOLD}名")]


def test_keep_top_ranked_above_ema_not_sold():
    """Rank 20 (= threshold boundary) is still inside the survivor set."""
    sells = rotation.rotation_sells(["T00", "T19"], make_ranked(25))
    assert sells == []


# ── rotation_buys ────────────────────────────────────────────────────────


def test_adr0005_ema_sold_but_top_ranked_is_rebought():
    """ADR-0005 signature: rank beats EMA — the EMA-sold, still-top-ranked
    name reappears in buys the same round (= net keep). Deliberate,
    WF-validated behaviour (ENTRY_EMA_FILTER defaults False)."""
    cien = row("CIEN", 1.40, price=439.34, exit_ema=508.37)
    ranked = [cien] + make_ranked(10, 1.3)
    sells = rotation.rotation_sells(["CIEN"], ranked)
    assert sells == [("CIEN", f"跌破EMA{config.EXIT_EMA}")]
    kept = []                          # caller derives holdings − sells
    buys = rotation.rotation_buys(kept, ranked)
    assert buys[0] == "CIEN"


def test_entry_ema_filter_on_excludes_below_ema(monkeypatch):
    """Flag path kept for WF re-validation (robustness/wf_ema_entry_filter.py)."""
    monkeypatch.setattr(config, "ENTRY_EMA_FILTER", True)
    cien = row("CIEN", 1.40, price=439.34, exit_ema=508.37)
    ranked = [cien] + make_ranked(10, 1.3)
    buys = rotation.rotation_buys([], ranked)
    assert "CIEN" not in buys
    assert buys == ["T00", "T01", "T02", "T03"]


def test_buys_fill_in_rank_order_up_to_target():
    buys = rotation.rotation_buys(["T00", "T01"], make_ranked(25))
    assert buys == ["T02", "T03"]


def test_no_buys_when_book_full():
    assert rotation.rotation_buys(["A", "B", "C", "D"], make_ranked(25)) == []


def test_overheated_candidate_does_not_consume_slot():
    hot = row("HOT", 999.0)
    ranked = [hot] + make_ranked(10, 1.3)
    buys = rotation.rotation_buys([], ranked)
    assert "HOT" not in buys
    assert buys == ["T00", "T01", "T02", "T03"]


def test_max_adj_slope_none_disables_overheat_filter(monkeypatch):
    """Canonical None-guard (was backtester-only; live crashed on None)."""
    monkeypatch.setattr(config, "MAX_ADJ_SLOPE", None)
    hot = row("HOT", 99.0)
    ranked = [hot] + make_ranked(10, 1.3)
    buys = rotation.rotation_buys([], ranked)
    assert buys[0] == "HOT"


# ── atr_weights ──────────────────────────────────────────────────────────


def test_atr_weights_inverse_proportions():
    w = rotation.atr_weights([row("A", 1, atr_pct=0.01),
                              row("B", 1, atr_pct=0.03)])
    assert abs(w["A"] - 0.75) < 1e-12
    assert abs(w["B"] - 0.25) < 1e-12


def test_atr_weights_drop_invalid_rows():
    w = rotation.atr_weights([row("A", 1, atr_pct=0.02),
                              {"ticker": "NOATR"},
                              row("ZERO", 1, atr_pct=0.0)])
    assert w == {"A": 1.0}


def test_atr_weights_empty_when_no_valid():
    assert rotation.atr_weights([]) == {}
    assert rotation.atr_weights([{"ticker": "Z", "atr_pct": 0}]) == {}
