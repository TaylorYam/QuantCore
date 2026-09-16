"""
測試 1：加速度信號品質分析
─────────────────────────
目的：檢驗加速度信號是否有預測力，還是純噪音。

測試項目：
1. adj_slope 5 天變化量的分布（是否有足夠的差異性）
2. 加速度信號 vs 未來 N 天報酬的相關性（是否有預測力）
3. adj_slope 接近零時，加速度比率的穩定性
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import numpy as np
import pandas as pd
from scipy import stats as sp_stats
from strategies.sp500_momentum.selection import SelectionEngine
from strategies.sp500_momentum import config

# ── 工具函式 ─────────────────────────────────────────────────────

def calc_acceleration(slope_now: float, slope_prev: float,
                      weight: float = 1.0) -> dict:
    """計算加速度修正，回傳中間值供分析。"""
    delta = slope_now - slope_prev
    denom = abs(slope_now) if abs(slope_now) > 1e-8 else 1e-8
    raw_ratio = delta / denom
    clipped = np.clip(raw_ratio, -1.0, 1.0)
    adjusted = slope_now * (1 + weight * clipped)
    return {
        "slope_now": slope_now,
        "slope_prev": slope_prev,
        "delta": delta,
        "raw_ratio": raw_ratio,
        "clipped_ratio": clipped,
        "adjusted_slope": adjusted,
    }


def get_trading_days(selector: SelectionEngine) -> pd.DatetimeIndex:
    """取得交易日序列。"""
    ref_path = config.REF_DIR / config.TRADING_DAYS_FILE
    df = pd.read_excel(ref_path)
    dates = pd.to_datetime(df["date"])
    return pd.DatetimeIndex(sorted(dates))


def run_signal_quality_test():
    print("=" * 70)
    print("測試 1：加速度信號品質分析")
    print("=" * 70)

    selector = SelectionEngine()
    selector.preload_all_data(config.START_DATE, config.END_DATE)
    trading_days = get_trading_days(selector)

    # 選取測試期間：每月第一個交易日，避免計算量太大
    start = pd.Timestamp("2020-01-01")
    end = pd.Timestamp("2025-12-31")
    mask = (trading_days >= start) & (trading_days <= end)
    sample_days = trading_days[mask]

    # 每月取一天
    monthly = sample_days.to_series().groupby(
        sample_days.to_period("M")
    ).first().values
    monthly = pd.DatetimeIndex(monthly)

    print(f"測試日期數量: {len(monthly)}")

    accel_gap_days = [5, 10, 20]  # 測試不同加速度窗口
    fwd_return_days = [5, 10, 20]  # 測試不同前瞻報酬窗口

    results_by_gap = {}

    for gap in accel_gap_days:
        all_records = []

        for date in monthly:
            # 找到 gap 天前的交易日
            idx = trading_days.get_indexer([date], method="pad")[0]
            if idx < gap:
                continue
            prev_date = trading_days[idx - gap]

            ranked_now = selector.scan_market(date, config.LOOKBACK)
            ranked_prev = selector.scan_market(prev_date, config.LOOKBACK)

            prev_map = {r["ticker"]: r["adj_slope"] for r in ranked_prev}

            for r in ranked_now:
                ticker = r["ticker"]
                slope_now = r["adj_slope"]

                if ticker not in prev_map:
                    continue
                slope_prev = prev_map[ticker]

                if slope_now <= -900:  # 跳過計算失敗的
                    continue

                acc = calc_acceleration(slope_now, slope_prev)

                # 計算前瞻報酬
                fwd_returns = {}
                df = selector._get_ticker_data(ticker)
                if df is not None and not df.empty:
                    future = df[df.index > date]
                    for fwd in fwd_return_days:
                        if len(future) >= fwd:
                            price_now = r["price"]
                            price_fwd = float(future["Close"].iloc[fwd - 1])
                            fwd_returns[f"fwd_{fwd}d"] = (
                                (price_fwd - price_now) / price_now
                            )

                record = {
                    "date": date,
                    "ticker": ticker,
                    **acc,
                    **fwd_returns,
                }
                all_records.append(record)

        df_all = pd.DataFrame(all_records)
        results_by_gap[gap] = df_all

        print(f"\n{'─' * 50}")
        print(f"加速度窗口 = {gap} 天")
        print(f"{'─' * 50}")
        print(f"樣本數: {len(df_all)}")

        if df_all.empty:
            print("  無資料，跳過")
            continue

        # 1. Delta 分布
        print(f"\n  adj_slope 變化量 (delta) 統計:")
        print(f"    mean   = {df_all['delta'].mean():.4f}")
        print(f"    std    = {df_all['delta'].std():.4f}")
        print(f"    median = {df_all['delta'].median():.4f}")
        print(f"    |delta| < 0.01 的比例 = "
              f"{(df_all['delta'].abs() < 0.01).mean():.1%}")

        # 2. 加速度比率分布
        print(f"\n  加速度比率 (clipped) 統計:")
        print(f"    mean   = {df_all['clipped_ratio'].mean():.4f}")
        print(f"    std    = {df_all['clipped_ratio'].std():.4f}")
        print(f"    觸碰邊界 (|ratio|=1) 的比例 = "
              f"{(df_all['clipped_ratio'].abs() >= 0.99).mean():.1%}")

        # 3. adj_slope 接近零的穩定性問題
        near_zero = df_all[df_all["slope_now"].abs() < 0.05]
        not_near_zero = df_all[df_all["slope_now"].abs() >= 0.05]
        print(f"\n  adj_slope 接近零 (|slope| < 0.05) 的樣本: "
              f"{len(near_zero)} ({len(near_zero)/len(df_all):.1%})")
        if len(near_zero) > 10:
            print(f"    這些樣本的 raw_ratio std = "
                  f"{near_zero['raw_ratio'].std():.2f}")
            print(f"    其他樣本的 raw_ratio std = "
                  f"{not_near_zero['raw_ratio'].std():.2f}")
            print(f"    → 接近零時波動是否更大: "
                  f"{'是 ⚠' if near_zero['raw_ratio'].std() > not_near_zero['raw_ratio'].std() * 2 else '否'}")

        # 4. 加速度 vs 前瞻報酬的相關性
        print(f"\n  加速度 vs 前瞻報酬 (Spearman rank correlation):")
        for fwd in fwd_return_days:
            col = f"fwd_{fwd}d"
            if col not in df_all.columns:
                continue
            valid = df_all[[col, "clipped_ratio"]].dropna()
            if len(valid) < 30:
                continue
            corr, pval = sp_stats.spearmanr(valid["clipped_ratio"], valid[col])
            sig = "***" if pval < 0.01 else "**" if pval < 0.05 else "*" if pval < 0.1 else ""
            print(f"    accel vs fwd_{fwd}d: r={corr:+.4f}  p={pval:.4f} {sig}")

        # 5. 分組測試：加速 vs 減速的前瞻報酬
        print(f"\n  分組比較（加速 vs 減速 的平均前瞻報酬）:")
        accel_group = df_all[df_all["clipped_ratio"] > 0.1]
        decel_group = df_all[df_all["clipped_ratio"] < -0.1]
        for fwd in fwd_return_days:
            col = f"fwd_{fwd}d"
            if col not in df_all.columns:
                continue
            a_ret = accel_group[col].mean()
            d_ret = decel_group[col].mean()
            diff = a_ret - d_ret if not (np.isnan(a_ret) or np.isnan(d_ret)) else float("nan")
            print(f"    fwd_{fwd}d: 加速={a_ret:+.4f}  減速={d_ret:+.4f}  差異={diff:+.4f}")

    # 輸出 CSV 供後續分析
    output_dir = os.path.join(os.path.dirname(__file__), "output")
    os.makedirs(output_dir, exist_ok=True)
    for gap, df in results_by_gap.items():
        if not df.empty:
            path = os.path.join(output_dir, f"signal_quality_gap{gap}.csv")
            df.to_csv(path, index=False)
            print(f"\n已輸出: {path}")


if __name__ == "__main__":
    run_signal_quality_test()
