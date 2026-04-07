"""
test_kl_replace_ma200.py
用 KL 散度完全取代 200MA 作為牛熊濾網。

原始邏輯：
  - SPY < 200MA → 清掉個股、進入熊市抄底模式
  - SPY > 200MA 連續 2 週 → 牛市確認、恢復選股

KL 取代邏輯：
  - KL > threshold → 清掉個股、進入熊市抄底模式
  - KL < threshold 連續 2 週 → 牛市確認、恢復選股

同時測試多組 KL 參數，找出最佳取代方案。
"""

import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_SCRIPT_DIR = Path(__file__).parent.resolve()
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from strategies.sp500_momentum import config
from strategies.sp500_momentum.selection import SelectionEngine
from strategies.sp500_momentum.backtester import SP500Backtester
from strategies.sp500_momentum import utils

_OUTPUT_DIR = _SCRIPT_DIR / "output"

# 報酬離散化 bins
_RETURN_BINS = [-np.inf, -0.03, -0.01, -0.003, 0.003, 0.01, 0.03, np.inf]


# ─────────────────────────────────────────────────────────────────────────────
# KL 散度預計算
# ─────────────────────────────────────────────────────────────────────────────

def precompute_kl_series(spy_close: pd.Series, ref_window: int, current_window: int):
    """預計算整段 SPY 的滾動 KL 散度，回傳 pd.Series。"""
    log_ret = np.log(spy_close / spy_close.shift(1)).dropna()
    values = log_ret.values
    dates = log_ret.index.values
    n = len(values)
    min_req = ref_window + current_window

    kl_dates, kl_values = [], []
    for t in range(min_req, n + 1):
        ref_slice = values[t - ref_window - current_window: t - current_window]
        cur_slice = values[t - current_window: t]

        p_ref = np.histogram(ref_slice, bins=_RETURN_BINS)[0].astype(float)
        p_cur = np.histogram(cur_slice, bins=_RETURN_BINS)[0].astype(float)

        eps = 1e-8
        p_ref = (p_ref + eps); p_ref /= p_ref.sum()
        p_cur = (p_cur + eps); p_cur /= p_cur.sum()

        kl = float(np.sum(p_cur * np.log(p_cur / p_ref)))
        kl_dates.append(dates[min(t - 1, n - 1)])
        kl_values.append(kl)

    return pd.Series(kl_values, index=pd.DatetimeIndex(kl_dates), name="kl")


# ─────────────────────────────────────────────────────────────────────────────
# KL 取代 200MA 的 Backtester
# ─────────────────────────────────────────────────────────────────────────────

