"""
test_kl_vs_ma200.py
核心問題：KL 散度能不能比 200MA 更早發出警告？

測試方法：
1. 用不同 current_window（15, 20, 30, 40, 60 天）計算 SPY 報酬 KL 散度
2. 找出每次 KL 首次突破門檻的日期
3. 與 SPY 跌破 200MA 的日期做比較
4. 量化領先/落後天數

同時測試：
- 不同 ref_window（126, 252, 504 天）
- 不同門檻百分位（90th, 95th）
"""

import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.gridspec import GridSpec

_SCRIPT_DIR = Path(__file__).parent.resolve()
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from kl_divergence import compute_kl, returns_to_histogram

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
_SPY_CSV      = _PROJECT_ROOT / "data" / "etfs" / "SPY.csv"
_OUTPUT_DIR   = _SCRIPT_DIR / "output"


# ─────────────────────────────────────────────────────────────────────────────
# 已知市場事件（用來評估信號品質）
# ─────────────────────────────────────────────────────────────────────────────
KNOWN_EVENTS = [
    {
        "name": "2018 VIX Shock",
        "ma200_break": "2018-03-23",   # SPY 首次跌破 200MA
        "crisis_start": "2018-02-02",  # 波動率開始爆發
    },
    {
        "name": "2020 COVID Crash",
        "ma200_break": "2020-02-28",   # SPY 首次跌破 200MA
        "crisis_start": "2020-02-20",  # 歷史高點開始下跌
    },
    {
        "name": "2022 Bear Market",
        "ma200_break": "2022-04-11",   # SPY 首次跌破 200MA
        "crisis_start": "2022-01-04",  # 歷史高點開始下跌
    },
]


def compute_rolling_kl_fast(log_returns: np.ndarray, dates: np.ndarray,
                            ref_window: int, current_window: int,
                            step: int = 1) -> tuple:
    """快速計算滾動 KL 散度，回傳 (dates_array, kl_array)。step=1 可以精確定位首次突破日。"""
    n = len(log_returns)
    min_required = ref_window + current_window
    if n < min_required:
        return np.array([]), np.array([])

    bins = [-np.inf, -0.03, -0.01, -0.003, 0.003, 0.01, 0.03, np.inf]
    kl_dates = []
    kl_values = []

    for t in range(min_required, n + 1, step):
        ref_slice = log_returns[t - ref_window - current_window: t - current_window]
        cur_slice = log_returns[t - current_window: t]

        p_ref = np.histogram(ref_slice, bins=bins)[0].astype(float)
        p_cur = np.histogram(cur_slice, bins=bins)[0].astype(float)

        # 正規化 + epsilon 平滑
        eps = 1e-8
        p_ref = (p_ref + eps); p_ref /= p_ref.sum()
        p_cur = (p_cur + eps); p_cur /= p_cur.sum()

        kl = float(np.sum(p_cur * np.log(p_cur / p_ref)))
        kl_dates.append(dates[min(t - 1, n - 1)])
        kl_values.append(kl)

    return np.array(kl_dates), np.array(kl_values)


def find_first_breach(dates, values, threshold, search_start, search_end):
    """在 [search_start, search_end] 區間內找到 KL 首次突破門檻的日期。"""
    mask = (dates >= np.datetime64(search_start)) & (dates <= np.datetime64(search_end))
    sub_dates = dates[mask]
    sub_values = values[mask]
    breaches = sub_dates[sub_values > threshold]
    if len(breaches) > 0:
        return pd.Timestamp(breaches[0])
    return None


