"""
test_factor_drift.py
測量 SP500 動能因子 (adj_slope) 分佈隨時間的 KL 散度漂移。

每週（週三）對所有 SP500 成分股計算 adj_slope（動能強度×品質）的橫截面分佈，
再與第一年（52週）的參考分佈比較，量化因子分佈的漂移程度。
"""

import os
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
import matplotlib
matplotlib.use("Agg")  # 無頭模式，不需要 display
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ── 路徑設定 ──────────────────────────────────────────────────────────────────
# 將同一資料夾加入 sys.path，以便 import kl_divergence
_SCRIPT_DIR = Path(__file__).parent.resolve()
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from kl_divergence import compute_kl  # noqa: E402  僅引入 compute_kl

# ── 資料路徑 ──────────────────────────────────────────────────────────────────
_PROJECT_ROOT   = Path(__file__).parent.parent.resolve()
_STOCKS_DIR     = _PROJECT_ROOT / "data" / "stocks"
_CONSTITUENTS   = _PROJECT_ROOT / "data" / "reference" / "sp500_constituents_daily.xlsx"
_SPY_CSV        = _PROJECT_ROOT / "data" / "etfs" / "SPY.csv"
_OUTPUT_DIR     = _SCRIPT_DIR / "output"

# ── adj_slope 分箱邊界（9 個區間）─────────────────────────────────────────────
# 從大幅負動能到大幅正動能
_FACTOR_BINS = [-np.inf, -0.1, 0.0, 0.1, 0.3, 0.5, 0.8, 1.0, 1.5, np.inf]


# ─────────────────────────────────────────────────────────────────────────────
# 工具函式
# ─────────────────────────────────────────────────────────────────────────────

def calc_adj_slope(prices: np.ndarray, lookback: int = 90):
    """
    計算動能強度（adj_slope）。

    adj_slope = ((1 + slope)^lookback - 1) * r²
    其中 slope 為對數價格對時間的線性迴歸斜率，r 為相關係數（品質過濾）。

    參數
    ----
    prices : np.ndarray
        Adj Close（或 Close）價格序列。
    lookback : int
        計算窗口（交易日數）。預設 90。

    回傳
    ----
    float 或 None（資料不足時）
    """
    if len(prices) < lookback:
        return None
    chunk = prices[-lookback:]
    y_log = np.log(chunk)
    x = np.arange(len(y_log))
    slope, _, r, _, _ = stats.linregress(x, y_log)
    adj_slope = ((1 + slope) ** lookback - 1) * (r ** 2)
    return adj_slope


def factors_to_histogram(factor_values: np.ndarray, bins=None) -> np.ndarray:
    """
    將 adj_slope 陣列轉換為離散機率分佈（正規化直方圖）。

    使用 _FACTOR_BINS 作為預設分箱邊界（9 個區間）。

    參數
    ----
    factor_values : np.ndarray
        adj_slope 數值陣列。
    bins : list 或 None
        自訂分箱邊界；若 None 則使用 _FACTOR_BINS。

    回傳
    ----
    np.ndarray
        長度等於區間數的機率陣列，總和為 1。
    """
    if bins is None:
        bins = _FACTOR_BINS
    factor_values = np.asarray(factor_values, dtype=float)
    counts, _ = np.histogram(factor_values, bins=bins)
    total = counts.sum()
    if total == 0:
        n_bins = len(bins) - 1
        return np.ones(n_bins) / n_bins
    return (counts / total).astype(float)


# ─────────────────────────────────────────────────────────────────────────────
# 成分股載入
# ─────────────────────────────────────────────────────────────────────────────

def load_constituents(filepath: Path) -> pd.DataFrame:
    """
    載入所有年度的成分股 Excel（各年份一個 Sheet）。

    只讀取名稱為純數字的 Sheet（如 "2015", "2016" … "2026"）。
    每列代表一個交易日，欄位 T1, T2, … 為當日成分股代號。
    回傳以 Date 為 index 的 DataFrame，已按日期排序。
    """
    xl = pd.ExcelFile(filepath)
    # 只取名稱為數字的 sheet（排除 Summary、Counts 等）
    year_sheets = [s for s in xl.sheet_names if s.isdigit()]

    frames = []
    for sheet in year_sheets:
        df = pd.read_excel(xl, sheet_name=sheet)
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date")
        frames.append(df)

    constituents_df = pd.concat(frames).sort_index()
    return constituents_df