class KLRegimeBacktester(SP500Backtester):
    """
    用 KL 散度取代 SPY 200MA 作為牛熊判斷：
      - KL > threshold → 熊市（清個股、可抄底）
      - KL < threshold 連續 2 週 → 牛市確認
    其餘邏輯（選股、ATR 權重、停損等）完全不變。
    """

    def __init__(self, kl_series: pd.Series, kl_threshold: float, **kwargs):
        self.kl_series = kl_series
        self.kl_threshold = kl_threshold
        self.kl_regime_log = []
        super().__init__(**kwargs)

    def _get_kl_value(self, date: pd.Timestamp) -> float | None:
        if date in self.kl_series.index:
            return float(self.kl_series.loc[date])
        valid = self.kl_series.index[self.kl_series.index <= date]
        if len(valid) == 0:
            return None
        return float(self.kl_series.loc[valid[-1]])

    def _check_bear_sso_logic(self, date):
        """
        完全覆寫：用 KL 散度取代 200MA 判斷牛熊。
        但抄底仍然用 SPY drawdown（DD）觸發，因為 DD 是價格層面的訊號。
        """
        state = self.market_regime.get_state(date)
        if not state:
            return

        spy_dd = abs(state["SPY_DD"])
        is_rebal = (date.weekday() == config.REBALANCE_WEEKDAY)

        # ── 用 KL 取代 200MA 判斷 ────────────────────────────────────────────
        kl_val = self._get_kl_value(date)
        if kl_val is None:
            # KL 還沒有足夠數據，fallback 到不動作
            return

        kl_is_normal = (kl_val <= self.kl_threshold)

        if is_rebal:
            self.bull_weeks_counter = (self.bull_weeks_counter + 1
                                       if kl_is_normal else 0)

        bull_confirmed = (self.bull_weeks_counter >= 2)

        if bull_confirmed:
            # 牛市確認：清掉所有 dip 持倉
            if is_rebal and any(t in self.holdings for t in self._dip_tickers):
                self._close_positions(date, "DIP")
                for k in self.dip_state:
                    self.dip_state[k] = False
            return

        # ── 熊市邏輯：KL 超標 → 清個股 ──────────────────────────────────────
        if kl_is_normal:
            return  # KL 正常但牛市週數未滿 2 週，不動作

        stocks = [t for t in self.holdings if t != config.DIP_BUY_TICKER]
        if stocks:
            self._close_positions(date, "STOCK")
            self.target_weights.clear()
            self.kl_regime_log.append({
                "date": date, "action": "BEAR_CLOSE",
                "kl_value": kl_val,
            })

        # ── 抄底邏輯（仍用 SPY DD 觸發）─────────────────────────────────────
        if not config.ENABLE_BEAR_DIP_BUY:
            return

        alloc_map = {0.15: 0.30, 0.20: 0.30, 0.25: 0.40}
        for level in [0.15, 0.20, 0.25]:
            if spy_dd >= level and not self.dip_state[level]:
                alloc = alloc_map[level]
                dip_ticker = self.dip_ticker_map[level]
                eq = (self._get_total_equity(date)
                      if self.compounding else self.initial_capital)
                amt = min(eq * alloc, self.cash)
                price = self._get_price(dip_ticker, date)
                if price > 0 and amt > 0:
                    qty = int(amt / (price * (1 + config.COMMISSION)))
                    if qty > 0:
                        self._buy(dip_ticker, date, price, qty,
                                  f"KL熊市抄底 -{level*100:.0f}% ({dip_ticker})")
                        self.dip_state[level] = True


# ─────────────────────────────────────────────────────────────────────────────
# 統計計算
# ─────────────────────────────────────────────────────────────────────────────

def calc_stats(equity: pd.Series) -> dict:
    ret = equity.pct_change().dropna()
    n = len(ret)
    if n < 10:
        return {}
    total_ret = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (1 + total_ret) ** (252 / n) - 1
    vol = ret.std() * np.sqrt(252)
    sharpe = cagr / vol if vol > 0 else 0
    dd = (equity / equity.cummax() - 1).min()
    calmar = cagr / abs(dd) if dd != 0 else 0
    return {
        "cagr": cagr, "vol": vol, "sharpe": sharpe,
        "max_dd": dd, "calmar": calmar, "final": equity.iloc[-1],
    }


# ─────────────────────────────────────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────────────────────────────────────

