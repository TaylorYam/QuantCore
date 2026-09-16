"""
test_return_drift.py
SPY 報酬分佈 KL 散度監測 — 偵測市場結構漂移。

用滾動窗口比較 SPY 當前報酬分佈與歷史參考分佈的 KL 散度，
當 KL 飆升時代表市場行為模式正在改變（可能領先大跌或波動放大）。
"""

import os
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")  # 無頭模式
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.gridspec import GridSpec

# ── 路徑設定 ──────────────────────────────────────────────────────────────────
_SCRIPT_DIR = Path(__file__).parent.resolve()
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from kl_divergence import (  # noqa: E402
    compute_kl,
    returns_to_histogram,
    rolling_kl,
    kl_percentile_threshold,
)

# ── 資料路徑 ──────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
_SPY_CSV      = _PROJECT_ROOT / "data" / "etfs" / "SPY.csv"
_OUTPUT_DIR   = _SCRIPT_DIR / "output"


# ─────────────────────────────────────────────────────────────────────────────
# 工具函式
# ─────────────────────────────────────────────────────────────────────────────

def find_spike_periods(kl_series: pd.Series, threshold: float, max_gap_days: int = 15):
    """
    找出 KL 散度超過門檻的連續飆升期間。

    由於 rolling_kl 是每 step 天計算一次，兩個連續高點之間可能間隔數天。
    若兩個高點之間的日曆天數 <= max_gap_days，視為同一飆升期。

    參數
    ----
    kl_series : pd.Series
        KL 散度時間序列（DatetimeIndex）。
    threshold : float
        飆升門檻值。
    max_gap_days : int
        判定為同一期間的最大日曆天數間隔。

    回傳
    ----
    list of dict
        每個 dict 包含 'start', 'end', 'max_kl', 'duration_days'。
    """
    above = kl_series[kl_series > threshold]
    if len(above) == 0:
        return []

    dates = above.index.sort_values()
    periods = []
    current_start = dates[0]
    current_end = dates[0]
    current_max = above.loc[dates[0]]

    for i in range(1, len(dates)):
        gap = (dates[i] - current_end).days
        if gap <= max_gap_days:
            # 延續同一期間
            current_end = dates[i]
            current_max = max(current_max, above.loc[dates[i]])
        else:
            # 結束當前期間，開始新的
            periods.append({
                "start": current_start,
                "end": current_end,
                "max_kl": current_max,
                "duration_days": (current_end - current_start).days + 1,
            })
            current_start = dates[i]
            current_end = dates[i]
            current_max = above.loc[dates[i]]

    # 最後一個期間
    periods.append({
        "start": current_start,
        "end": current_end,
        "max_kl": current_max,
        "duration_days": (current_end - current_start).days + 1,
    })

    return periods


# ─────────────────────────────────────────────────────────────────────────────
# 主要執行邏輯
# ─────────────────────────────────────────────────────────────────────────────