def run_test():
    print("=" * 70)
    print("KL 散度 vs 200MA：信號時效比較測試")
    print("=" * 70)

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── 載入 SPY ─────────────────────────────────────────────────────────────
    spy_df = pd.read_csv(_SPY_CSV, parse_dates=["Date"], index_col="Date").sort_index()
    spy_close = spy_df["Close"]
    spy_ma200 = spy_close.rolling(200).mean()
    log_ret = np.log(spy_close / spy_close.shift(1)).dropna()

    dates_arr = log_ret.index.values
    returns_arr = log_ret.values

    # ── 測試參數組合 ─────────────────────────────────────────────────────────
    test_configs = [
        # (ref_window, current_window, label)
        (252, 15, "ref=252, cur=15"),
        (252, 20, "ref=252, cur=20"),
        (252, 30, "ref=252, cur=30"),
        (252, 40, "ref=252, cur=40"),
        (252, 60, "ref=252, cur=60"),
        (126, 20, "ref=126, cur=20"),
        (126, 30, "ref=126, cur=30"),
        (504, 20, "ref=504, cur=20"),
        (504, 30, "ref=504, cur=30"),
    ]

    # ── 對每個事件分析 ───────────────────────────────────────────────────────
    all_results = []

    for cfg_ref, cfg_cur, cfg_label in test_configs:
        print(f"\n計算 {cfg_label}...")
        kl_dates, kl_values = compute_rolling_kl_fast(
            returns_arr, dates_arr, cfg_ref, cfg_cur, step=1
        )

        if len(kl_values) == 0:
            continue

        # 用不同百分位門檻
        for pct in [90, 95]:
            threshold = float(np.percentile(kl_values, pct))

            for event in KNOWN_EVENTS:
                ma200_date = pd.Timestamp(event["ma200_break"])
                # 搜尋範圍：事件前 120 天 ~ 事件後 30 天
                search_start = ma200_date - pd.Timedelta(days=120)
                search_end = ma200_date + pd.Timedelta(days=60)

                first_kl = find_first_breach(kl_dates, kl_values, threshold,
                                             search_start, search_end)

                if first_kl is not None:
                    lead_days = (ma200_date - first_kl).days  # 正=KL領先, 負=KL落後
                else:
                    lead_days = None

                all_results.append({
                    "config": cfg_label,
                    "percentile": pct,
                    "threshold": threshold,
                    "event": event["name"],
                    "ma200_break": ma200_date.date(),
                    "kl_first_breach": first_kl.date() if first_kl else "N/A",
                    "lead_days": lead_days,  # 正=KL領先200MA, 負=落後
                })

    # ── 結果表格 ─────────────────────────────────────────────────────────────
    results_df = pd.DataFrame(all_results)
    print("\n" + "=" * 70)
    print("信號時效比較結果（正數 = KL 領先 200MA，負數 = KL 落後）")
    print("=" * 70)

    # 用 pivot 方式呈現
    for pct in [90, 95]:
        print(f"\n{'─' * 70}")
        print(f"門檻：{pct}th Percentile")
        print(f"{'─' * 70}")
        sub = results_df[results_df["percentile"] == pct]
        pivot = sub.pivot_table(
            index="config", columns="event", values="lead_days", aggfunc="first"
        )
        # 排序
        order = [c for c in ["2018 VIX Shock", "2020 COVID Crash", "2022 Bear Market"] if c in pivot.columns]
        pivot = pivot[order]
        print(pivot.to_string(float_format=lambda x: f"{x:+.0f}" if pd.notna(x) else "N/A"))

    # ── 儲存完整結果 ─────────────────────────────────────────────────────────
    csv_path = _OUTPUT_DIR / "kl_vs_ma200_comparison.csv"
    results_df.to_csv(csv_path, index=False)
    print(f"\n完整結果已儲存至：{csv_path}")

    # ── 繪製最佳組合的比較圖 ─────────────────────────────────────────────────
    # 選出平均領先天數最大的組合
    summary = results_df.dropna(subset=["lead_days"]).groupby(["config", "percentile"])["lead_days"].mean()
    if len(summary) > 0:
        best_idx = summary.idxmax()
        best_ref_str, best_pct = best_idx
        print(f"\n最佳組合：{best_ref_str}，{best_pct}th pct（平均領先 {summary[best_idx]:+.1f} 天）")

        # 用最佳組合畫圖
        parts = best_ref_str.replace("ref=", "").replace("cur=", "").replace(" ", "").split(",")
        best_ref = int(parts[0])
        best_cur = int(parts[1])

        kl_dates, kl_values = compute_rolling_kl_fast(
            returns_arr, dates_arr, best_ref, best_cur, step=1
        )
        best_threshold = float(np.percentile(kl_values, best_pct))

        fig = plt.figure(figsize=(16, 12))
        gs = GridSpec(3, 1, height_ratios=[4, 2, 2], hspace=0.12)
        ax1 = fig.add_subplot(gs[0])
        ax2 = fig.add_subplot(gs[1], sharex=ax1)
        ax3 = fig.add_subplot(gs[2], sharex=ax1)

        fig.suptitle(
            f"KL Divergence vs 200MA Signal Timing\n"
            f"Best config: {best_ref_str}, {best_pct}th percentile threshold",
            fontsize=13, fontweight="bold",
        )

        # 上圖：SPY 價格 + 200MA
        plot_range = spy_close.loc["2017-01-01":]
        ma_range = spy_ma200.loc["2017-01-01":]
        ax1.plot(plot_range.index, plot_range.values, color="#1565C0", lw=1.2, label="SPY Close")
        ax1.plot(ma_range.index, ma_range.values, color="#FF9800", lw=1.5, ls="--", label="200MA")

        # 標記事件
        colors_ev = ["#9C27B0", "#F44336", "#4CAF50"]
        for i, event in enumerate(KNOWN_EVENTS):
            ax1.axvline(pd.Timestamp(event["ma200_break"]), color=colors_ev[i], ls="-", lw=1.5, alpha=0.7,
                        label=f"{event['name']} 200MA break")

        ax1.set_ylabel("SPY Price (USD)")
        ax1.legend(loc="upper left", fontsize=8)
        ax1.grid(True, alpha=0.3)

        # 中圖：KL 散度 + 門檻
        kl_mask = kl_dates >= np.datetime64("2017-01-01")
        kl_d = kl_dates[kl_mask]
        kl_v = kl_values[kl_mask]

        ax2.plot(kl_d, kl_v, color="#2196F3", lw=0.8, label=f"KL Divergence ({best_ref_str})")
        ax2.axhline(best_threshold, color="#F44336", ls="--", lw=1.5,
                    label=f"{best_pct}th pct threshold ({best_threshold:.3f})")
        ax2.fill_between(kl_d, kl_v, best_threshold,
                         where=(kl_v > best_threshold), alpha=0.3, color="#F44336")

        # 標記 KL 首次突破
        for i, event in enumerate(KNOWN_EVENTS):
            ma200_date = pd.Timestamp(event["ma200_break"])
            search_s = ma200_date - pd.Timedelta(days=120)
            search_e = ma200_date + pd.Timedelta(days=60)
            fb = find_first_breach(kl_dates, kl_values, best_threshold, search_s, search_e)
            if fb:
                ax2.axvline(fb, color=colors_ev[i], ls=":", lw=1.5, alpha=0.8)
                ax1.axvline(fb, color=colors_ev[i], ls=":", lw=1.5, alpha=0.5)

        ax2.set_ylabel("KL Divergence (nats)")
        ax2.legend(loc="upper left", fontsize=8)
        ax2.grid(True, alpha=0.3)
        ax2.set_ylim(bottom=0)

        # 下圖：信號領先/落後天數的時間表
        ax3.set_xlim(ax1.get_xlim())
        event_labels = []
        for i, event in enumerate(KNOWN_EVENTS):
            ma200_date = pd.Timestamp(event["ma200_break"])
            search_s = ma200_date - pd.Timedelta(days=120)
            search_e = ma200_date + pd.Timedelta(days=60)
            fb = find_first_breach(kl_dates, kl_values, best_threshold, search_s, search_e)

            y_pos = 2 - i
            # 200MA break marker
            ax3.plot(ma200_date, y_pos, "s", color=colors_ev[i], markersize=12, zorder=5)
            ax3.annotate("200MA", (ma200_date, y_pos), textcoords="offset points",
                         xytext=(0, 12), ha="center", fontsize=7, color=colors_ev[i])

            if fb:
                ax3.plot(fb, y_pos, "^", color=colors_ev[i], markersize=12, zorder=5)
                ax3.annotate("KL", (fb, y_pos), textcoords="offset points",
                             xytext=(0, -15), ha="center", fontsize=7, color=colors_ev[i])
                # 連線
                ax3.annotate("", xy=(ma200_date, y_pos), xytext=(fb, y_pos),
                             arrowprops=dict(arrowstyle="->", color=colors_ev[i], lw=1.5))
                lead = (ma200_date - fb).days
                mid = fb + (ma200_date - fb) / 2
                ax3.text(mid, y_pos + 0.25, f"{lead:+d} days",
                         ha="center", fontsize=9, fontweight="bold", color=colors_ev[i])

            event_labels.append(event["name"])

        ax3.set_yticks([0, 1, 2])
        ax3.set_yticklabels(list(reversed(event_labels)), fontsize=9)
        ax3.set_ylim(-0.5, 2.8)
        ax3.set_xlabel("Date")
        ax3.grid(True, alpha=0.2, axis="x")
        ax3.set_title("Signal Lead/Lag (△ = KL first breach, ■ = 200MA break)", fontsize=10)

        ax3.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
        ax3.xaxis.set_major_locator(mdates.YearLocator())

        output_path = _OUTPUT_DIR / "kl_vs_ma200_timing.png"
        plt.savefig(output_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        print(f"比較圖表已儲存至：{output_path}")

    # ── 額外：暴力掃描最佳 current_window ────────────────────────────────────
    print("\n" + "=" * 70)
    print("暴力掃描：ref=252, current_window 5~80, 90th pct")
    print("=" * 70)

    sweep_results = []
    for cw in range(5, 81, 5):
        kl_d, kl_v = compute_rolling_kl_fast(returns_arr, dates_arr, 252, cw, step=1)
        if len(kl_v) == 0:
            continue
        th = float(np.percentile(kl_v, 90))

        leads = []
        for event in KNOWN_EVENTS:
            ma_date = pd.Timestamp(event["ma200_break"])
            fb = find_first_breach(kl_d, kl_v, th, ma_date - pd.Timedelta(days=120),
                                   ma_date + pd.Timedelta(days=60))
            if fb:
                leads.append((ma_date - fb).days)

        avg_lead = np.mean(leads) if leads else None
        min_lead = min(leads) if leads else None
        sweep_results.append({
            "current_window": cw,
            "threshold_90pct": th,
            "avg_lead_days": avg_lead,
            "min_lead_days": min_lead,
            "events_detected": len(leads),
        })
        if avg_lead is not None:
            print(f"  cur={cw:3d}d  threshold={th:.4f}  "
                  f"avg_lead={avg_lead:+6.1f}d  min_lead={min_lead:+4d}d  "
                  f"detected={len(leads)}/3")

    sweep_df = pd.DataFrame(sweep_results)
    sweep_csv = _OUTPUT_DIR / "kl_window_sweep.csv"
    sweep_df.to_csv(sweep_csv, index=False)
    print(f"\n掃描結果已儲存至：{sweep_csv}")

    # 找最佳 current_window（三個事件都偵測到 + 平均領先最大）
    valid = sweep_df[(sweep_df["events_detected"] == 3) & (sweep_df["avg_lead_days"].notna())]
    if not valid.empty:
        best_row = valid.loc[valid["avg_lead_days"].idxmax()]
        print(f"\n最佳 current_window = {int(best_row['current_window'])} 天")
        print(f"  平均領先 200MA：{best_row['avg_lead_days']:+.1f} 天")
        print(f"  最少領先 200MA：{best_row['min_lead_days']:+.0f} 天")
    else:
        print("\n沒有任何 current_window 能同時偵測到三個事件。")


if __name__ == "__main__":
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    run_test()
