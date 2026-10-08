"""
Unit tests for rebalance_plan.build_order_list (REBALANCE_THRESHOLD filter).

原為 sync_position._build_order_list 的測試；候選 4 後差額計算唯一實作
搬到 rebalance_plan，scheduled rebalance 與 Sync Pos 共用 — 斷言逐字保留。

Coverage:
  - Drift < threshold → skipped (both BUY and SELL)
  - Drift ≥ threshold → kept
  - target=0 (full exit) → always executed even if small
  - current=0 (new entry) → always executed even if small
  - Other holdings (manual) never generate orders
  - Zero / invalid prices are skipped
  - PlannedOrder (=SyncOrder) dataclass fields populated correctly
"""

from __future__ import annotations

import pytest


# build_order_list reads sp_cfg.REBALANCE_THRESHOLD at runtime.
# Real value is 0.03 (3% NAV); we test with that assumption.


def test_delta_above_threshold_kept():
    """A 5% NAV drift should generate an order."""
    from rebalance_plan import build_order_list

    # NAV $100k, threshold 3% = $3000
    # XLB @ $100, delta = 50 shares = $5000 drift = 5% → kept
    orders = build_order_list(
        targets={"XLB": {"shares": 100, "strategy": "SR"}},
        current_pos={"XLB": 50},
        current_other_holdings=[],
        prices={"XLB": 100.0},
        nav=100_000,
    )
    assert len(orders) == 1
    assert orders[0].symbol == "XLB"
    assert orders[0].action == "BUY"
    assert orders[0].qty == 50
    assert orders[0].target == 100
    assert orders[0].current == 50
    assert orders[0].strategy == "SR"


def test_delta_below_threshold_skipped():
    """A 1% NAV drift should be filtered out."""
    from rebalance_plan import build_order_list

    # NAV $100k, threshold 3% = $3000
    # XLB @ $100, delta = 10 shares = $1000 drift = 1% → skipped
    orders = build_order_list(
        targets={"XLB": {"shares": 60, "strategy": "SR"}},
        current_pos={"XLB": 50},
        current_other_holdings=[],
        prices={"XLB": 100.0},
        nav=100_000,
    )
    assert orders == []


def test_full_exit_bypasses_threshold():
    """target=0 (清倉) must execute even if the notional is tiny."""
    from rebalance_plan import build_order_list

    # Current 1 share @ $100 = $100 (< 3% of $100k)
    # target=0 → must SELL even though below threshold
    orders = build_order_list(
        targets={},                        # no target
        current_pos={"XLB": 1},
        current_other_holdings=[],
        prices={"XLB": 100.0},
        nav=100_000,
    )
    assert len(orders) == 1
    assert orders[0].action == "SELL"
    assert orders[0].qty == 1
    assert orders[0].target == 0
    assert orders[0].current == 1
    # 當 targets 沒有該 sym，strategy 預設 'EXIT'
    assert orders[0].strategy == "EXIT"


def test_new_entry_bypasses_threshold():
    """current=0 (從無到有) must execute even if tiny."""
    from rebalance_plan import build_order_list

    orders = build_order_list(
        targets={"XLB": {"shares": 5, "strategy": "SR"}},  # $500
        current_pos={},                                       # no position
        current_other_holdings=[],
        prices={"XLB": 100.0},
        nav=100_000,                                          # $500 is 0.5%
    )
    assert len(orders) == 1
    assert orders[0].action == "BUY"
    assert orders[0].qty == 5
    assert orders[0].current == 0


def test_sell_delta_above_threshold():
    from rebalance_plan import build_order_list

    # Holding 100 sh, target 40 sh → SELL 60 sh × $100 = $6000 = 6% → kept
    orders = build_order_list(
        targets={"XLB": {"shares": 40, "strategy": "SR"}},
        current_pos={"XLB": 100},
        current_other_holdings=[],
        prices={"XLB": 100.0},
        nav=100_000,
    )
    assert len(orders) == 1
    assert orders[0].action == "SELL"
    assert orders[0].qty == 60


