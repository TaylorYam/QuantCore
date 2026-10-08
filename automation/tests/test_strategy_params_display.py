"""策略參數顯示計算（#150）與 Allocation 調整的檢查（#220）：Panel 用的純函式。"""
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


# ── 調整 Allocation（#220）：驗證與確認文字 ──────────────────────────

def test_allocation_change_lists_the_new_values_and_only_touches_this_account():
    from strategy_params_display import allocation_change

    c = allocation_change("Taylor", {"alloc_sr": 0.80, "alloc_sp500": 0.50, "alloc_skewls": 0.0},
                          (0.75, 0.45, 0.0))
    assert c.changed and not c.error and not c.first_enable_skewls
    assert c.updates() == {"alloc_sr": 0.75, "alloc_sp500": 0.45, "alloc_skewls": 0.0}
    text = c.confirm_text()
    for part in ("Taylor", "SR", "75%", "SP500", "45%", "Skew LS", "0%",
                 "120% (1.2x)", "Borrow: 20%", "只改這個帳戶"):
        assert part in text
    assert "放空" not in text


def test_allocation_values_are_rounded_like_the_tk_sliders():
    from strategy_params_display import allocation_change

    c = allocation_change("Rosa", {}, (0.7000000001, 0.35, 0.1499999))
    assert c.values == (0.70, 0.35, 0.15)
    assert c.updates() == {"alloc_sr": 0.70, "alloc_sp500": 0.35, "alloc_skewls": 0.15}


def test_no_borrow_line_at_or_below_one():
    from strategy_params_display import allocation_change

    text = allocation_change("Rosa", {}, (0.5, 0.3, 0.2)).confirm_text()
    assert "100% (1.0x)" in text and "No leverage" in text and "Borrow" not in text


def test_same_values_are_not_a_change():
    from strategy_params_display import allocation_change

    cfg = {"alloc_sr": 0.75, "alloc_sp500": 0.45, "alloc_skewls": 0.30}
    assert not allocation_change("Yvonne", cfg, (0.75, 0.45, 0.30)).changed
    assert not allocation_change("Taylor", {}, (0.80, 0.50, 0.0)).changed   # 沒寫 = 預設值
    assert allocation_change("Taylor", {}, (0.80, 0.50, 0.05)).changed


def test_total_above_250_percent_is_refused():
    from strategy_params_display import MAX_TOTAL_ALLOCATION, allocation_change, allocation_error

    assert MAX_TOTAL_ALLOCATION == 2.5
    c = allocation_change("Taylor", {}, (1.0, 1.0, 0.55))
    assert "255%" in c.error and "250%" in c.error
    assert not allocation_change("Taylor", {}, (1.0, 1.0, 0.5)).error      # 剛好 250% 可以
    assert not allocation_change("Taylor", {}, (0.9, 0.85, 0.75)).error    # 浮點加總不誤擋
    assert allocation_error((1.0, 1.0, 0.55)) == c.error and allocation_error((0.8, 0.5, 0)) == ""


def test_first_enable_of_skewls_warns_about_the_xlc_short():
    from strategy_params_display import allocation_change

    c = allocation_change("Taylor", {"alloc_skewls": 0.0}, (0.8, 0.5, 0.2))
    assert c.first_enable_skewls
    assert "首次啟用" in c.confirm_text() and "放空 XLC" in c.confirm_text()
    assert "margin" in c.confirm_text()
    assert allocation_change("Taylor", {}, (0.8, 0.5, 0.2)).first_enable_skewls  # 沒寫 = 0
    assert not allocation_change("Rosa", {"alloc_skewls": 0.2}, (0.5, 0.3, 0.3)).first_enable_skewls
    assert not allocation_change("Rosa", {"alloc_skewls": 0.0}, (0.5, 0.3, 0.0)).first_enable_skewls
