"""
QuantCore Robustness — Monte Carlo Simulation
==============================================

對三個策略（SR、SP500、Combo_80_50_1.3x）進行蒙地卡羅模擬：
  方法 A：Trade Reshuffling（每日報酬無放回洗牌）
  方法 B：Bootstrap（有放回抽樣）

輸出：
  - mc_stats_{ts}.csv           每個（策略 × 方法）的統計摘要
  - mc_paths_sample_{ts}.csv    每個策略 100 條樣本路徑
  - mc_ruin_prob_{ts}.csv       不同 MDD 門檻的破產機率
  - 9 張 PNG：6 張 fanchart + MDD 分布 + 終端財富 + 連續虧損天數
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from robustness.utils import (
    setup_dark_theme,
    save_fig,
    timestamp,
    OUTPUT_ROOT,
    DARK_BG,
    DARK_FG,
    ACCENT,
    ACCENT_2,
    ACCENT_3,
    ACCENT_4,
)

# ── 參數 ──────────────────────────────────────────────────────────────────────
N_SIM       = 10_000
N_SAMPLE    = 100      # 保存的樣本路徑數
ANN         = 252
SEED        = 42

STRATEGIES  = ["SR", "SP500", "Combo_80_50_1.3x"]
METHODS     = ["reshuffle", "bootstrap"]
MDD_THRESHOLDS = [0.20, 0.30, 0.40, 0.50]

BASELINE_CSV = OUTPUT_ROOT / "baseline_returns.csv"
OUT_DIR      = OUTPUT_ROOT / "monte_carlo"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ── 向量化模擬 ────────────────────────────────────────────────────────────────

def simulate_paths(
    returns: np.ndarray,
    n_sim: int,
    method: str,
    rng: np.random.Generator,
) -> np.ndarray:
    """
    回傳 shape (n_sim, n_days) 的模擬日報酬矩陣。

    method:
      - "reshuffle" : 每條路徑是原序列的隨機排列（無放回）
      - "bootstrap" : 有放回抽樣
    """
    n = len(returns)
    if method == "reshuffle":
        # 對每一列獨立洗牌：先平鋪再用 argsort 隨機
        rand = rng.random((n_sim, n))
        idx = np.argsort(rand, axis=1)
        paths = returns[idx]
    elif method == "bootstrap":
        idx = rng.integers(0, n, size=(n_sim, n))
        paths = returns[idx]
    else:
        raise ValueError(method)
    return paths


def compute_path_metrics(paths: np.ndarray, ann: int = ANN) -> dict:
    """
    向量化計算每條路徑的：
      - terminal   : 累積報酬 prod(1+r) - 1
      - mdd        : 最大回撤（負值）
      - longest_loss_streak : 最長連續負報酬天數
      - ann_ret    : 年化報酬
      - sharpe
    paths: shape (n_sim, n_days)
    """
    n_sim, n_days = paths.shape
    equity = np.cumprod(1.0 + paths, axis=1)          # (n_sim, n_days)
    terminal = equity[:, -1] - 1.0

    # MDD（向量化）
    running_max = np.maximum.accumulate(equity, axis=1)
    dd = equity / running_max - 1.0
    mdd = dd.min(axis=1)

    # 年化報酬、Sharpe
    ann_ret = (1.0 + terminal) ** (ann / n_days) - 1.0
    std = paths.std(axis=1, ddof=1)
    ann_vol = std * np.sqrt(ann)
    sharpe = np.where(ann_vol > 0, ann_ret / ann_vol, np.nan)

    # 最長連續下跌天數（向量化）
    neg = (paths < 0).astype(np.int32)
    longest = np.zeros(n_sim, dtype=np.int32)
    cur = np.zeros(n_sim, dtype=np.int32)
    for t in range(n_days):
        col = neg[:, t]
        cur = np.where(col == 1, cur + 1, 0)
        longest = np.maximum(longest, cur)

    return {
        "terminal": terminal,
        "mdd": mdd,
        "ann_ret": ann_ret,
        "sharpe": sharpe,
        "longest_loss_streak": longest,
        "equity": equity,
    }


# ── 統計彙整 ──────────────────────────────────────────────────────────────────

def summarize(vec: np.ndarray, name: str) -> dict:
    pcts = np.percentile(vec, [5, 25, 50, 75, 95])
    return {
        f"{name}_mean":   float(np.mean(vec)),
        f"{name}_std":    float(np.std(vec, ddof=1)),
        f"{name}_p05":    float(pcts[0]),
        f"{name}_p25":    float(pcts[1]),
        f"{name}_p50":    float(pcts[2]),
        f"{name}_p75":    float(pcts[3]),
        f"{name}_p95":    float(pcts[4]),
    }


def compute_original_metrics(r: np.ndarray, ann: int = ANN) -> dict:
    equity = np.cumprod(1.0 + r)
    terminal = equity[-1] - 1.0
    running_max = np.maximum.accumulate(equity)
    dd = equity / running_max - 1.0
    mdd = dd.min()
    ann_ret = (1.0 + terminal) ** (ann / len(r)) - 1.0
    ann_vol = r.std(ddof=1) * np.sqrt(ann)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else np.nan
    # longest loss streak
    cur = 0
    longest = 0
    for x in r:
        if x < 0:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 0
    return {
        "terminal": terminal,
        "mdd": mdd,
        "ann_ret": ann_ret,
        "sharpe": sharpe,
        "longest_loss_streak": longest,
    }


# ── 圖表 ──────────────────────────────────────────────────────────────────────

def plot_fanchart(equity: np.ndarray, original_r: np.ndarray, title: str, path: Path):
    """equity: (n_sim, n_days), original_r: (n_days,)."""
    n_sim, n_days = equity.shape
    x = np.arange(n_days)
    pcts = np.percentile(equity, [5, 25, 50, 75, 95], axis=0)

    original_eq = np.cumprod(1.0 + original_r)

    fig, ax = plt.subplots(figsize=(11, 6))
    ax.fill_between(x, pcts[0], pcts[4], color=ACCENT, alpha=0.15, label="5–95%")
    ax.fill_between(x, pcts[1], pcts[3], color=ACCENT, alpha=0.30, label="25–75%")
    ax.plot(x, pcts[2], color=ACCENT, lw=1.8, label="Median")
    ax.plot(x, original_eq, color=ACCENT_2, lw=1.8, label="Original", alpha=0.95)

    ax.set_yscale("log")
    ax.set_title(title, fontsize=12)
    ax.set_xlabel("Days")
    ax.set_ylabel("Equity (log)")
    ax.legend(loc="upper left", framealpha=0.3)
    save_fig(fig, path)


def plot_mdd_distribution(mdds: dict, orig_mdds: dict, path: Path):
    """
    mdds: {(strategy, method): ndarray}
    orig_mdds: {strategy: float}
    """
    fig, axes = plt.subplots(3, 2, figsize=(13, 11))
    for i, strat in enumerate(STRATEGIES):
        for j, method in enumerate(METHODS):
            ax = axes[i, j]
            vec = mdds[(strat, method)] * 100.0  # to %
            orig = orig_mdds[strat] * 100.0
            ax.hist(vec, bins=60, color=ACCENT, alpha=0.75, edgecolor=DARK_FG, linewidth=0.2)
            ax.axvline(orig, color=ACCENT_2, lw=2, label=f"Original {orig:.1f}%")
            pct_of_orig = (vec < orig).mean() * 100.0  # where original sits (lower = worse MDD)
            ax.set_title(f"{strat} — {method}  (orig @ p{pct_of_orig:.0f})", fontsize=10)
            ax.set_xlabel("MDD (%)")
            ax.set_ylabel("Count")
            ax.legend(fontsize=8, framealpha=0.3)
    fig.suptitle("Monte Carlo — Max Drawdown Distribution", fontsize=13, color=DARK_FG)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    save_fig(fig, path)


def plot_terminal_wealth(terms: dict, orig_terms: dict, path: Path):
    fig, axes = plt.subplots(3, 2, figsize=(13, 11))
    for i, strat in enumerate(STRATEGIES):
        for j, method in enumerate(METHODS):
            ax = axes[i, j]
            vec = (1.0 + terms[(strat, method)]) * 100.0  # final wealth %
            orig = (1.0 + orig_terms[strat]) * 100.0
            ax.hist(vec, bins=60, color=ACCENT_4, alpha=0.75, edgecolor=DARK_FG, linewidth=0.2)
            ax.axvline(orig, color=ACCENT_2, lw=2, label=f"Original {orig:.0f}%")
            ax.set_title(f"{strat} — {method}", fontsize=10)
            ax.set_xlabel("Terminal wealth (%)")
            ax.set_ylabel("Count")
            ax.set_xscale("log")
            ax.legend(fontsize=8, framealpha=0.3)
    fig.suptitle("Monte Carlo — Terminal Wealth Distribution", fontsize=13, color=DARK_FG)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    save_fig(fig, path)


def plot_longest_streak(streaks: dict, orig_streaks: dict, path: Path):
    """3 策略並排，每張合併兩個方法。"""
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    for i, strat in enumerate(STRATEGIES):
        ax = axes[i]
        for method, color in zip(METHODS, [ACCENT, ACCENT_3]):
            vec = streaks[(strat, method)]
            ax.hist(vec, bins=range(int(vec.min()), int(vec.max()) + 2),
                    color=color, alpha=0.55, label=method, edgecolor=DARK_FG, linewidth=0.2)
        orig = orig_streaks[strat]
        ax.axvline(orig, color=ACCENT_2, lw=2, label=f"Original {orig}")
        ax.set_title(strat, fontsize=11)
        ax.set_xlabel("Longest losing streak (days)")
        ax.set_ylabel("Count")
        ax.legend(fontsize=8, framealpha=0.3)
    fig.suptitle("Monte Carlo — Longest Consecutive Losing Days", fontsize=13, color=DARK_FG)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    save_fig(fig, path)


# ── 主程式 ────────────────────────────────────────────────────────────────────

def main():
    t0 = time.time()
    setup_dark_theme()
    rng = np.random.default_rng(SEED)
    np.random.seed(SEED)

    ts = timestamp()
    print("=" * 70)
    print(f"QuantCore Monte Carlo  |  N_SIM={N_SIM:,}  seed={SEED}")
    print("=" * 70)

    # 讀基準日報酬
    df = pd.read_csv(BASELINE_CSV, parse_dates=["Date"], index_col="Date")
    print(f"Baseline: {len(df)} days, {df.index[0].date()} → {df.index[-1].date()}")
    print(f"Strategies: {STRATEGIES}\n")

    # 結果容器
    mdd_store        = {}  # (strat, method) -> ndarray
    term_store       = {}
    streak_store     = {}
    equity_store     = {}  # (strat, method) -> (n_sim, n_days) — 只留 fanchart 用
    stats_rows       = []
    ruin_rows        = []
    sample_paths_rec = []  # 存樣本路徑用

    orig_metrics_all = {}

    for strat in STRATEGIES:
        r = df[strat].dropna().to_numpy()
        orig = compute_original_metrics(r)
        orig_metrics_all[strat] = orig
        print(f"[{strat}]  days={len(r)}  "
              f"orig CAGR={orig['ann_ret']*100:+.2f}%  "
              f"MDD={orig['mdd']*100:.2f}%  "
              f"Sharpe={orig['sharpe']:.2f}  "
              f"longest_loss={orig['longest_loss_streak']}")

        for method in METHODS:
            t1 = time.time()
            paths = simulate_paths(r, N_SIM, method, rng)
            m = compute_path_metrics(paths)

            mdd_store[(strat, method)]    = m["mdd"]
            term_store[(strat, method)]   = m["terminal"]
            streak_store[(strat, method)] = m["longest_loss_streak"]
            equity_store[(strat, method)] = m["equity"]

            # 統計摘要
            row = {"strategy": strat, "method": method, "n_sim": N_SIM, "n_days": len(r)}
            row.update(summarize(m["mdd"],                 "mdd"))
            row.update(summarize(m["terminal"],            "terminal"))
            row.update(summarize(m["ann_ret"],             "ann_ret"))
            row.update(summarize(m["sharpe"],              "sharpe"))
            row.update(summarize(m["longest_loss_streak"].astype(float), "longest_loss"))

            # 原始序列在分布中的百分位
            row["orig_mdd"]             = orig["mdd"]
            row["orig_mdd_percentile"]  = float((m["mdd"] < orig["mdd"]).mean() * 100)  # 越小越差
            row["orig_terminal"]        = orig["terminal"]
            row["orig_term_percentile"] = float((m["terminal"] < orig["terminal"]).mean() * 100)
            stats_rows.append(row)

            # 破產機率
            ruin_row = {"strategy": strat, "method": method}
            for thr in MDD_THRESHOLDS:
                ruin_row[f"prob_mdd_gt_{int(thr*100)}pct"] = float((m["mdd"] < -thr).mean())
            ruin_rows.append(ruin_row)

            print(f"   {method:<10s}  "
                  f"MDD p5/p50/p95 = "
                  f"{np.percentile(m['mdd'],5)*100:6.2f}% / "
                  f"{np.percentile(m['mdd'],50)*100:6.2f}% / "
                  f"{np.percentile(m['mdd'],95)*100:6.2f}%   "
                  f"P(MDD>40%)={((m['mdd']< -0.40).mean()):.3f}   "
                  f"[{time.time()-t1:.1f}s]")

    # ── CSV 輸出 ──
    stats_df = pd.DataFrame(stats_rows)
    stats_path = OUT_DIR / f"mc_stats_{ts}.csv"
    stats_df.to_csv(stats_path, index=False, float_format="%.6f")

    ruin_df = pd.DataFrame(ruin_rows)
    ruin_path = OUT_DIR / f"mc_ruin_prob_{ts}.csv"
    ruin_df.to_csv(ruin_path, index=False, float_format="%.6f")

    # 樣本路徑（每個策略 × 方法 各存 N_SAMPLE 條 equity 路徑）
    sample_records = {}
    for strat in STRATEGIES:
        for method in METHODS:
            eq = equity_store[(strat, method)][:N_SAMPLE, :]  # (N_SAMPLE, n_days)
            for k in range(eq.shape[0]):
                col = f"{strat}__{method}__sim{k:03d}"
                sample_records[col] = eq[k]
    n_days = len(df)
    sample_df = pd.DataFrame(sample_records, index=df.index[:n_days])
    sample_path = OUT_DIR / f"mc_paths_sample_{ts}.csv"
    sample_df.to_csv(sample_path, float_format="%.6f")

    # ── 繪圖 ──
    print("\nGenerating figures...")
    for strat in STRATEGIES:
        r = df[strat].dropna().to_numpy()
        for method in METHODS:
            eq = equity_store[(strat, method)]
            title = f"Monte Carlo Fanchart — {strat} ({method}, n={N_SIM:,})"
            p = OUT_DIR / f"mc_fanchart_{strat}_{method}_{ts}.png"
            plot_fanchart(eq, r, title, p)

    plot_mdd_distribution(
        mdd_store,
        {s: orig_metrics_all[s]["mdd"] for s in STRATEGIES},
        OUT_DIR / f"mc_mdd_distribution_{ts}.png",
    )
    plot_terminal_wealth(
        term_store,
        {s: orig_metrics_all[s]["terminal"] for s in STRATEGIES},
        OUT_DIR / f"mc_terminal_wealth_{ts}.png",
    )
    plot_longest_streak(
        streak_store,
        {s: orig_metrics_all[s]["longest_loss_streak"] for s in STRATEGIES},
        OUT_DIR / f"mc_longest_losing_streak_{ts}.png",
    )

    # ── 終端摘要表 ──
    print("\n" + "=" * 70)
    print("KEY STATISTICS SUMMARY")
    print("=" * 70)
    hdr = (f"{'Strategy':<20} {'Method':<11} "
           f"{'MDD p5':>8} {'MDD p50':>8} {'MDD p95':>8} "
           f"{'P(MDD>40%)':>11} "
           f"{'Term p5':>9} {'Term p50':>9} {'Term p95':>9} "
           f"{'OrigPct':>8}")
    print(hdr)
    print("-" * len(hdr))
    for row in stats_rows:
        prob_ruin = next(
            rr[f"prob_mdd_gt_40pct"]
            for rr in ruin_rows
            if rr["strategy"] == row["strategy"] and rr["method"] == row["method"]
        )
        print(
            f"{row['strategy']:<20} {row['method']:<11} "
            f"{row['mdd_p05']*100:>7.2f}% {row['mdd_p50']*100:>7.2f}% {row['mdd_p95']*100:>7.2f}% "
            f"{prob_ruin:>10.3f} "
            f"{row['terminal_p05']*100:>8.1f}% {row['terminal_p50']*100:>8.1f}% {row['terminal_p95']*100:>8.1f}% "
            f"p{row['orig_mdd_percentile']:>5.1f}"
        )

    print("\nNote: reshuffle keeps multiset of daily returns → terminal wealth is identical across paths (math identity).")
    print("      Only path shape / MDD / streak varies. Bootstrap varies all metrics.\n")
    print("Original vs simulation percentile (higher MDD percentile = historically shallower drawdown than typical path):")
    for row in stats_rows:
        hint = "lucky" if row["orig_mdd_percentile"] > 95 else ("unlucky" if row["orig_mdd_percentile"] < 5 else "typical")
        print(f"  {row['strategy']:<20} {row['method']:<11}  "
              f"orig MDD={row['orig_mdd']*100:6.2f}%  @ p{row['orig_mdd_percentile']:5.1f}  [{hint}]")

    print("\n" + "=" * 70)
    print("OUTPUT FILES")
    print("=" * 70)
    print(f"CSV:")
    print(f"  {stats_path}")
    print(f"  {sample_path}")
    print(f"  {ruin_path}")
    print(f"PNG (in {OUT_DIR}):")
    for p in sorted(OUT_DIR.glob(f"*_{ts}.png")):
        print(f"  {p.name}")

    print(f"\nTotal elapsed: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
