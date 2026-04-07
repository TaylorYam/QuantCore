"""
QuantCore — 組合回測
將兩個策略的每日報酬率合併成三種配比：
  100/0  → 全 Sector Rotation MOM
  0/100  → 全 SP500 Momentum
  50/50  → 月初重設 50/50，月內自由漂移
"""

import pandas as pd
import numpy as np


# ── 單一策略包裝 ──────────────────────────────────────────────────────────────

def run_sr(start_date: str = None, end_date: str = None) -> pd.Series:
    """
    執行 Sector Rotation MOM，回傳每日報酬率 Series。
    使用 run_full_backtest()，它直接回傳 net_ret（日報酬率）。
    """
    from strategies.sector_rotation.data_loader import load_data
    from strategies.sector_rotation.backtest    import run_full_backtest

    data = load_data()

    # 日期截取（在資料層裁切，避免 signal 計算用到範圍外的資料）
    if start_date or end_date:
        sd = pd.to_datetime(start_date) if start_date else None
        ed = pd.to_datetime(end_date)   if end_date   else None
        for key in ("etf_close", "etf_open"):
            df = data[key]
            if sd: df = df[df.index >= sd]
            if ed: df = df[df.index <= ed]
            data[key] = df
        for key in ("spy_close", "spy_open"):
            s = data[key]
            if sd: s = s[s.index >= sd]
            if ed: s = s[s.index <= ed]
            data[key] = s

    net_ret, _, _ = run_full_backtest(data)
    net_ret.name = "SR"
    return net_ret


def run_sp500(start_date: str = None, end_date: str = None) -> pd.Series:
    """
    執行 SP500 Momentum，回傳每日報酬率 Series。
    run() 回傳 SP500Backtester，再呼叫 .get_equity_series()。
    """
    from strategies.sp500_momentum import run
    bt     = run(start_date=start_date, end_date=end_date, write_reports=False)
    equity = bt.get_equity_series()
    ret    = equity.pct_change().dropna()
    ret.name = "SP500"
    return ret


def run_benchmark(start_date: str = None, end_date: str = None) -> pd.Series:
    """載入 SPY 作為基準，回傳每日報酬率 Series。"""
    from strategies.sector_rotation.data_loader import load_data
    data = load_data()
    spy = data["spy_close"]
    if start_date:
        spy = spy[spy.index >= pd.to_datetime(start_date)]
    if end_date:
        spy = spy[spy.index <= pd.to_datetime(end_date)]
    ret = spy.pct_change().dropna()
    ret.name = "SPY B&H"
    return ret


# ── 組合邏輯 ──────────────────────────────────────────────────────────────────

def combine_100_0(ret_sr: pd.Series) -> pd.Series:
    """100% SR MOM。"""
    s = ret_sr.copy()
    s.name = "100/0 SR"
    return s


def combine_0_100(ret_sp: pd.Series) -> pd.Series:
    """100% SP500 MOM。"""
    s = ret_sp.copy()
    s.name = "0/100 SP500"
    return s


def combine_monthly(
    ret_sr: pd.Series,
    ret_sp: pd.Series,
    alloc_sr: float = 0.5,
    alloc_sp: float = 0.5,
    ib_annual_rate: float | None = None,
    initial_equity: float = 1_000_000,
) -> pd.Series:
    """
    月初重設至目標比例，月內自由漂移。支援槓桿（alloc_sr + alloc_sp > 1）。

    Parameters
    ----------
    alloc_sr / alloc_sp : 月初目標比例（相對於本金）
        50/50 → alloc_sr=0.5, alloc_sp=0.5
        80/50 → alloc_sr=0.8, alloc_sp=0.5（總 1.3x，借款 0.3）
    ib_annual_rate : 年化融資利率；None = 依 IB 分級自動計算
    initial_equity : 初始本金（計算 IB 分級利率用）
    """
    from backtest.leverage import ib_blended_rate

    borrow_frac = max(0.0, alloc_sr + alloc_sp - 1.0)
    if ib_annual_rate is None and borrow_frac > 0:
        ib_annual_rate = ib_blended_rate(borrow_frac * initial_equity)
    daily_financing = borrow_frac * (ib_annual_rate or 0.0) / 252

    df = pd.DataFrame({"sr": ret_sr, "sp": ret_sp}).dropna()
    month_start_set = set(
        df.index.to_series().groupby(df.index.to_period("M")).first().values
    )

    results = []
    w_sr, w_sp = alloc_sr, alloc_sp   # 月初重設

    for date, row in df.iterrows():
        if date in month_start_set:
            w_sr, w_sp = alloc_sr, alloc_sp   # 月初回設

        daily_ret = w_sr * row["sr"] + w_sp * row["sp"] - daily_financing

        # 月內漂移：各自 NAV 變動
        nav_sr = w_sr * (1 + row["sr"])
        nav_sp = w_sp * (1 + row["sp"])
        # 總 NAV 相對本金的比例維持（不強制再平衡）
        w_sr   = nav_sr
        w_sp   = nav_sp

        results.append(daily_ret)

    # 組合名稱
    if alloc_sr == 0.5 and alloc_sp == 0.5:
        label = "50/50 Monthly"
    else:
        label = f"{alloc_sr:.0%}/{alloc_sp:.0%} Monthly"
        if borrow_frac > 0:
            label += f" {1+borrow_frac:.1f}x"

    return pd.Series(results, index=df.index, name=label)


