"""
QuantCore — 績效指標計算
統一入口，所有策略與組合共用。
"""

import numpy as np
import pandas as pd


def compute(ret: pd.Series, label: str = "", ann: int = 252) -> dict:
    """
    從日報酬率序列計算常用績效指標。

    Parameters
    ----------
    ret   : pd.Series  日報酬率
    label : str        策略名稱（只用於顯示）
    ann   : int        年化係數（交易日數，預設 252）

    Returns
    -------
    dict，包含所有指標
    """
    ret = ret.dropna()
    if len(ret) == 0:
        return {}

    n       = len(ret)
    cum     = (1 + ret).cumprod()
    total   = cum.iloc[-1] - 1
    ann_ret = (1 + ret).prod() ** (ann / n) - 1
    ann_vol = ret.std() * np.sqrt(ann)
    sharpe  = ann_ret / ann_vol if ann_vol > 0 else np.nan

    # 回撤
    dd      = cum / cum.cummax() - 1
    max_dd  = dd.min()
    calmar  = ann_ret / abs(max_dd) if max_dd != 0 else np.nan

    # Sortino
    down    = ret[ret < 0]
    down_vol = down.std() * np.sqrt(ann) if len(down) > 0 else np.nan
    sortino = ann_ret / down_vol if (down_vol and down_vol > 0) else np.nan

    # 月勝率
    monthly     = (1 + ret).resample("ME").prod() - 1
    win_rate_m  = (monthly > 0).mean()
    best_month  = monthly.max()
    worst_month = monthly.min()

    return {
        "label":       label,
        "start":       ret.index[0].date(),
        "end":         ret.index[-1].date(),
        "days":        n,
        "total_ret":   total,
        "ann_ret":     ann_ret,
        "ann_vol":     ann_vol,
        "sharpe":      sharpe,
        "sortino":     sortino,
        "calmar":      calmar,
        "max_dd":      max_dd,
        "win_rate_m":  win_rate_m,
        "best_month":  best_month,
        "worst_month": worst_month,
        "final_1m":    cum.iloc[-1] * 1_000_000,
    }


def annual_returns(ret: pd.Series) -> pd.Series:
    """回傳逐年報酬率 Series（index = 年度整數）。"""
    ann = (1 + ret).resample("YE").prod() - 1
    ann.index = ann.index.year
    return ann


def print_table(metrics_list: list[dict]):
    """在終端印出多策略比較表。"""
    if not metrics_list:
        return

    labels = [m["label"] for m in metrics_list]
    W = 18

    def row(name, key, fmt):
        vals = [fmt(m.get(key, float("nan"))) for m in metrics_list]
        print(f"  {name:<22}" + "".join(f"{v:>{W}}" for v in vals))

    header = f"  {'指標':<22}" + "".join(f"{l:>{W}}" for l in labels)
    sep    = "─" * len(header)

    print(sep)
    print(header)
    print(sep)
    row("總報酬",        "total_ret",   lambda x: f"{x:+.1%}")
    row("年化報酬",      "ann_ret",     lambda x: f"{x:+.1%}")
    row("年化波動",      "ann_vol",     lambda x: f"{x:.1%}")
    row("Sharpe",        "sharpe",      lambda x: f"{x:.2f}")
    row("Sortino",       "sortino",     lambda x: f"{x:.2f}")
    row("Calmar",        "calmar",      lambda x: f"{x:.2f}")
    row("最大回撤",      "max_dd",      lambda x: f"{x:.1%}")
    row("月勝率",        "win_rate_m",  lambda x: f"{x:.1%}")
    row("最佳月",        "best_month",  lambda x: f"{x:+.1%}")
    row("最差月",        "worst_month", lambda x: f"{x:+.1%}")
    row("最終 $1M",     "final_1m",    lambda x: f"${x:,.0f}")
    print(sep)
