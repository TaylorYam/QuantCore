"""
Rebalance 決策層（rebalance_plan）— 事故 regression + mode 矩陣 pin tests。

四件歷史事故永久考題化（都發生在同一決策帶、當時零測試）：
  1. SkewLS inverse ETF 凍結（2026-06-01 起 35 天，兩次 TRIGGER 單腿執行；
     fix f327d66）→ classify 不得把 inverse 歸「其他」、差額必須出單
  2. CIEN stale-universe 誤分類（2026-01）→ [[Universe]] = 歷史聯集語意
  3. MU floor 低配（部署率 82~89%，2026-07-05 改 round）→ SP500 sizing pin
  4. 週二舊訊號 whipsaw（CIEN 2026-06，ADR-0004）→ 非 sp500 mode 的
     hold 防護：SP500 訊號不進 combine、持股 target=current

另含 SkewLS threshold gate 決策矩陣與 re-export 相容 pins。
"""

from __future__ import annotations

import pytest


def _make_ctx(alloc_sr: float = 0.80, alloc_sp500: float = 0.50,
              alloc_skewls: float = 0.0):
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
        alloc_skewls=alloc_skewls,
        price_feed_port=4002,
    )


# ── 事故 1：SkewLS inverse ETF 凍結 ──────────────────────────────────

def test_classify_inverse_etf_is_not_other():
    """持有的 inverse ETF 必須不被歸入「其他」（歸其他 = 永久凍結）。"""
    from rebalance_plan import classify_holdings

    split = classify_holdings(
        {'REW': 4.0, 'ERY': 16.0, 'XLK': 10.0, 'MU': 1.0, 'BTC': 5.0},
        sp500_universe={'MU'},
    )
    assert 'REW' not in split.other
    assert 'ERY' not in split.other
    assert split.other == ['BTC']          # 真正的手動持倉仍被排除
    assert split.sr == ['XLK']
    assert split.sp500 == ['MU']


def test_inverse_position_adjusts_not_frozen():
    """2026-06 凍結事故 pin：持有 inverse、target 漂移 ≥3% NAV → 必須出單。

    數字取自實況：Yvonne REW 持 85 股、hold target 263 股 @ ~$20。
    """
    from rebalance_plan import classify_holdings, build_order_list

    positions = {'REW': 85.0}
    split = classify_holdings(positions, sp500_universe=set())
    orders = build_order_list(
        targets={'REW': {'shares': 263, 'strategy': 'SkewLS'}},
        current_pos=positions,
        current_other_holdings=split.other,
        prices={'REW': 20.0},
        nav=100_000,
    )
    # delta +178sh × $20 = $3,560 = 3.56% NAV ≥ 3% → BUY（凍結時代 = 零單）
    assert [(o.symbol, o.action, o.qty) for o in orders] == [('REW', 'BUY', 178)]


def test_inverse_full_exit_executes():
    """inverse 清倉（target=0）必須執行 — 凍結時代連出場都出不了。"""
    from rebalance_plan import classify_holdings, build_order_list

    positions = {'ERY': 16.0}
    split = classify_holdings(positions, sp500_universe=set())
    orders = build_order_list(
        targets={},
        current_pos=positions,
        current_other_holdings=split.other,
        prices={'ERY': 13.0},
        nav=100_000,
    )
    assert [(o.symbol, o.action, o.qty) for o in orders] == [('ERY', 'SELL', 16)]


# ── 事故 2：CIEN stale-universe 誤分類（[[Universe]] 語意）─────────────

def test_removed_constituent_still_classified_sp500():
    """[[Universe]] = 歷史聯集：已被移出 SP500 的持股仍歸 SP500 不歸其他。

    否則移除日起持股凍結（CIEN 2026-01 教訓）— caller 必須傳歷史聯集
    （signals 的 sp500_universe(historical=True)）。
    """
    from rebalance_plan import classify_holdings

    split = classify_holdings({'CIEN': 10.0}, sp500_universe={'CIEN', 'MU'})
    assert split.sp500 == ['CIEN']
    assert split.other == []


def test_never_constituent_is_other():
    """從未屬於任何策略的持股 = 手動，永不調整。"""
    from rebalance_plan import classify_holdings

    split = classify_holdings({'GLD': 10.0}, sp500_universe={'MU'})
    assert split.other == ['GLD']


# ── 事故 3：MU floor 低配（SP500 round sizing）───────────────────────

def test_sp500_round_sizing_regression():
    """高價股 × 小 sleeve：SP500 必須 int(round()) 而非 floor。

    1.55 股 → round 2 股；floor 時代給 1 股（−35% 部位，MU worst −45%）。
    SR 維持 floor（int 截斷）— per-strategy 取整不同是設計（ADR-0008）。
    """
    from rebalance_plan import combine_targets

    ctx = _make_ctx(alloc_sr=0.80, alloc_sp500=0.50)
    nav = 100_000
    # SP500: 100000 × 0.5 × 0.25 / 8065 = 1.5498 → round 2（floor 1）
    # SR:    100000 × 0.8 × 0.10 / 5150 = 1.5534 → floor 1（維持）
    targets = combine_targets(
        nav=nav,
        sr_wgt={'XLK': 0.10},
        sp500_wgt={'MU': 0.25},
        prices={'MU': 8065.0, 'XLK': 5150.0},
        ctx=ctx,
    )
    assert targets['MU']['shares'] == 2
    assert targets['XLK']['shares'] == 1


