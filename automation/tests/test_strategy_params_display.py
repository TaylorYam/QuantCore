"""策略參數顯示計算（#150）：Tk StrategyParamsView 與新 Panel 共用的純函式。"""
from __future__ import annotations

from strategy_params_display import allocations_from_cfg, strategy_params


def rows(p):
    return [(r.label, r.text, r.color) for r in p.rows]


def test_each_strategy_allocation_is_a_whole_percent_with_its_color():
    p = strategy_params(0.75, 0.45, 0.30)
    assert rows(p) == [("SR", "75%", "blue"), ("SP500", "45%", "green"), ("Skew LS", "30%", "red")]
    assert [r.value for r in p.rows] == [0.75, 0.45, 0.30]


def test_leverage_is_the_sum_of_allocations_with_borrow_when_above_one():
    p = strategy_params(0.80, 0.50, 0.0)
    assert (p.leverage_text, p.leverage_color, p.detail) == ("130% (1.3x)", "orange", "Borrow: 30%")


def test_exactly_one_or_below_is_no_leverage():
    assert (strategy_params(0.5, 0.3, 0.2).leverage_text,
            strategy_params(0.5, 0.3, 0.2).detail) == ("100% (1.0x)", "No leverage")
    p = strategy_params(0.6, 0.3, 0.1)                   # 浮點加總 0.9999… 仍是 No leverage
    assert (p.leverage_text, p.leverage_color, p.detail) == ("100% (1.0x)", "green", "No leverage")
    assert strategy_params(0, 0, 0).leverage_text == "0% (0.0x)"


def test_skewls_defaults_to_zero():
    assert rows(strategy_params(0.8, 0.5))[2] == ("Skew LS", "0%", "red")


def test_allocations_from_config_use_the_same_defaults_as_the_tk_panel():
    assert allocations_from_cfg({}) == (0.80, 0.50, 0.0)
    cfg = {"alloc_sr": 0.75, "alloc_sp500": 0.45, "alloc_skewls": 0.30, "leverage": 9}
    assert allocations_from_cfg(cfg) == (0.75, 0.45, 0.30)   # leverage 欄位不採用，永遠是加總