def get_constituents_for_date(constituents_df: pd.DataFrame, target_date: pd.Timestamp) -> list:
    """
    取得指定日期的成分股清單。

    使用 method='pad' 向前填充：若當日無資料，使用最近的過去日期。

    參數
    ----
    constituents_df : pd.DataFrame
        由 load_constituents() 回傳的 DataFrame。
    target_date : pd.Timestamp
        目標日期。

    回傳
    ----
    list of str
        成分股代號清單（移除 NaN）。
    """
    # 重新索引到目標日期，用 pad 填充
    idx = constituents_df.index
    # 取最近的過去或當日日期
    valid_dates = idx[idx <= target_date]
    if len(valid_dates) == 0:
        return []
    nearest_date = valid_dates[-1]
    row = constituents_df.loc[nearest_date]
    # 取所有非 NaN 的 ticker 欄位值
    tickers = [v for v in row.values if pd.notna(v) and isinstance(v, str) and v.strip() != ""]
    return tickers


# ─────────────────────────────────────────────────────────────────────────────
# 主要計算邏輯
# ─────────────────────────────────────────────────────────────────────────────

def run_factor_drift_analysis():
    """
    主要執行函式：
    1. 載入成分股與 SPY
    2. 逐週（週三）計算 adj_slope 橫截面分佈
    3. 對比第一年參考分佈，計算 KL 散度
    4. 輸出圖表與統計摘要
    """
    print("=" * 60)
    print("SP500 動能因子漂移分析（KL 散度）")
    print("=" * 60)

    # ── 建立輸出資料夾 ──────────────────────────────────────────────────────
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"輸出資料夾：{_OUTPUT_DIR}")

    # ── 載入成分股 ────────────────────────────────────────────────────────────
    print("\n[1/4] 載入成分股資料...")
    constituents_df = load_constituents(_CONSTITUENTS)
    print(f"      成分股資料範圍：{constituents_df.index.min().date()} ~ {constituents_df.index.max().date()}")

    # ── 載入 SPY（收盤價）────────────────────────────────────────────────────
    print("[2/4] 載入 SPY 資料...")
    spy_df = pd.read_csv(_SPY_CSV, parse_dates=["Date"], index_col="Date")
    spy_df = spy_df.sort_index()
    print(f"      SPY 資料範圍：{spy_df.index.min().date()} ~ {spy_df.index.max().date()}")

    # ── 產生所有週三日期（2019-01-01 至 2025-12-31）────────────────────────
    print("[3/4] 開始逐週計算 adj_slope 分佈...")
    all_wednesdays = pd.date_range(start="2019-01-01", end="2025-12-31", freq="W-WED")
    print(f"      共 {len(all_wednesdays)} 個週三")

    # 股票 CSV 快取（避免重複讀取 I/O）
    # key: ticker, value: pd.DataFrame（已排序、以 Date 為 index）
    stock_cache: dict = {}

    # 儲存每週計算結果
    results = []  # list of (date, kl_value, num_stocks)

    # 第一年 52 週的直方圖累積（用於計算參考分佈）
    ref_histograms = []
    P_ref = None  # 參考分佈（第一年平均直方圖）

    for week_idx, trade_date in enumerate(all_wednesdays):

        # 進度報告（每 50 週印一次）
        if week_idx % 50 == 0:
            print(f"      進度：第 {week_idx + 1}/{len(all_wednesdays)} 週 ({trade_date.date()})")

        # 取得當日成分股
        tickers = get_constituents_for_date(constituents_df, trade_date)
        if not tickers:
            continue  # 無成分股資料，跳過

        # 對每支成分股計算 adj_slope
        adj_slopes = []
        for ticker in tickers:
            # ── 從快取或讀檔取得股票資料 ──────────────────────────────────
            if ticker not in stock_cache:
                csv_path = _STOCKS_DIR / f"{ticker}.csv"
                if not csv_path.exists():
                    stock_cache[ticker] = None  # 標記為不存在
                    continue
                try:
                    df = pd.read_csv(csv_path, parse_dates=["Date"], index_col="Date")
                    df = df.sort_index()
                    stock_cache[ticker] = df
                except Exception:
                    stock_cache[ticker] = None
                    continue

            df = stock_cache[ticker]
            if df is None:
                continue

            # 取截至 trade_date 的資料（包含當日）
            hist = df.loc[:trade_date]
            if len(hist) < 90:
                continue  # 資料不足 90 天

            # 優先使用 Adj Close，否則用 Close
            if "Adj Close" in hist.columns:
                prices = hist["Adj Close"].values
            elif "Close" in hist.columns:
                prices = hist["Close"].values
            else:
                continue

            # 移除 NaN 與非正值
            prices = prices[np.isfinite(prices) & (prices > 0)]
            if len(prices) < 90:
                continue

            val = calc_adj_slope(prices, lookback=90)
            if val is not None and np.isfinite(val):
                adj_slopes.append(val)

        if len(adj_slopes) < 10:
            # 有效股票數量太少，跳過此週
            continue

        # 轉換為直方圖（機率分佈）
        adj_slopes_arr = np.array(adj_slopes)
        hist_current = factors_to_histogram(adj_slopes_arr)

        # ── 建立參考分佈（第一年 52 週平均）─────────────────────────────────
        if P_ref is None:
            ref_histograms.append(hist_current)
            if len(ref_histograms) >= 52:
                # 以第一年 52 週的平均直方圖作為參考分佈
                P_ref = np.mean(np.array(ref_histograms), axis=0)
                print(f"      參考分佈已建立（{len(ref_histograms)} 週平均，截至 {trade_date.date()}）")
            # 前 52 週不計算 KL 散度
            results.append((trade_date, np.nan, len(adj_slopes)))
            continue

        # ── 計算 KL 散度：D_KL(P_current || P_ref) ───────────────────────
        kl_val = compute_kl(hist_current, P_ref)
        results.append((trade_date, kl_val, len(adj_slopes)))

    print(f"\n      計算完成，共 {len(results)} 個資料點")

    # ── 整理結果為 DataFrame ─────────────────────────────────────────────────
    results_df = pd.DataFrame(results, columns=["date", "kl_divergence", "num_stocks"])
    results_df = results_df.set_index("date")
    results_df = results_df.dropna(subset=["kl_divergence"])

    if results_df.empty:
        print("警告：沒有有效的 KL 散度計算結果，請確認資料完整性。")
        return

    # ── 統計摘要 ─────────────────────────────────────────────────────────────
    print("\n[4/4] 統計摘要")
    print("-" * 40)
    kl_series = results_df["kl_divergence"]
    print(f"資料期間        : {results_df.index.min().date()} ~ {results_df.index.max().date()}")
    print(f"有效週數        : {len(kl_series)}")
    print(f"平均 KL 散度    : {kl_series.mean():.4f}")
    print(f"中位數 KL 散度  : {kl_series.median():.4f}")
    print(f"最大 KL 散度    : {kl_series.max():.4f}（{kl_series.idxmax().date()}）")
    print(f"最小 KL 散度    : {kl_series.min():.4f}（{kl_series.idxmin().date()}）")
    p95 = float(np.percentile(kl_series.values, 95))
    print(f"95 百分位閾值   : {p95:.4f}")
    exceed_count = (kl_series > p95).sum()
    print(f"超過 95% 閾值次數: {exceed_count}（{exceed_count / len(kl_series) * 100:.1f}%）")
    print(f"平均掃描股票數  : {results_df['num_stocks'].mean():.0f}")
    print("-" * 40)

    # ── 繪圖 ─────────────────────────────────────────────────────────────────
    fig, (ax1, ax2) = plt.subplots(
        2, 1,
        figsize=(14, 8),
        sharex=True,
        gridspec_kw={"height_ratios": [2, 1]},
    )
    fig.suptitle(
        "SP500 Momentum Factor (adj_slope) Distribution KL Divergence",
        fontsize=14,
        fontweight="bold",
    )

    # 上圖：KL 散度時間序列
    ax1.plot(
        kl_series.index,
        kl_series.values,
        color="#2196F3",
        linewidth=1.2,
        label="KL Divergence (D_KL current || reference)",
    )
    ax1.axhline(
        y=p95,
        color="#F44336",
        linewidth=1.5,
        linestyle="--",
        label=f"95th Percentile Threshold ({p95:.3f})",
    )
    # 標記超過閾值的區域（填色）
    ax1.fill_between(
        kl_series.index,
        kl_series.values,
        p95,
        where=(kl_series.values > p95),
        alpha=0.25,
        color="#F44336",
        label="Exceeds 95th Pct",
    )
    ax1.set_ylabel("KL Divergence (nats)", fontsize=11)
    ax1.legend(loc="upper left", fontsize=9)
    ax1.grid(True, alpha=0.3)
    ax1.set_ylim(bottom=0)

    # 下圖：SPY 收盤價（參考用）
    spy_plot = spy_df["Close"].loc["2019-01-01":"2025-12-31"]
    ax2.plot(
        spy_plot.index,
        spy_plot.values,
        color="#4CAF50",
        linewidth=1.2,
        label="SPY Close",
    )
    ax2.set_ylabel("SPY Close Price (USD)", fontsize=11)
    ax2.set_xlabel("Date", fontsize=11)
    ax2.legend(loc="upper left", fontsize=9)
    ax2.grid(True, alpha=0.3)

    # 日期格式化
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax2.xaxis.set_major_locator(mdates.YearLocator())
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=0, ha="center")

    plt.tight_layout()

    output_path = _OUTPUT_DIR / "factor_drift_kl.png"
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\n圖表已儲存至：{output_path}")

    # ── 同時儲存 CSV 結果（方便後續分析）────────────────────────────────────
    csv_path = _OUTPUT_DIR / "factor_drift_kl.csv"
    results_df.to_csv(csv_path)
    print(f"數值結果已儲存至：{csv_path}")


# ─────────────────────────────────────────────────────────────────────────────
# 入口點
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    run_factor_drift_analysis()
