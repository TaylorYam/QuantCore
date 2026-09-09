"""
QuantCore Robustness — 參數敏感度分析
====================================
對 SR 與 SP500 兩個策略的關鍵參數進行單因子掃描，
輸出每參數的 CAGR / Sharpe / MaxDD / Calmar 曲線與彙整報告。

輸出目錄：C:/QuantCore-master/robustness/output/sensitivity/
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
    run_sr_backtest,
    run_sp500_backtest,
    compute_metrics,
    setup_dark_theme,
    save_fig,
    OUTPUT_ROOT,
    timestamp,
    ACCENT,
    ACCENT_2,
    ACCENT_3,
    ACCENT_4,
    DARK_FG,
)

# ─────────────────────────────────────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────────────────────────────────────

OUT_DIR = OUTPUT_ROOT / "sensitivity"
OUT_DIR.mkdir(parents=True, exist_ok=True)

TS = timestamp()

# 基準參數
BASELINE_SR_LOOKBACKS = {60: 0.1, 20: 0.2, 10: 0.3, 5: 0.4}
BASELINE_SR_ZSCORE    = 0.4
BASELINE_SP_LOOKBACK  = 90
BASELINE_SP_HOLDINGS  = 4
BASELINE_SP_STOPLOSS  = 0.20
BASELINE_SP_EMA       = 50
BASELINE_SP_RANKBUF   = 20

# 基準週期 (60,20,10,5) 對應權重 (0.1,0.2,0.3,0.4)
BASE_PERIODS = [60, 20, 10, 5]
BASE_WEIGHTS = [0.1, 0.2, 0.3, 0.4]


def scale_lookbacks(scale: float) -> dict:
    """將基準週期按比例縮放，並確保整數、>0、不重複。"""
    scaled = [max(1, int(round(p * scale))) for p in BASE_PERIODS]
    # 修正重複：由小到大依序確保後者 > 前者（升序處理）
    srt_idx = np.argsort(scaled)
    unique_sorted = []
    used = set()
    for i in srt_idx:
        v = scaled[i]
        while v in used:
            v += 1
        used.add(v)
        unique_sorted.append((i, v))
    # 按原索引順序還原
    out_vals = [0] * len(scaled)
    for i, v in unique_sorted:
        out_vals[i] = v
    return dict(zip(out_vals, BASE_WEIGHTS))


# ─────────────────────────────────────────────────────────────────────────────
# 敏感度判定
# ─────────────────────────────────────────────────────────────────────────────

def classify_sensitivity(df: pd.DataFrame, baseline_value) -> tuple[str, float]:
    """
    在基準值週圍計算 CAGR 相對變動幅度。
    取基準點相鄰兩點的 CAGR，計算 (max-min)/|baseline_cagr|。
    回傳 (label, rel_change)。
    """
    df = df.sort_values("param_value").reset_index(drop=True)
    # 找基準 index
    if "is_baseline" in df.columns and df["is_baseline"].any():
        base_idx = df.index[df["is_baseline"]].tolist()[0]
    else:
        diffs = (df["param_value"] - baseline_value).abs()
        base_idx = int(diffs.idxmin())

    base_cagr = df.loc[base_idx, "ann_ret"]
    neigh_idx = [base_idx]
    if base_idx - 1 >= 0:
        neigh_idx.append(base_idx - 1)
    if base_idx + 1 < len(df):
        neigh_idx.append(base_idx + 1)

    cagrs = df.loc[neigh_idx, "ann_ret"].values
    rel_change = (cagrs.max() - cagrs.min()) / abs(base_cagr) if base_cagr != 0 else np.nan

    label = "High Sensitivity WARN" if rel_change > 0.30 else "Low Sensitivity OK"
    return label, rel_change


# ─────────────────────────────────────────────────────────────────────────────
# 繪圖
# ─────────────────────────────────────────────────────────────────────────────

def plot_param_curve(df: pd.DataFrame, baseline_value, param_name: str,
                     out_path: Path, xlabel: str = None):
    """2x2 子圖：CAGR / Sharpe / MaxDD / Calmar vs 參數值。"""
    df = df.sort_values("param_value").reset_index(drop=True)
    xlabel = xlabel or param_name

    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    metrics_spec = [
        ("ann_ret", "CAGR",    ACCENT,   "{:+.1%}"),
        ("sharpe",  "Sharpe",  ACCENT_3, "{:.2f}"),
        ("max_dd",  "Max DD",  ACCENT_2, "{:.1%}"),
        ("calmar",  "Calmar",  ACCENT_4, "{:.2f}"),
    ]
    for ax, (col, title, color, fmt) in zip(axes.ravel(), metrics_spec):
        ax.plot(df["param_value"], df[col], marker="o", color=color,
                linewidth=2, markersize=7)
        ax.axvline(baseline_value, color=ACCENT_2, linestyle="--",
                   linewidth=1.5, alpha=0.8)
        # 基準值標註
        ymin, ymax = ax.get_ylim()
        ax.text(baseline_value, ymax, f" baseline={baseline_value}",
                color=ACCENT_2, fontsize=9, va="top", ha="left")
        ax.set_title(f"{title} vs {xlabel}", fontsize=11)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(title)
        ax.grid(True, alpha=0.3)

    fig.suptitle(f"Parameter Sensitivity: {param_name}", fontsize=13,
                 color=DARK_FG, y=0.995)
    fig.tight_layout()
    save_fig(fig, out_path)


def plot_heatmap(summary: pd.DataFrame, out_path: Path):
    """彙整敏感度熱力圖：每個參數在不同偏離度下的 CAGR 變動。"""
    # 用相對於基準 CAGR 的變動百分點
    pivot_data = []
    param_order = summary["param_name"].drop_duplicates().tolist()

    for p in param_order:
        sub = summary[summary["param_name"] == p].sort_values("param_value").reset_index(drop=True)
        if not sub["is_baseline"].any():
            continue
        base_idx = int(sub.index[sub["is_baseline"]][0])
        base_cagr = sub.loc[base_idx, "ann_ret"]
        for i, row in sub.iterrows():
            offset = i - base_idx
            pivot_data.append({
                "param": p,
                "offset": offset,
                "delta_cagr_pp": (row["ann_ret"] - base_cagr) * 100,
            })

    pdf = pd.DataFrame(pivot_data)
    mat = pdf.pivot(index="param", columns="offset", values="delta_cagr_pp")
    mat = mat.reindex(param_order)

    fig, ax = plt.subplots(figsize=(10, max(4, 0.6 * len(mat) + 2)))
    vmax = np.nanmax(np.abs(mat.values))
    im = ax.imshow(mat.values, cmap="RdYlGn", aspect="auto",
                   vmin=-vmax, vmax=vmax)

    ax.set_xticks(range(len(mat.columns)))
    ax.set_xticklabels([f"{int(c):+d}" for c in mat.columns])
    ax.set_yticks(range(len(mat.index)))
    ax.set_yticklabels(mat.index)
    ax.set_xlabel("Offset from baseline (steps)")
    ax.set_title("Sensitivity Heatmap — ΔCAGR (percentage points) vs baseline",
                 fontsize=12)

    for i in range(mat.shape[0]):
        for j in range(mat.shape[1]):
            v = mat.values[i, j]
            if not np.isnan(v):
                ax.text(j, i, f"{v:+.1f}", ha="center", va="center",
                        fontsize=8, color="black")

    cbar = fig.colorbar(im, ax=ax, shrink=0.8)
    cbar.set_label("ΔCAGR (pp)", color=DARK_FG)
    cbar.ax.tick_params(colors=DARK_FG)

    fig.tight_layout()
    save_fig(fig, out_path)


# ─────────────────────────────────────────────────────────────────────────────
# 單因子掃描
# ─────────────────────────────────────────────────────────────────────────────

def run_one(runner_fn, label: str) -> dict:
    """跑一次回測並回傳指標 dict。"""
    t0 = time.time()
    ret = runner_fn()
    m = compute_metrics(ret)
    dt = time.time() - t0
    print(f"    {label:<28} -> CAGR {m['ann_ret']:+.2%} "
          f"Sharpe {m['sharpe']:.2f} MDD {m['max_dd']:.1%}  "
          f"({dt:.1f}s)")
    return m


def sweep_sr_zscore() -> pd.DataFrame:
    print("\n[1/7] SR: Z-score threshold")
    values = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]
    rows = []
    for v in values:
        m = run_one(
            lambda v=v: run_sr_backtest(zscore_thresh=v, quiet=True),
            f"zscore={v}",
        )
        rows.append({
            "param_name": "sr_zscore",
            "param_value": v,
            "is_baseline": abs(v - BASELINE_SR_ZSCORE) < 1e-9,
            "ann_ret": m["ann_ret"],
            "sharpe":  m["sharpe"],
            "max_dd":  m["max_dd"],
            "calmar":  m["calmar"],
        })
    return pd.DataFrame(rows)


def sweep_sr_lookback_scale() -> pd.DataFrame:
    print("\n[2/7] SR: Momentum lookback scale")
    scales = [0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3]
    rows = []
    for s in scales:
        lb = scale_lookbacks(s)
        desc = f"scale={s:.1f} {sorted(lb.keys(), reverse=True)}"
        m = run_one(
            lambda lb=lb: run_sr_backtest(mom_lookbacks=lb, quiet=True),
            desc,
        )
        rows.append({
            "param_name": "sr_lookback_scale",
            "param_value": s,
            "is_baseline": abs(s - 1.0) < 1e-9,
            "ann_ret": m["ann_ret"],
            "sharpe":  m["sharpe"],
            "max_dd":  m["max_dd"],
            "calmar":  m["calmar"],
        })
    return pd.DataFrame(rows)


def sweep_sp500_lookback() -> pd.DataFrame:
    print("\n[3/7] SP500: LOOKBACK")
    values = [60, 70, 80, 90, 100, 110, 120]
    rows = []
    for v in values:
        m = run_one(
            lambda v=v: run_sp500_backtest(lookback=v, quiet=True),
            f"lookback={v}",
        )
        rows.append({
            "param_name": "sp500_lookback",
            "param_value": v,
            "is_baseline": v == BASELINE_SP_LOOKBACK,
            "ann_ret": m["ann_ret"],
            "sharpe":  m["sharpe"],
            "max_dd":  m["max_dd"],
            "calmar":  m["calmar"],
        })
    return pd.DataFrame(rows)


def sweep_sp500_holdings() -> pd.DataFrame:
    print("\n[4/7] SP500: TARGET_HOLDINGS")
    values = [3, 4, 5, 6, 7, 8]
    rows = []
    for v in values:
        m = run_one(
            lambda v=v: run_sp500_backtest(target_holdings=v, quiet=True),
            f"holdings={v}",
        )
        rows.append({
            "param_name": "sp500_holdings",
            "param_value": v,
            "is_baseline": v == BASELINE_SP_HOLDINGS,
            "ann_ret": m["ann_ret"],
            "sharpe":  m["sharpe"],
            "max_dd":  m["max_dd"],
            "calmar":  m["calmar"],
        })
    return pd.DataFrame(rows)


def sweep_sp500_stop_loss() -> pd.DataFrame:
    print("\n[5/7] SP500: STOP_LOSS_PCT")
    values = [0.10, 0.14, 0.17, 0.20, 0.23, 0.27, 0.30]
    rows = []
    for v in values:
        m = run_one(
            lambda v=v: run_sp500_backtest(stop_loss_pct=v, quiet=True),
            f"stop_loss={v}",
        )
        rows.append({
            "param_name": "sp500_stop_loss",
            "param_value": v,
            "is_baseline": abs(v - BASELINE_SP_STOPLOSS) < 1e-9,
            "ann_ret": m["ann_ret"],
            "sharpe":  m["sharpe"],
            "max_dd":  m["max_dd"],
            "calmar":  m["calmar"],
        })
    return pd.DataFrame(rows)


def sweep_sp500_ema() -> pd.DataFrame:
    print("\n[6/7] SP500: EXIT_EMA")
    values = [30, 40, 45, 50, 55, 60, 70]
    rows = []
    for v in values:
        m = run_one(
            lambda v=v: run_sp500_backtest(exit_ema=v, quiet=True),
            f"exit_ema={v}",
        )
        rows.append({
            "param_name": "sp500_ema",
            "param_value": v,
            "is_baseline": v == BASELINE_SP_EMA,
            "ann_ret": m["ann_ret"],
            "sharpe":  m["sharpe"],
            "max_dd":  m["max_dd"],
            "calmar":  m["calmar"],
        })
    return pd.DataFrame(rows)


def sweep_sp500_rank_buffer() -> pd.DataFrame:
    print("\n[7/7] SP500: SELL_RANK_THRESHOLD (Rank Buffer)")
    values = [10, 15, 20, 25, 30]
    rows = []
    for v in values:
        m = run_one(
            lambda v=v: run_sp500_backtest(sell_rank_threshold=v, quiet=True),
            f"rank_buffer={v}",
        )
        rows.append({
            "param_name": "sp500_rank_buffer",
            "param_value": v,
            "is_baseline": v == BASELINE_SP_RANKBUF,
            "ann_ret": m["ann_ret"],
            "sharpe":  m["sharpe"],
            "max_dd":  m["max_dd"],
            "calmar":  m["calmar"],
        })
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────────────

def main():
    setup_dark_theme()

    start_wall = time.time()
    print("=" * 72)
    print("QuantCore Robustness — 參數敏感度分析")
    print("=" * 72)
    print(f"輸出目錄: {OUT_DIR}")
    print(f"時間戳  : {TS}")
    print(f"預計 SP500 回測次數: 7+6+7+7+5 = 32 次 (~30-60 分鐘)")
    print(f"         SR 回測次數: 7+7 = 14 次 (~1-2 分鐘)")

    # 依序掃描
    sweeps = [
        ("sr_zscore",           BASELINE_SR_ZSCORE, "Z-score threshold",      sweep_sr_zscore),
        ("sr_lookback_scale",   1.0,                "Lookback scale",          sweep_sr_lookback_scale),
        ("sp500_lookback",      BASELINE_SP_LOOKBACK,"LOOKBACK (days)",        sweep_sp500_lookback),
        ("sp500_holdings",      BASELINE_SP_HOLDINGS,"TARGET_HOLDINGS",        sweep_sp500_holdings),
        ("sp500_stop_loss",     BASELINE_SP_STOPLOSS,"STOP_LOSS_PCT",          sweep_sp500_stop_loss),
        ("sp500_ema",           BASELINE_SP_EMA,     "EXIT_EMA",               sweep_sp500_ema),
        ("sp500_rank_buffer",   BASELINE_SP_RANKBUF, "SELL_RANK_THRESHOLD",    sweep_sp500_rank_buffer),
    ]

    all_dfs = []
    sensitivity_results = []

    for param_key, baseline_val, xlabel, fn in sweeps:
        df = fn()
        df.to_csv(OUT_DIR / f"{param_key}_{TS}.csv", index=False)

        # 繪圖
        plot_param_curve(
            df, baseline_val, param_key,
            OUT_DIR / f"{param_key}_curve_{TS}.png",
            xlabel=xlabel,
        )

        # 敏感度判定
        label, rel_chg = classify_sensitivity(df, baseline_val)
        sensitivity_results.append({
            "param_name": param_key,
            "baseline_value": baseline_val,
            "rel_cagr_change_near_baseline": rel_chg,
            "classification": label,
        })
        print(f"  >> {param_key}: rel ΔCAGR near baseline = {rel_chg:.1%} → {label}")

        all_dfs.append(df)

    # 彙整
    summary = pd.concat(all_dfs, ignore_index=True)

    # delta_cagr_pct 欄位：相對同組 baseline CAGR 的變動 %
    def add_delta(grp):
        base = grp.loc[grp["is_baseline"], "ann_ret"]
        base_val = base.iloc[0] if len(base) else np.nan
        grp["delta_cagr_pct"] = (grp["ann_ret"] - base_val) / abs(base_val) * 100 \
            if base_val and base_val != 0 else np.nan
        return grp

    summary = summary.groupby("param_name", group_keys=False).apply(add_delta)
    summary_cls = pd.DataFrame(sensitivity_results).set_index("param_name")
    summary["classification"] = summary["param_name"].map(summary_cls["classification"])

    summary.to_csv(OUT_DIR / f"sensitivity_summary_{TS}.csv", index=False)

    # 熱力圖
    plot_heatmap(summary, OUT_DIR / f"sensitivity_heatmap_{TS}.png")

    # 最終報告
    elapsed = time.time() - start_wall
    print("\n" + "=" * 72)
    print("敏感度分析完成")
    print("=" * 72)
    print(f"{'參數':<26} {'基準':>10}  {'ΔCAGR 鄰近':>12}  判定")
    print("-" * 72)
    for r in sensitivity_results:
        print(f"{r['param_name']:<26} {str(r['baseline_value']):>10}  "
              f"{r['rel_cagr_change_near_baseline']:>11.1%}   {r['classification']}")
    print("-" * 72)
    print(f"總執行時間: {elapsed/60:.1f} 分鐘")
    print(f"輸出目錄  : {OUT_DIR}")
    print("=" * 72)


if __name__ == "__main__":
    main()