def test_other_holdings_ignored():
    """Manual positions (current_other_holdings) must never generate orders —
    even if they're not in targets."""
    from rebalance_plan import build_order_list

    orders = build_order_list(
        targets={},
        current_pos={"BTC": 100, "XLB": 10},  # both present
        current_other_holdings=["BTC"],         # BTC is manual
        prices={"BTC": 30.0, "XLB": 100.0},
        nav=100_000,
    )
    syms = {o.symbol for o in orders}
    assert "BTC" not in syms, "BTC is manual, should not be synced"
    # XLB current=10, target=0 → full exit bypasses threshold
    assert "XLB" in syms


def test_zero_price_skipped():
    from rebalance_plan import build_order_list

    orders = build_order_list(
        targets={"XLB": {"shares": 100, "strategy": "SR"}},
        current_pos={"XLB": 50},
        current_other_holdings=[],
        prices={"XLB": 0},
        nav=100_000,
    )
    assert orders == []


def test_multiple_orders_sorted_by_symbol():
    """Orders should be sorted alphabetically (per source code)."""
    from rebalance_plan import build_order_list

    orders = build_order_list(
        targets={
            "XLU": {"shares": 100, "strategy": "SR"},
            "XLB": {"shares": 100, "strategy": "SR"},
            "XLE": {"shares": 100, "strategy": "SR"},
        },
        current_pos={},
        current_other_holdings=[],
        prices={"XLB": 100.0, "XLE": 100.0, "XLU": 100.0},
        nav=100_000,
    )
    symbols = [o.symbol for o in orders]
    assert symbols == ["XLB", "XLE", "XLU"]


def test_exact_threshold_edge_case():
    """Drift exactly AT threshold (3%) — current implementation uses `<`,
    so 3.0% should NOT be filtered (kept)."""
    from rebalance_plan import build_order_list

    # 30 shares × $100 = $3000 = exactly 3% of $100k
    orders = build_order_list(
        targets={"XLB": {"shares": 80, "strategy": "SR"}},
        current_pos={"XLB": 50},
        current_other_holdings=[],
        prices={"XLB": 100.0},
        nav=100_000,
    )
    # drift_pct = 3000/100000 = 0.03; rebal_thresh = 0.03
    # `drift_pct < rebal_thresh` → False → kept
    assert len(orders) == 1


def test_no_delta_no_order():
    """target == current → no order generated."""
    from rebalance_plan import build_order_list

    orders = build_order_list(
        targets={"XLB": {"shares": 50, "strategy": "SR"}},
        current_pos={"XLB": 50},
        current_other_holdings=[],
        prices={"XLB": 100.0},
        nav=100_000,
    )
    assert orders == []


def test_sync_order_alias_preserved():
    """sync_position.SyncOrder 舊名必須仍指向決策層的 PlannedOrder
    （SyncPlan 的欄位型別沿用這個名字）。"""
    from rebalance_plan import PlannedOrder
    from sync_position import SyncOrder

    assert SyncOrder is PlannedOrder


def test_ignore_threshold_full_sync_includes_small_deltas():
    """Sync Pos 全同步語意（2026-07-06 定案）：ignore_threshold=True 時
    門檻內差額也列單 — 凍結遺留的 <3% 殘餘因此清得掉；scheduled
    rebalance 走預設 False，3% 換手抑制不受影響。"""
    from rebalance_plan import build_order_list

    # 1% NAV 漂移：預設會被門檻濾掉（上面 test_delta_below_threshold_skipped
    # 釘住），全同步模式必須列單
    orders = build_order_list(
        targets={"XLB": {"shares": 60, "strategy": "SR"}},
        current_pos={"XLB": 50},
        current_other_holdings=[],
        prices={"XLB": 100.0},
        nav=100_000,
        ignore_threshold=True,
    )
    assert len(orders) == 1
    assert orders[0].action == "BUY"
    assert orders[0].qty == 10


def test_ignore_threshold_keeps_manual_and_aligned_untouched():
    """全同步只放寬門檻 — 手動持倉照樣排除、對齊的照樣不動。"""
    from rebalance_plan import build_order_list

    orders = build_order_list(
        targets={"XLB": {"shares": 50, "strategy": "SR"}},
        current_pos={"XLB": 50, "BTC": 3},
        current_other_holdings=["BTC"],
        prices={"XLB": 100.0, "BTC": 30.0},
        nav=100_000,
        ignore_threshold=True,
    )
    assert orders == []
