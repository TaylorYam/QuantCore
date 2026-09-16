"""
test_kl_backtest.py
KL 散度增強回測 — 對比原始 STOCK 策略與 KL 預警版本。

做法：
  - 原始版：直接跑 SP500Backtester（不修改任何原始碼）
  - KL 版：subclass SP500Backtester，在再平衡時檢查 KL 散度
    - KL > 90th pct 門檻 → TARGET_HOLDINGS 降為 2（減倉）
    - KL 回落 → 恢復 TARGET_HOLDINGS = 4

用 quantstats 產出完整比較報告。
"""

import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

# ── 讓 Python 能 import 原始策略模組 ─────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_SCRIPT_DIR = Path(__file__).parent.resolve()
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

# 原始策略模組（只讀 import，不修改）
from strategies.sp500_momentum import config
from strategies.sp500_momentum.selection import SelectionEngine
from strategies.sp500_momentum.backtester import SP500Backtester
from strategies.sp500_momentum import utils

# KL 散度計算
from kl_divergence import returns_to_histogram, compute_kl

_OUTPUT_DIR = _SCRIPT_DIR / "output"

# ── KL 參數（來自 test_kl_vs_ma200.py 測試結果）──────────────────────────────
KL_REF_WINDOW     = 252   # 參考窗口（1 年）
KL_CURRENT_WINDOW = 20    # 當前窗口（20 天）
KL_PERCENTILE     = 90    # 門檻百分位
KL_REDUCED_HOLDINGS = 2   # KL 超標時的目標持股數

# 報酬離散化 bins
_RETURN_BINS = [-np.inf, -0.03, -0.01, -0.003, 0.003, 0.01, 0.03, np.inf]


# ─────────────────────────────────────────────────────────────────────────────
# KL 散度預計算
# ─────────────────────────────────────────────────────────────────────────────

def precompute_kl_series(spy_close: pd.Series) -> tuple[pd.Series, float]:
    """
    預先計算整段 SPY 的滾動 KL 散度，回傳 (kl_series, threshold)。
    step=1 以便在任何日期都能查詢。
    """
    log_ret = np.log(spy_close / spy_close.shift(1)).dropna()
    values = log_ret.values
    dates = log_ret.index.values
    n = len(values)
    min_req = KL_REF_WINDOW + KL_CURRENT_WINDOW

    kl_dates, kl_values = [], []
    for t in range(min_req, n + 1):
        ref_slice = values[t - KL_REF_WINDOW - KL_CURRENT_WINDOW: t - KL_CURRENT_WINDOW]
        cur_slice = values[t - KL_CURRENT_WINDOW: t]

        p_ref = np.histogram(ref_slice, bins=_RETURN_BINS)[0].astype(float)
        p_cur = np.histogram(cur_slice, bins=_RETURN_BINS)[0].astype(float)

        eps = 1e-8
        p_ref = (p_ref + eps); p_ref /= p_ref.sum()
        p_cur = (p_cur + eps); p_cur /= p_cur.sum()

        kl = float(np.sum(p_cur * np.log(p_cur / p_ref)))
        kl_dates.append(dates[min(t - 1, n - 1)])
        kl_values.append(kl)

    kl_series = pd.Series(kl_values, index=pd.DatetimeIndex(kl_dates), name="kl")
    threshold = float(np.percentile(kl_values, KL_PERCENTILE))
    return kl_series, threshold


# ─────────────────────────────────────────────────────────────────────────────
# KL 增強版 Backtester（繼承原始類別，覆寫再平衡邏輯）
# ─────────────────────────────────────────────────────────────────────────────