# ── 事故 4：週二舊訊號 whipsaw（ADR-0004；hold 防護）──────────────────

def test_non_sp500_mode_holds_sp500_positions():
    """mode=sr（週二）：SP500 訊號不進 combine，持股 target=current → 零 SP500 單。

    ADR-0004 whipsaw：週二 --mode all 用舊 SP500 訊號把停損掉的 CIEN 買回。
    hold 防護 = 非 sp500 mode 下 SP500 持股永遠原封不動。
    """
    from rebalance_plan import (apply_sp500_hold, build_order_list,
                                classify_holdings, combine_targets)

    ctx = _make_ctx(alloc_sr=0.80, alloc_sp500=0.50)
    positions = {'CIEN': 7.0, 'XLK': 10.0}
    prices = {'CIEN': 80.0, 'XLK': 250.0}
    split = classify_holdings(positions, sp500_universe={'CIEN'})

    targets = combine_targets(nav=100_000, sr_wgt={'XLK': 1.0}, sp500_wgt={},
                              prices=prices, ctx=ctx)
    targets = apply_sp500_hold(targets, run_sp500=False,
                               current_sp500_holdings=split.sp500,
                               positions=positions)
    assert targets['CIEN'] == {'shares': 7, 'strategy': 'SP500 (hold)',
                               'alloc_w': 0}

    orders = build_order_list(targets, positions, split.other, prices,
                              nav=100_000)
    assert all(o.symbol != 'CIEN' for o in orders), "hold 模式不得動 SP500 持股"


def test_sp500_hold_overlap_keeps_combined_target():
    """罕見 overlap（dip-buy SPY 同時被 SR 用）：保留 combined target，只附註 tag。"""
    from rebalance_plan import apply_sp500_hold

    targets = {'SPY': {'shares': 100, 'strategy': 'SR', 'alloc_w': 0.8}}
    out = apply_sp500_hold(targets, run_sp500=False,
                           current_sp500_holdings=['SPY'],
                           positions={'SPY': 40.0})
    assert out['SPY']['shares'] == 100          # SR 貢獻不被覆蓋
    assert out['SPY']['strategy'] == 'SR|SP500-hold'


def test_sp500_mode_no_hold_override():
    """run_sp500=True：targets 原樣通過（訊號當家）。"""
    from rebalance_plan import apply_sp500_hold

    targets = {'MU': {'shares': 3, 'strategy': 'SP500', 'alloc_w': 0.125}}
    out = apply_sp500_hold(dict(targets), run_sp500=True,
                           current_sp500_holdings=['MU'],
                           positions={'MU': 1.0})
    assert out == targets


# ── SkewLS threshold gate 決策矩陣 ────────────────────────────────────

_STATE = {'last_target': {'REW': 0.04}, 'last_target_date': '2026-06-11'}


def test_skewls_trigger_uses_today_target():
    from rebalance_plan import select_skewls_weights

    sk = select_skewls_weights(True, 0.3, dict(_STATE),
                               today_target={'REW': 0.50},
                               today_signal_date='2026-07-06')
    assert sk.triggered is True
    assert sk.wgt == {'REW': 0.50}
    assert sk.signal_date == '2026-07-06'
    assert sk.delta is not None and sk.delta > sk.threshold


def test_skewls_hold_uses_state_target_and_date():
    from rebalance_plan import select_skewls_weights

    sk = select_skewls_weights(True, 0.3, dict(_STATE),
                               today_target={'REW': 0.05},
                               today_signal_date='2026-07-06')
    assert sk.triggered is False
    assert sk.wgt == {'REW': 0.04}
    assert sk.signal_date == '2026-06-11'


def test_skewls_not_checked_in_other_modes():
    """sr/sp500/both mode：不檢查 threshold、一律 last_target —
    確保 SR/SP500 rebal 不誤砍 SkewLS 倉位。"""
    from rebalance_plan import select_skewls_weights

    sk = select_skewls_weights(False, 0.3, dict(_STATE))
    assert sk.triggered is None
    assert sk.delta is None
    assert sk.wgt == {'REW': 0.04}
    assert sk.signal_date == '2026-06-11'


def test_skewls_disabled_when_alloc_zero():
    from rebalance_plan import select_skewls_weights

    sk = select_skewls_weights(True, 0.0, {},
                               today_target={'REW': 0.50},
                               today_signal_date='x')
    assert sk.wgt == {}
    assert sk.triggered is None


# ── 雜項 pins ─────────────────────────────────────────────────────────

def test_sp500_placeholder_weights():
    from rebalance_plan import sp500_placeholder_weights

    assert sp500_placeholder_weights(['MU', 'STX']) == {'MU': 0, 'STX': 0}
    assert sp500_placeholder_weights([]) == {}


def test_plan_symbols_union_sorted():
    from rebalance_plan import plan_symbols

    syms = plan_symbols({'XLK': 1.0}, {'MU': 0}, {'REW': 0.1}, {'BTC': 5.0})
    assert syms == ['BTC', 'MU', 'REW', 'XLK']


def test_combine_targets_reexport_from_rebalance_ib():
    """tests 與舊 callers 的 import 路徑（rebalance_ib.combine_targets）不變。"""
    import rebalance_ib
    import rebalance_plan

    assert rebalance_ib.combine_targets is rebalance_plan.combine_targets
