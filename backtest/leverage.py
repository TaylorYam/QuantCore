"""
QuantCore — 槓桿模組
將策略報酬率序列套用固定槓桿（constant-leverage），扣除 IB 融資利息。

IB Pro USD 融資利率（2026-03 當前）
------------------------------------
  $0      – $100,000  : 5.14%  (BM + 1.5%)
  $100,000 – $1,000,000: 4.64%  (BM + 1.0%)
  $1,000,000+          : 4.39%  (BM + 0.75%)

槓桿模型（constant-leverage）
------------------------------
  gross_ret  = leverage × strategy_daily_ret
  borrow_pct = (leverage - 1)                      ← 每日借款佔本金比例
  daily_cost = borrow_pct × blended_rate / 252     ← 每日融資成本
  net_ret    = gross_ret - daily_cost

注意：constant-leverage 假設每日再平衡以維持固定槓桿倍數，
      現實中 IB 不強制每日調整，但這是回測的標準保守假設。
"""

import numpy as np
import pandas as pd


# ── IB Pro USD 融資利率分級 ──────────────────────────────────────────────────

IB_TIERS_USD = [
    (100_000,        0.0514),   # $0 – $100K
    (1_000_000,      0.0464),   # $100K – $1M
    (50_000_000,     0.0439),   # $1M – $50M
    (250_000_000,    0.0414),   # $50M – $250M
    (float("inf"),   0.0414),   # > $250M
]


def ib_blended_rate(borrow_amount: float) -> float:
    """
    計算 IB Pro USD 混合融資利率（分級計費）。

    Parameters
    ----------
    borrow_amount : float，借款金額（美元）

    Returns
    -------
    blended : float，年化加權平均利率
    """
    if borrow_amount <= 0:
        return 0.0

    total_interest = 0.0
    remaining = borrow_amount
    prev_cap = 0.0

    for cap, rate in IB_TIERS_USD:
        tier_size = min(remaining, cap - prev_cap)
        if tier_size <= 0:
            break
        total_interest += tier_size * rate
        remaining -= tier_size
        prev_cap = cap
        if remaining <= 0:
            break

    return total_interest / borrow_amount


def apply_leverage(
    ret: pd.Series,
    leverage: float = 1.3,
    initial_equity: float = 1_000_000,
    annual_rate: float | None = None,
) -> pd.Series:
    """
    套用固定槓桿並扣除 IB 融資利息，回傳調整後每日報酬率。

    Parameters
    ----------
    ret            : pd.Series，未槓桿每日報酬率
    leverage       : float，槓桿倍數（1.0 = 無槓桿，1.3 = 30% 借款）
    initial_equity : float，初始本金（用於計算混合利率）
    annual_rate    : float or None
        指定年化融資利率；None = 依 IB 分級自動計算

    Returns
    -------
    pd.Series，套用槓桿後淨報酬率
    """
    if leverage == 1.0:
        return ret.copy()

    borrow_frac = leverage - 1.0           # 借款佔本金比例
    borrow_amt  = borrow_frac * initial_equity

    if annual_rate is None:
        annual_rate = ib_blended_rate(borrow_amt)

    daily_cost = borrow_frac * annual_rate / 252

    leveraged = leverage * ret - daily_cost
    leveraged.name = f"{ret.name} {leverage:.1f}x"
    return leveraged


def leverage_summary(leverage: float, initial_equity: float = 1_000_000) -> None:
    """印出槓桿參數摘要。"""
    borrow_frac = leverage - 1.0
    borrow_amt  = borrow_frac * initial_equity
    rate        = ib_blended_rate(borrow_amt)
    daily_drag  = borrow_frac * rate / 252

    print(f"槓桿倍數     : {leverage:.1f}x")
    print(f"借款比例     : {borrow_frac:.0%}  (${borrow_amt:,.0f} on ${initial_equity:,.0f})")
    print(f"IB 混合利率  : {rate:.3%} / yr")
    print(f"每日資金成本 : {daily_drag:.5%}  ({daily_drag*252:.3%} / yr)")