class KLEnhancedBacktester(SP500Backtester):
    """
    繼承 SP500Backtester，在再平衡時加入 KL 散度檢查。
    KL 超標 → 縮減 TARGET_HOLDINGS；KL 正常 → 恢復原設定。
    """

    def __init__(self, kl_series: pd.Series, kl_threshold: float, **kwargs):
        self.kl_series = kl_series
        self.kl_threshold = kl_threshold
        self.kl_state_log = []  # 記錄 KL 狀態轉換
        self._original_target = config.TARGET_HOLDINGS
        self._kl_active = False  # 目前是否在減倉狀態
        super().__init__(**kwargs)

    def _get_kl_value(self, date: pd.Timestamp) -> float | None:
        """查詢指定日期的 KL 值。"""
        if date in self.kl_series.index:
            return float(self.kl_series.loc[date])
        # 向前查找最近的 KL 值
        valid = self.kl_series.index[self.kl_series.index <= date]
        if len(valid) == 0:
            return None
        return float(self.kl_series.loc[valid[-1]])

    def _unified_rebalance(self, date, signal_date, is_rotation_week):
        """覆寫再平衡：先檢查 KL 狀態，再調整 TARGET_HOLDINGS。"""
        kl_val = self._get_kl_value(date)

        if kl_val is not None and kl_val > self.kl_threshold:
            # KL 超標 → 減倉模式
            if not self._kl_active:
                self._kl_active = True
                self.kl_state_log.append({
                    "date": date, "action": "REDUCE",
                    "kl_value": kl_val, "threshold": self.kl_threshold,
                })
            config.TARGET_HOLDINGS = KL_REDUCED_HOLDINGS
        else:
            # KL 正常 → 恢復原設定
            if self._kl_active:
                self._kl_active = False
                self.kl_state_log.append({
                    "date": date, "action": "RESTORE",
                    "kl_value": kl_val, "threshold": self.kl_threshold,
                })
            config.TARGET_HOLDINGS = self._original_target

        # 呼叫原始再平衡邏輯
        super()._unified_rebalance(date, signal_date, is_rotation_week)

    def run(self):
        """覆寫 run，結束後恢復 config。"""
        result = super().run()
        config.TARGET_HOLDINGS = self._original_target  # 確保恢復
        return result


# ─────────────────────────────────────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────────────────────────────────────