def run_comparison():
    print("=" * 70)
    print("KL 散度取代 200MA 濾網：完整回測比較")
    print("=" * 70)

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── 載入資料 ─────────────────────────────────────────────────────────────
    print("\n[1/4] 載入資料...")
    spy_df = utils.load_benchmark(config.ETF_DIR / "SPY.csv")
    sso_df = utils.load_benchmark(config.ETF_DIR / "SSO.csv")
    selector = SelectionEngine()

    start_date = config.START_DATE
    end_date = config.END_DATE

    # ── 跑原始版（基準線）────────────────────────────────────────────────────
    print("\n[2/4] 回測原始版（200MA 濾網）...")
    config.TARGET_HOLDINGS = 4

    bt_orig = SP500Backtester(
        start_date=start_date, end_date=end_date,
        initial_capital=config.INITIAL_CASH, compounding=True,
        selector=selector, spy_df=spy_df, sso_df=sso_df,
        write_reports=True,
    )
    bt_orig.run()
    eq_orig = bt_orig.get_equity_series()
    stats_orig = calc_stats(eq_orig)
    print(f"      最終：${stats_orig['final']:,.0f}  "
          f"CAGR={stats_orig['cagr']:+.2%}  Sharpe={stats_orig['sharpe']:.2f}  "
          f"MaxDD={stats_orig['max_dd']:.2%}")

    # ── 測試多組 KL 參數 ─────────────────────────────────────────────────────
    print("\n[3/4] 測試 KL 取代 200MA（多組參數）...")

    test_configs = [
        # (ref_window, current_window, percentile, label)
        (252, 10, 90, "ref252_cur10_p90"),
        (252, 15, 90, "ref252_cur15_p90"),
        (252, 20, 90, "ref252_cur20_p90"),
        (252, 20, 85, "ref252_cur20_p85"),
        (252, 20, 95, "ref252_cur20_p95"),
        (252, 30, 90, "ref252_cur30_p90"),
        (126, 20, 90, "ref126_cur20_p90"),
        (126, 15, 90, "ref126_cur15_p90"),
    ]

    all_results = []
    best_sharpe = -999
    best_label = ""
    best_eq = None
    best_bt = None

    for ref_w, cur_w, pct, label in test_configs:
        print(f"\n  測試 {label}...")

        # 預計算 KL
        kl_series = precompute_kl_series(spy_df["Close"], ref_w, cur_w)
        threshold = float(np.percentile(kl_series.values, pct))
        print(f"    threshold={threshold:.4f}")

        # 新 selector（共用 data cache）
        sel = SelectionEngine(data_cache=selector.data_cache.copy())
        config.TARGET_HOLDINGS = 4

        bt = KLRegimeBacktester(
            kl_series=kl_series, kl_threshold=threshold,
            start_date=start_date, end_date=end_date,
            initial_capital=config.INITIAL_CASH, compounding=True,
            selector=sel, spy_df=spy_df, sso_df=sso_df,
            write_reports=False,
        )
        bt.run()
        config.TARGET_HOLDINGS = 4

        eq = bt.get_equity_series()
        stats = calc_stats(eq)
        bear_closes = len([e for e in bt.kl_regime_log if e["action"] == "BEAR_CLOSE"])

        print(f"    最終：${stats['final']:,.0f}  "
              f"CAGR={stats['cagr']:+.2%}  Sharpe={stats['sharpe']:.2f}  "
              f"MaxDD={stats['max_dd']:.2%}  熊市清倉次數={bear_closes}")

        result = {
            "label": label, "ref": ref_w, "cur": cur_w, "pct": pct,
            "threshold": threshold, "bear_closes": bear_closes,
            **stats,
        }
        all_results.append(result)

        if stats["sharpe"] > best_sharpe:
            best_sharpe = stats["sharpe"]
            best_label = label
            best_eq = eq
            best_bt = bt

    # ── 結果比較表 ───────────────────────────────────────────────────────────
    print("\n\n" + "=" * 70)
    print("完整比較結果")
    print("=" * 70)

    results_df = pd.DataFrame(all_results)

    # 加上原始版作為基準
    orig_row = {
        "label": "ORIGINAL_MA200", "ref": "-", "cur": "-", "pct": "-",
        "threshold": "-", "bear_closes": "-", **stats_orig,
    }

    print(f"\n{'配置':<22} {'CAGR':>8} {'Vol':>8} {'Sharpe':>8} "
          f"{'MaxDD':>8} {'Calmar':>8} {'最終資產':>14} {'清倉次數':>8}")
    print("─" * 100)

    # 原始版
    print(f"{'ORIGINAL (MA200)':<22} {stats_orig['cagr']:>+7.2%} {stats_orig['vol']:>7.2%} "
          f"{stats_orig['sharpe']:>7.2f} {stats_orig['max_dd']:>+7.2%} "
          f"{stats_orig['calmar']:>7.2f} {stats_orig['final']:>13,.0f} {'N/A':>8}")

    for r in all_results:
        print(f"{r['label']:<22} {r['cagr']:>+7.2%} {r['vol']:>7.2%} "
              f"{r['sharpe']:>7.2f} {r['max_dd']:>+7.2%} "
              f"{r['calmar']:>7.2f} {r['final']:>13,.0f} {r['bear_closes']:>8}")

    print("─" * 100)

    # 找出各指標最佳
    best_sharpe_row = max(all_results, key=lambda x: x["sharpe"])
    best_calmar_row = max(all_results, key=lambda x: x["calmar"])
    best_dd_row = max(all_results, key=lambda x: x["max_dd"])  # max_dd is negative, bigger = better
    best_cagr_row = max(all_results, key=lambda x: x["cagr"])

    print(f"\n最佳 Sharpe  : {best_sharpe_row['label']} ({best_sharpe_row['sharpe']:.2f}) "
          f"vs 原始 {stats_orig['sharpe']:.2f} → {best_sharpe_row['sharpe']-stats_orig['sharpe']:+.2f}")
    print(f"最佳 Calmar  : {best_calmar_row['label']} ({best_calmar_row['calmar']:.2f}) "
          f"vs 原始 {stats_orig['calmar']:.2f} → {best_calmar_row['calmar']-stats_orig['calmar']:+.2f}")
    print(f"最小 MaxDD   : {best_dd_row['label']} ({best_dd_row['max_dd']:.2%}) "
          f"vs 原始 {stats_orig['max_dd']:.2%} → {abs(best_dd_row['max_dd'])-abs(stats_orig['max_dd']):+.2%}")
    print(f"最高 CAGR    : {best_cagr_row['label']} ({best_cagr_row['cagr']:.2%}) "
          f"vs 原始 {stats_orig['cagr']:.2%} → {best_cagr_row['cagr']-stats_orig['cagr']:+.2%}")

    # 儲存結果 CSV
    csv_path = _OUTPUT_DIR / "kl_replace_ma200_results.csv"
    pd.DataFrame([orig_row] + all_results).to_csv(csv_path, index=False)
    print(f"\n結果已儲存：{csv_path}")

    # ── QuantStats 報告（最佳 Sharpe vs 原始）────────────────────────────────
    print("\n[4/4] 產生 QuantStats 報告...")
    import quantstats as qs

    # 對齊
    common_idx = eq_orig.index.intersection(best_eq.index)
    ret_orig = eq_orig.loc[common_idx].pct_change().dropna()
    ret_best = best_eq.loc[common_idx].pct_change().dropna()
    spy_close = spy_df["Close"].reindex(common_idx, method="pad")
    ret_spy = spy_close.pct_change().dropna()

    # 去重
    ret_orig = ret_orig[~ret_orig.index.duplicated(keep="last")]
    ret_best = ret_best[~ret_best.index.duplicated(keep="last")]
    ret_spy = ret_spy[~ret_spy.index.duplicated(keep="last")]

    common_dates = ret_orig.index.intersection(ret_best.index).intersection(ret_spy.index)
    ret_orig = ret_orig.loc[common_dates]
    ret_best = ret_best.loc[common_dates]
    ret_spy = ret_spy.loc[common_dates]

    # KL 版 vs SPY
    report1 = _OUTPUT_DIR / "kl_regime_best_vs_spy.html"
    qs.reports.html(
        ret_best, benchmark=ret_spy,
        title=f"KL Regime ({best_label}) vs SPY",
        output=str(report1),
    )
    print(f"  最佳 KL 版 vs SPY：{report1}")

    # 原始版 vs SPY（如果之前沒生成）
    report2 = _OUTPUT_DIR / "original_ma200_vs_spy.html"
    qs.reports.html(
        ret_orig, benchmark=ret_spy,
        title="Original (MA200 Regime) vs SPY",
        output=str(report2),
    )
    print(f"  原始 MA200 版 vs SPY：{report2}")

    # ── 比較圖表 ─────────────────────────────────────────────────────────────
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.gridspec import GridSpec

    fig = plt.figure(figsize=(16, 14))
    gs = GridSpec(4, 1, height_ratios=[5, 2, 1.5, 1.5], hspace=0.12)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    ax3 = fig.add_subplot(gs[2], sharex=ax1)
    ax4 = fig.add_subplot(gs[3], sharex=ax1)

    fig.suptitle(
        f"200MA vs KL Regime Filter Comparison\n"
        f"Best KL config: {best_label}",
        fontsize=13, fontweight="bold",
    )

    # 上圖：權益曲線（正規化）
    eq_o = eq_orig.loc[common_idx]
    eq_b = best_eq.loc[common_idx]
    spy_n = spy_close.reindex(common_idx, method="pad")

    ax1.plot(eq_o.index, eq_o / eq_o.iloc[0] * 100,
             color="#1565C0", lw=1.5, label="Original (MA200)")
    ax1.plot(eq_b.index, eq_b / eq_b.iloc[0] * 100,
             color="#F44336", lw=1.5, label=f"KL Regime ({best_label})")
    ax1.plot(spy_n.index, spy_n / spy_n.iloc[0] * 100,
             color="#9E9E9E", lw=1.0, ls="--", label="SPY")
    ax1.set_ylabel("Normalized Equity (100)")
    ax1.legend(loc="upper left", fontsize=9)
    ax1.grid(True, alpha=0.3)
    plt.setp(ax1.get_xticklabels(), visible=False)

    # Drawdown
    dd_o = eq_o / eq_o.cummax() - 1
    dd_b = eq_b / eq_b.cummax() - 1
    ax2.fill_between(dd_o.index, dd_o * 100, 0, alpha=0.3, color="#1565C0", label="MA200 DD")
    ax2.fill_between(dd_b.index, dd_b * 100, 0, alpha=0.3, color="#F44336", label="KL DD")
    ax2.set_ylabel("Drawdown (%)")
    ax2.legend(loc="lower left", fontsize=9)
    ax2.grid(True, alpha=0.3)
    plt.setp(ax2.get_xticklabels(), visible=False)

    # KL 散度
    parts = best_label.split("_")
    best_ref = int(parts[0].replace("ref", ""))
    best_cur = int(parts[1].replace("cur", ""))
    best_pct = int(parts[2].replace("p", ""))
    kl_s = precompute_kl_series(spy_df["Close"], best_ref, best_cur)
    kl_th = float(np.percentile(kl_s.values, best_pct))
    kl_plot = kl_s.loc[common_idx[0]:common_idx[-1]]

    ax3.plot(kl_plot.index, kl_plot.values, color="#2196F3", lw=0.8, label="KL Divergence")
    ax3.axhline(kl_th, color="#F44336", ls="--", lw=1.5,
                label=f"{best_pct}th pct ({kl_th:.3f})")
    ax3.fill_between(kl_plot.index, kl_plot.values, kl_th,
                     where=(kl_plot.values > kl_th), alpha=0.3, color="#F44336")
    ax3.set_ylabel("KL Divergence")
    ax3.legend(loc="upper left", fontsize=8)
    ax3.grid(True, alpha=0.3)
    ax3.set_ylim(bottom=0)
    plt.setp(ax3.get_xticklabels(), visible=False)

    # SPY vs 200MA（原始濾網參考）
    spy_ma = spy_df["Close"].rolling(200).mean()
    spy_p = spy_df["Close"].loc[common_idx[0]:common_idx[-1]]
    ma_p = spy_ma.loc[common_idx[0]:common_idx[-1]]
    ax4.plot(spy_p.index, spy_p, color="#1565C0", lw=1.0, label="SPY")
    ax4.plot(ma_p.index, ma_p, color="#FF9800", lw=1.2, ls="--", label="200MA")
    ax4.set_ylabel("SPY Price")
    ax4.set_xlabel("Date")
    ax4.legend(loc="upper left", fontsize=8)
    ax4.grid(True, alpha=0.3)

    ax4.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax4.xaxis.set_major_locator(mdates.YearLocator())

    chart_path = _OUTPUT_DIR / "kl_replace_ma200_comparison.png"
    plt.savefig(chart_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  比較圖表：{chart_path}")

    print("\n完成！")


if __name__ == "__main__":
    warnings.filterwarnings("ignore")
    run_comparison()