def run_return_drift_analysis():
    """
    主要執行函式：
    1. 載入 SPY 日線資料並計算對數報酬
    2. 用滾動窗口計算報酬分佈 KL 散度
    3. 找出飆升期間並輸出分析
    4. 繪製雙軸圖表
    """
    print("=" * 60)
    print("SPY 報酬分佈 KL 散度監測")
    print("=" * 60)

    # ── 建立輸出資料夾 ──────────────────────────────────────────────────────
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── 載入 SPY 資料 ────────────────────────────────────────────────────────
    print("\n[1/4] 載入 SPY 資料...")
    spy_df = pd.read_csv(_SPY_CSV, parse_dates=["Date"], index_col="Date")
    spy_df = spy_df.sort_index()
    print(f"      SPY 資料範圍：{spy_df.index.min().date()} ~ {spy_df.index.max().date()}")

    # ── 計算對數報酬 ─────────────────────────────────────────────────────────
    spy_close = spy_df["Close"]
    log_returns = np.log(spy_close / spy_close.shift(1)).dropna()
    print(f"      對數報酬序列長度：{len(log_returns)} 天")

    # ── 計算滾動 KL 散度 ─────────────────────────────────────────────────────
    print("\n[2/4] 計算滾動 KL 散度...")
    print("      參考窗口：252 天（1 年）")
    print("      當前窗口：60 天（1 季）")
    print("      計算步長：5 天（每週）")

    kl_series = rolling_kl(
        log_returns,
        ref_window=252,
        current_window=60,
        step=5,
    )
    print(f"      KL 散度資料點數：{len(kl_series)}")

    if kl_series.empty:
        print("錯誤：資料不足，無法計算 KL 散度。")
        return

    # ── 計算門檻值 ───────────────────────────────────────────────────────────
    threshold = kl_percentile_threshold(kl_series, percentile=95)

    # ── 統計摘要 ─────────────────────────────────────────────────────────────
    print("\n[3/4] 統計摘要")
    print("-" * 50)
    print(f"分析期間        : {kl_series.index.min().date()} ~ {kl_series.index.max().date()}")
    print(f"資料點數        : {len(kl_series)}")
    print(f"平均 KL 散度    : {kl_series.mean():.4f}")
    print(f"中位數 KL 散度  : {kl_series.median():.4f}")
    print(f"最大 KL 散度    : {kl_series.max():.4f}（{kl_series.idxmax().date()}）")
    print(f"最小 KL 散度    : {kl_series.min():.4f}（{kl_series.idxmin().date()}）")
    print(f"95 百分位閾值   : {threshold:.4f}")

    # ── 找出飆升期間 ─────────────────────────────────────────────────────────
    spike_periods = find_spike_periods(kl_series, threshold)
    print(f"\n飆升期間數      : {len(spike_periods)}")
    print("-" * 50)

    if spike_periods:
        print("\n各飆升期間明細：")
        for i, p in enumerate(spike_periods, 1):
            print(f"  [{i}] {p['start'].date()} ~ {p['end'].date()}  "
                  f"（{p['duration_days']} 天，最大 KL = {p['max_kl']:.4f}）")

    # Top 5 最高 KL 散度日期
    top5 = kl_series.nlargest(5)
    print(f"\nTop 5 最高 KL 散度日期：")
    for date, val in top5.items():
        print(f"  {date.date()}  KL = {val:.4f}")
    print("-" * 50)

    # ── 繪圖 ─────────────────────────────────────────────────────────────────
    print("\n[4/4] 繪製圖表...")

    fig = plt.figure(figsize=(14, 10))
    gs = GridSpec(2, 1, height_ratios=[7, 3], hspace=0.08)
    ax_price = fig.add_subplot(gs[0])
    ax_kl = fig.add_subplot(gs[1], sharex=ax_price)

    fig.suptitle(
        "SPY Return Distribution KL Divergence — Market Structure Monitor",
        fontsize=14,
        fontweight="bold",
        y=0.98,
    )

    # ── 上圖：SPY 收盤價 + 飆升期間標記 ──────────────────────────────────────
    # 裁切到 KL 散度有資料的期間
    plot_start = kl_series.index.min()
    plot_end = kl_series.index.max()
    spy_plot = spy_close.loc[plot_start:plot_end]

    ax_price.plot(
        spy_plot.index, spy_plot.values,
        color="#1565C0", linewidth=1.2, label="SPY Close",
    )

    # 飆升期間紅色陰影
    for p in spike_periods:
        ax_price.axvspan(
            p["start"], p["end"],
            alpha=0.15, color="#F44336", zorder=0,
        )

    ax_price.set_ylabel("SPY Close Price (USD)", fontsize=11)
    ax_price.legend(loc="upper left", fontsize=9)
    ax_price.grid(True, alpha=0.3)
    plt.setp(ax_price.get_xticklabels(), visible=False)

    # ── 下圖：KL 散度 + 門檻線 ───────────────────────────────────────────────
    ax_kl.plot(
        kl_series.index, kl_series.values,
        color="#2196F3", linewidth=1.0, label="KL Divergence",
    )
    ax_kl.axhline(
        y=threshold,
        color="#F44336", linewidth=1.5, linestyle="--",
        label=f"95th Percentile ({threshold:.3f})",
    )
    ax_kl.fill_between(
        kl_series.index, kl_series.values, threshold,
        where=(kl_series.values > threshold),
        alpha=0.3, color="#F44336",
        label="Above Threshold",
    )

    ax_kl.set_ylabel("KL Divergence (nats)", fontsize=11)
    ax_kl.set_xlabel("Date", fontsize=11)
    ax_kl.legend(loc="upper left", fontsize=9)
    ax_kl.grid(True, alpha=0.3)
    ax_kl.set_ylim(bottom=0)

    # 日期格式
    ax_kl.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax_kl.xaxis.set_major_locator(mdates.YearLocator())

    output_path = _OUTPUT_DIR / "return_drift_kl.png"
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\n圖表已儲存至：{output_path}")

    # ── 儲存 CSV ─────────────────────────────────────────────────────────────
    csv_path = _OUTPUT_DIR / "return_drift_kl.csv"
    kl_series.to_csv(csv_path, header=True)
    print(f"數值結果已儲存至：{csv_path}")


# ─────────────────────────────────────────────────────────────────────────────
# 入口點
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    run_return_drift_analysis()