def run_comparison():
    print("=" * 70)
    print("KL 散度增強回測：原始 vs KL 預警版")
    print("=" * 70)

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── 載入共用資料 ─────────────────────────────────────────────────────────
    print("\n[1/5] 載入資料...")
    spy_df = utils.load_benchmark(config.ETF_DIR / "SPY.csv")
    sso_df = utils.load_benchmark(config.ETF_DIR / "SSO.csv")
    selector = SelectionEngine()

    start_date = config.START_DATE
    end_date = config.END_DATE

    # ── 預計算 KL 散度 ──────────────────────────────────────────────────────
    print("[2/5] 預計算 SPY KL 散度...")
    kl_series, kl_threshold = precompute_kl_series(spy_df["Close"])
    print(f"      KL 資料點數：{len(kl_series)}")
    print(f"      {KL_PERCENTILE}th percentile 門檻：{kl_threshold:.4f}")

    # ── 跑原始版回測 ─────────────────────────────────────────────────────────
    print("\n[3/5] 回測原始版（TARGET_HOLDINGS={})...".format(config.TARGET_HOLDINGS))
    # 確保 config 是原始值
    config.TARGET_HOLDINGS = 4

    bt_original = SP500Backtester(
        start_date=start_date, end_date=end_date,
        initial_capital=config.INITIAL_CASH, compounding=True,
        selector=selector, spy_df=spy_df, sso_df=sso_df,
        write_reports=True,
    )
    bt_original.run()
    eq_original = bt_original.get_equity_series()
    print(f"      最終資產：${eq_original.iloc[-1]:,.0f}")

    # ── 跑 KL 增強版回測 ────────────────────────────────────────────────────
    print("\n[4/5] 回測 KL 增強版（KL > {:.0f}th pct → 持股降至 {}）...".format(
        KL_PERCENTILE, KL_REDUCED_HOLDINGS))

    # 需要新的 selector（清空 cache 避免干擾）
    selector2 = SelectionEngine(data_cache=selector.data_cache.copy())
    config.TARGET_HOLDINGS = 4  # 先恢復

    bt_kl = KLEnhancedBacktester(
        kl_series=kl_series, kl_threshold=kl_threshold,
        start_date=start_date, end_date=end_date,
        initial_capital=config.INITIAL_CASH, compounding=True,
        selector=selector2, spy_df=spy_df, sso_df=sso_df,
        write_reports=True,
    )
    bt_kl.run()
    eq_kl = bt_kl.get_equity_series()
    print(f"      最終資產：${eq_kl.iloc[-1]:,.0f}")

    # 確保 config 恢復
    config.TARGET_HOLDINGS = 4

    # ── KL 狀態轉換記錄 ─────────────────────────────────────────────────────
    if bt_kl.kl_state_log:
        print(f"\n      KL 狀態轉換共 {len(bt_kl.kl_state_log)} 次：")
        for entry in bt_kl.kl_state_log:
            action_str = "減倉→2" if entry["action"] == "REDUCE" else "恢復→4"
            print(f"        {entry['date'].date()}  {action_str}  "
                  f"KL={entry['kl_value']:.4f}")

    # ── 比較統計 ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("回測比較摘要")
    print("=" * 70)

    # 對齊兩條曲線的日期
    common_idx = eq_original.index.intersection(eq_kl.index)
    eq_orig_aligned = eq_original.loc[common_idx]
    eq_kl_aligned = eq_kl.loc[common_idx]

    # 計算報酬序列
    ret_orig = eq_orig_aligned.pct_change().dropna()
    ret_kl = eq_kl_aligned.pct_change().dropna()

    # SPY benchmark
    spy_close = spy_df["Close"]
    spy_aligned = spy_close.reindex(common_idx, method="pad")
    ret_spy = spy_aligned.pct_change().dropna()

    def calc_stats(returns: pd.Series, equity: pd.Series, label: str):
        n = len(returns)
        total_ret = equity.iloc[-1] / equity.iloc[0] - 1
        cagr = (1 + total_ret) ** (252 / n) - 1
        vol = returns.std() * np.sqrt(252)
        sharpe = cagr / vol if vol > 0 else 0
        dd = (equity / equity.cummax() - 1).min()

        # Calmar ratio
        calmar = cagr / abs(dd) if dd != 0 else 0

        print(f"\n  {label}")
        print(f"  {'─' * 40}")
        print(f"  年化報酬    : {cagr:+.2%}")
        print(f"  年化波動    : {vol:.2%}")
        print(f"  Sharpe      : {sharpe:.2f}")
        print(f"  Max DD      : {dd:.2%}")
        print(f"  Calmar      : {calmar:.2f}")
        print(f"  最終資產    : ${equity.iloc[-1]:,.0f}")
        return {"cagr": cagr, "vol": vol, "sharpe": sharpe, "max_dd": dd, "calmar": calmar}

    stats_orig = calc_stats(ret_orig, eq_orig_aligned, "原始版 (TARGET=4)")
    stats_kl = calc_stats(ret_kl, eq_kl_aligned, f"KL 增強版 (KL>{KL_PERCENTILE}th→TARGET=2)")

    print(f"\n  {'─' * 40}")
    print(f"  KL 版 Sharpe 差異 : {stats_kl['sharpe'] - stats_orig['sharpe']:+.2f}")
    print(f"  KL 版 MaxDD 改善  : {abs(stats_kl['max_dd']) - abs(stats_orig['max_dd']):+.2%}")
    print(f"  KL 版 Calmar 差異 : {stats_kl['calmar'] - stats_orig['calmar']:+.2f}")

    # ── QuantStats 報告 ──────────────────────────────────────────────────────
    print("\n[5/5] 產生 QuantStats 報告...")

    import quantstats as qs

    # 對齊所有序列的起始日期，去除重複 index
    ret_orig = ret_orig[~ret_orig.index.duplicated(keep="last")]
    ret_kl = ret_kl[~ret_kl.index.duplicated(keep="last")]
    ret_spy = ret_spy[~ret_spy.index.duplicated(keep="last")]

    common_start = max(ret_orig.index[0], ret_kl.index[0], ret_spy.index[0])
    ret_orig = ret_orig.loc[common_start:]
    ret_kl = ret_kl.loc[common_start:]
    ret_spy = ret_spy.loc[common_start:]

    # 再次對齊到共同日期
    common_dates = ret_orig.index.intersection(ret_kl.index).intersection(ret_spy.index)
    ret_orig = ret_orig.loc[common_dates]
    ret_kl = ret_kl.loc[common_dates]
    ret_spy = ret_spy.loc[common_dates]

    # 報告 1：KL 增強版 vs SPY
    report_path_kl = _OUTPUT_DIR / "kl_enhanced_vs_spy.html"
    qs.reports.html(
        ret_kl, benchmark=ret_spy,
        title=f"KL Enhanced SP500 Momentum (KL>{KL_PERCENTILE}th to {KL_REDUCED_HOLDINGS} holdings)",
        output=str(report_path_kl),
    )
    print(f"  KL 增強版 vs SPY 報告：{report_path_kl}")

    # 報告 2：原始版 vs SPY
    report_path_orig = _OUTPUT_DIR / "original_vs_spy.html"
    qs.reports.html(
        ret_orig, benchmark=ret_spy,
        title="Original SP500 Momentum (TARGET=4)",
        output=str(report_path_orig),
    )
    print(f"  原始版 vs SPY 報告：{report_path_orig}")

    # ── 自製比較圖表（兩條權益曲線 + KL 狀態）────────────────────────────────
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.gridspec import GridSpec

    fig = plt.figure(figsize=(16, 12))
    gs = GridSpec(3, 1, height_ratios=[5, 2, 1.5], hspace=0.12)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    ax3 = fig.add_subplot(gs[2], sharex=ax1)

    fig.suptitle(
        "SP500 Momentum: Original vs KL-Enhanced\n"
        f"KL params: ref={KL_REF_WINDOW}, cur={KL_CURRENT_WINDOW}, "
        f"threshold={KL_PERCENTILE}th pct, reduce to {KL_REDUCED_HOLDINGS} holdings",
        fontsize=13, fontweight="bold",
    )

    # 上圖：權益曲線
    ax1.plot(eq_orig_aligned.index, eq_orig_aligned.values / eq_orig_aligned.iloc[0] * 100,
             color="#1565C0", lw=1.5, label="Original (TARGET=4)")
    ax1.plot(eq_kl_aligned.index, eq_kl_aligned.values / eq_kl_aligned.iloc[0] * 100,
             color="#F44336", lw=1.5, label=f"KL Enhanced (→{KL_REDUCED_HOLDINGS} when alert)")
    # SPY benchmark
    spy_norm = spy_aligned / spy_aligned.iloc[0] * 100
    ax1.plot(spy_norm.index, spy_norm.values,
             color="#9E9E9E", lw=1.0, ls="--", label="SPY (benchmark)")

    # 標記 KL 減倉期間
    for entry in bt_kl.kl_state_log:
        if entry["action"] == "REDUCE":
            ax1.axvline(entry["date"], color="#FF9800", alpha=0.3, lw=0.8)

    ax1.set_ylabel("Normalized Equity (start = 100)")
    ax1.legend(loc="upper left", fontsize=9)
    ax1.grid(True, alpha=0.3)
    plt.setp(ax1.get_xticklabels(), visible=False)

    # 中圖：Drawdown 比較
    dd_orig = eq_orig_aligned / eq_orig_aligned.cummax() - 1
    dd_kl = eq_kl_aligned / eq_kl_aligned.cummax() - 1

    ax2.fill_between(dd_orig.index, dd_orig.values * 100, 0,
                     alpha=0.3, color="#1565C0", label="Original DD")
    ax2.fill_between(dd_kl.index, dd_kl.values * 100, 0,
                     alpha=0.3, color="#F44336", label="KL Enhanced DD")
    ax2.set_ylabel("Drawdown (%)")
    ax2.legend(loc="lower left", fontsize=9)
    ax2.grid(True, alpha=0.3)
    plt.setp(ax2.get_xticklabels(), visible=False)

    # 下圖：KL 散度
    kl_plot = kl_series.loc[common_idx[0]:common_idx[-1]]
    ax3.plot(kl_plot.index, kl_plot.values, color="#2196F3", lw=0.8, label="KL Divergence")
    ax3.axhline(kl_threshold, color="#F44336", ls="--", lw=1.5,
                label=f"{KL_PERCENTILE}th pct ({kl_threshold:.3f})")
    ax3.fill_between(kl_plot.index, kl_plot.values, kl_threshold,
                     where=(kl_plot.values > kl_threshold),
                     alpha=0.3, color="#F44336")
    ax3.set_ylabel("KL Divergence")
    ax3.set_xlabel("Date")
    ax3.legend(loc="upper left", fontsize=9)
    ax3.grid(True, alpha=0.3)
    ax3.set_ylim(bottom=0)

    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax3.xaxis.set_major_locator(mdates.YearLocator())

    chart_path = _OUTPUT_DIR / "kl_backtest_comparison.png"
    plt.savefig(chart_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  比較圖表：{chart_path}")

    # ── 儲存交易記錄 ─────────────────────────────────────────────────────────
    if bt_kl.trades:
        trades_path = _OUTPUT_DIR / "kl_enhanced_trades.csv"
        pd.DataFrame(bt_kl.trades).to_csv(trades_path, index=False)
        print(f"  KL 版交易記錄：{trades_path}")

    print("\n完成！")


if __name__ == "__main__":
    warnings.filterwarnings("ignore")
    run_comparison()