# 向後相容別名
def combine_50_50(ret_sr: pd.Series, ret_sp: pd.Series,
                  initial_equity: float = 1_000_000) -> pd.Series:
    return combine_monthly(ret_sr, ret_sp, 0.5, 0.5,
                           initial_equity=initial_equity)


# ── 主入口 ────────────────────────────────────────────────────────────────────

def build_all(
    start_date: str = None,
    end_date: str   = None,
    preloaded_sr:   pd.Series = None,
    preloaded_sp:   pd.Series = None,
    extra_allocs:   list[tuple[float, float]] | None = None,
    initial_equity: float = 1_000_000,
) -> dict[str, pd.Series]:
    """
    執行兩個策略 + 組合 + SPY B&H。

    Parameters
    ----------
    extra_allocs : list of (alloc_sr, alloc_sp)
        額外要比較的組合比例，例如 [(0.8, 0.5)] 代表 80% SR + 50% SP500。
        預設 None → 只跑 100/0、0/100、50/50。
    initial_equity : 用於 IB 槓桿利率計算
    """
    print("=" * 60)
    print("Step 1/4  SR MOM...")
    ret_sr = preloaded_sr if preloaded_sr is not None else run_sr(start_date, end_date)

    print("Step 2/4  SP500 MOM...")
    ret_sp = preloaded_sp if preloaded_sp is not None else run_sp500(start_date, end_date)

    print("Step 3/4  SPY benchmark...")
    ret_spy = run_benchmark(start_date, end_date)

    print("Step 4/4  Combining...")

    common = ret_sr.index.intersection(ret_sp.index)
    ret_sr = ret_sr.loc[common]
    ret_sp = ret_sp.loc[common]

    c5050 = combine_50_50(ret_sr, ret_sp, initial_equity=initial_equity)
    combos = {
        combine_100_0(ret_sr).name:  combine_100_0(ret_sr),
        combine_0_100(ret_sp).name:  combine_0_100(ret_sp),
        c5050.name:                  c5050,
    }

    for a_sr, a_sp in (extra_allocs or []):
        s = combine_monthly(ret_sr, ret_sp, a_sr, a_sp,
                            initial_equity=initial_equity)
        combos[s.name] = s

    combos[ret_spy.name] = ret_spy

    print("Done.\n")
    return combos


def to_equity_curves(combos: dict[str, pd.Series]) -> pd.DataFrame:
    """
    將報酬率字典轉成權益曲線 DataFrame（初始值 1.0）。
    各序列取各自日期範圍（不強制對齊，方便 chart 用 dropna 各畫各的）。
    """
    # 去除每個序列的重複 index（保留最後一筆）
    clean = {k: v[~v.index.duplicated(keep="last")].sort_index()
             for k, v in combos.items()}

    df = pd.DataFrame(clean)
    equity = (1 + df).cumprod()
    # 第一行補 1.0（起始點）
    first_row = pd.DataFrame(
        {col: [1.0] for col in equity.columns},
        index=[equity.index[0] - pd.tseries.offsets.BDay(1)]
    )
    equity = pd.concat([first_row, equity]).sort_index()
    return equity
