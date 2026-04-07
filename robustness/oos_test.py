"""
QuantCore Robustness — Out-of-Sample (OOS) Test
================================================
樣本外測試：
  1. Simple Split — 前 70% IS / 後 30% OOS
  2. Walk-Forward Analysis — 滾動 4 年訓練 / 1 年 OOS

輸入：robustness/output/baseline_returns.csv
輸出：robustness/output/oos/
  - simple_split_{ts}.csv
  - walkforward_{ts}.csv
  - is_vs_oos_bars_{ts}.png
  - walkforward_performance_{ts}.png
"""

from __future__ import annotations

import sys
import io
from pathlib import Path

sys.path.insert(0, str(Path("C:/QuantCore-master")))

# Windows cp950 console compatibility
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from robustness.utils import (
    setup_dark_theme,
    save_fig,
    compute_metrics,
    OUTPUT_ROOT,
    timestamp,
    ACCENT, ACCENT_2, ACCENT_3, ACCENT_4,
)

STRATEGIES = ["SR", "SP500", "Combo_80_50_1.3x"]
STRAT_LABEL = {
    "SR":                "Sector Rotation",
    "SP500":             "SP500 Momentum",
    "Combo_80_50_1.3x":  "Combo (80/50, 1.3x)",
}
STRAT_COLORS = {
    "SR":                ACCENT,
    "SP500":             ACCENT_3,
    "Combo_80_50_1.3x":  ACCENT_4,
}

OOS_DIR = OUTPUT_ROOT / "oos"
OOS_DIR.mkdir(parents=True, exist_ok=True)


def _slice_metrics(ret: pd.Series) -> dict:
    m = compute_metrics(ret)
    return {
        "CAGR":   m.get("ann_ret", np.nan),
        "Sharpe": m.get("sharpe",  np.nan),
        "MDD":    m.get("max_dd",  np.nan),
        "Calmar": m.get("calmar",  np.nan),
    }


def simple_split_test(df: pd.DataFrame, split_ratio: float = 0.7) -> pd.DataFrame:
    """前 split_ratio IS / 後 (1-split_ratio) OOS。"""
    n       = len(df)
    cut     = int(n * split_ratio)
    df_is   = df.iloc[:cut]
    df_oos  = df.iloc[cut:]

    print(f"\n[Simple Split {split_ratio:.0%} / {1-split_ratio:.0%}]")
    print(f"  IS : {df_is.index[0].date()} → {df_is.index[-1].date()}  ({len(df_is)} 天)")
    print(f"  OOS: {df_oos.index[0].date()} → {df_oos.index[-1].date()}  ({len(df_oos)} 天)")

    rows = []
    for col in STRATEGIES + ["SPY"]:
        m_is  = _slice_metrics(df_is[col])
        m_oos = _slice_metrics(df_oos[col])
        degrade_cagr = (m_is["CAGR"] - m_oos["CAGR"]) / abs(m_is["CAGR"]) if m_is["CAGR"] != 0 else np.nan
        rows.append({
            "Strategy":     col,
            "IS_CAGR":      m_is["CAGR"],
            "IS_Sharpe":    m_is["Sharpe"],
            "IS_MDD":       m_is["MDD"],
            "IS_Calmar":    m_is["Calmar"],
            "OOS_CAGR":     m_oos["CAGR"],
            "OOS_Sharpe":   m_oos["Sharpe"],
            "OOS_MDD":      m_oos["MDD"],
            "OOS_Calmar":   m_oos["Calmar"],
            "degradation":  degrade_cagr,
        })
    out = pd.DataFrame(rows).set_index("Strategy")
    return out


def walkforward_test(
    df: pd.DataFrame,
    train_years: int = 4,
    oos_years: int = 1,
    step_years: int = 1,
) -> pd.DataFrame:
    """滾動窗口 walk-forward，只看 OOS 段績效。"""
    print(f"\n[Walk-Forward] train={train_years}y / oos={oos_years}y / step={step_years}y")

    start = df.index[0]
    end   = df.index[-1]

    rows = []
    train_start = start
    while True:
        train_end = train_start + pd.DateOffset(years=train_years)
        oos_start = train_end
        oos_end   = oos_start + pd.DateOffset(years=oos_years)
        if oos_end > end + pd.Timedelta(days=1):
            # 允許最後一段不滿，只要 OOS 有資料就跑
            if oos_start >= end:
                break
            oos_end = end + pd.Timedelta(days=1)

        df_train = df[(df.index >= train_start) & (df.index < train_end)]
        df_oos   = df[(df.index >= oos_start)   & (df.index < oos_end)]

        if len(df_oos) < 20:
            break

        oos_label = f"{oos_start.date()}→{min(oos_end, end).date()}"

        row = {
            "train_start": train_start.date().isoformat(),
            "train_end":   train_end.date().isoformat(),
            "oos_start":   oos_start.date().isoformat(),
            "oos_end":     min(oos_end, end).date().isoformat(),
            "oos_days":    len(df_oos),
            "oos_year":    oos_start.year,
        }
        for col in STRATEGIES:
            m_oos   = _slice_metrics(df_oos[col])
            m_train = _slice_metrics(df_train[col])
            row[f"{col}_OOS_CAGR"]   = m_oos["CAGR"]
            row[f"{col}_OOS_Sharpe"] = m_oos["Sharpe"]
            row[f"{col}_OOS_MDD"]    = m_oos["MDD"]
            row[f"{col}_IS_CAGR"]    = m_train["CAGR"]
            row[f"{col}_IS_Sharpe"]  = m_train["Sharpe"]
        rows.append(row)

        print(f"  OOS {oos_label}: "
              + "  ".join(
                  f"{col}={row[f'{col}_OOS_CAGR']:+.1%}"
                  for col in STRATEGIES
              ))

        train_start = train_start + pd.DateOffset(years=step_years)
        # 停止條件：下一個 OOS 起點超過結束日
        next_oos_start = train_start + pd.DateOffset(years=train_years)
        if next_oos_start >= end:
            break

    return pd.DataFrame(rows)


def plot_is_vs_oos(split_df: pd.DataFrame, out_path: Path):
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    metrics = [("CAGR", "CAGR"), ("Sharpe", "Sharpe"), ("MDD", "Max Drawdown")]

    strategies = STRATEGIES  # ignore SPY in bar chart, but include as ref
    x   = np.arange(len(strategies))
    width = 0.35

    for ax, (mkey, mname) in zip(axes, metrics):
        is_vals  = [split_df.loc[s, f"IS_{mkey}"]  for s in strategies]
        oos_vals = [split_df.loc[s, f"OOS_{mkey}"] for s in strategies]

        ax.bar(x - width/2, is_vals,  width, label="IS (70%)",  color=ACCENT,   alpha=0.85)
        ax.bar(x + width/2, oos_vals, width, label="OOS (30%)", color=ACCENT_2, alpha=0.85)

        ax.set_title(mname, fontsize=12, fontweight="bold")
        ax.set_xticks(x)
        ax.set_xticklabels([STRAT_LABEL[s] for s in strategies], rotation=15, ha="right", fontsize=9)
        ax.axhline(0, color="#888", linewidth=0.8)
        ax.legend(loc="best", framealpha=0.3)

        # 格式化 y 軸
        if mkey in ("CAGR", "MDD"):
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))

    fig.suptitle("In-Sample vs Out-of-Sample (70/30 Split)", fontsize=14, fontweight="bold")
    fig.tight_layout()
    save_fig(fig, out_path)
    print(f"  ↳ saved: {out_path.name}")


def plot_walkforward(wf_df: pd.DataFrame, out_path: Path):
    fig, axes = plt.subplots(2, 1, figsize=(13, 9), sharex=True)

    x_labels = [f"{y}" for y in wf_df["oos_year"]]
    x = np.arange(len(wf_df))

    # 上：OOS CAGR（柱狀圖，三策略並排）
    ax = axes[0]
    n_strat = len(STRATEGIES)
    bar_w = 0.8 / n_strat
    for i, col in enumerate(STRATEGIES):
        vals = wf_df[f"{col}_OOS_CAGR"].values
        ax.bar(x + (i - n_strat/2 + 0.5) * bar_w, vals, bar_w,
               label=STRAT_LABEL[col], color=STRAT_COLORS[col], alpha=0.85)
    ax.axhline(0, color="#888", linewidth=0.8)
    ax.set_ylabel("OOS CAGR")
    ax.set_title("Walk-Forward: Out-of-Sample CAGR by Year",
                 fontsize=13, fontweight="bold")
    ax.legend(loc="best", framealpha=0.3)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))

    # 下：OOS Sharpe（折線）
    ax = axes[1]
    for col in STRATEGIES:
        ax.plot(x, wf_df[f"{col}_OOS_Sharpe"].values,
                marker="o", linewidth=2, markersize=7,
                label=STRAT_LABEL[col], color=STRAT_COLORS[col])
    ax.axhline(0, color="#888", linewidth=0.8)
    ax.axhline(1, color="#888", linewidth=0.5, linestyle="--", alpha=0.5)
    ax.set_ylabel("OOS Sharpe")
    ax.set_xlabel("OOS Window (starting year)")
    ax.set_title("Walk-Forward: Out-of-Sample Sharpe Ratio",
                 fontsize=13, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels(x_labels)
    ax.legend(loc="best", framealpha=0.3)

    fig.tight_layout()
    save_fig(fig, out_path)
    print(f"  ↳ saved: {out_path.name}")


def print_wf_summary(wf_df: pd.DataFrame):
    print("\n[Walk-Forward Summary] — 各策略所有 OOS 段")
    print("-" * 72)
    hdr = f"{'Strategy':<22} {'Mean':>8} {'Median':>8} {'Min':>8} {'Max':>8} {'Std':>8}"
    print(f"CAGR:  {hdr[9:]}")
    for col in STRATEGIES:
        v = wf_df[f"{col}_OOS_CAGR"]
        print(f"  {STRAT_LABEL[col]:<20} "
              f"{v.mean():>7.1%} {v.median():>7.1%} {v.min():>7.1%} {v.max():>7.1%} {v.std():>7.1%}")
    print()
    print(f"Sharpe:{hdr[9:]}")
    for col in STRATEGIES:
        v = wf_df[f"{col}_OOS_Sharpe"]
        print(f"  {STRAT_LABEL[col]:<20} "
              f"{v.mean():>8.2f} {v.median():>8.2f} {v.min():>8.2f} {v.max():>8.2f} {v.std():>8.2f}")
    print()
    print(f"MaxDD: {hdr[9:]}")
    for col in STRATEGIES:
        v = wf_df[f"{col}_OOS_MDD"]
        print(f"  {STRAT_LABEL[col]:<20} "
              f"{v.mean():>7.1%} {v.median():>7.1%} {v.min():>7.1%} {v.max():>7.1%} {v.std():>7.1%}")


def print_split_conclusion(split_df: pd.DataFrame):
    print("\n[Simple Split Results]")
    print("-" * 72)
    print(f"{'Strategy':<22} {'IS CAGR':>9} {'OOS CAGR':>9} {'IS Shrp':>8} {'OOS Shrp':>9} {'Degrad':>8}")
    print("-" * 72)
    for s in STRATEGIES + ["SPY"]:
        row = split_df.loc[s]
        print(f"  {STRAT_LABEL.get(s, s):<20} "
              f"{row['IS_CAGR']:>+8.1%} {row['OOS_CAGR']:>+8.1%} "
              f"{row['IS_Sharpe']:>8.2f} {row['OOS_Sharpe']:>8.2f} "
              f"{row['degradation']:>+7.1%}")

    print("\n[Overfitting Verdict]")
    for s in STRATEGIES:
        d = split_df.loc[s, "degradation"]
        if pd.isna(d):
            verdict = "N/A"
        elif d > 0.5:
            verdict = "HIGH DEGRADATION (overfitting suspected)"
        elif d > 0.2:
            verdict = "Moderate degradation"
        elif d < -0.1:
            verdict = "OOS outperformed IS (no overfitting)"
        else:
            verdict = "Stable"
        print(f"  {STRAT_LABEL[s]:<22} degradation={d:>+6.1%}  → {verdict}")


def main():
    setup_dark_theme()

    baseline_path = OUTPUT_ROOT / "baseline_returns.csv"
    print("=" * 72)
    print("QuantCore — Out-of-Sample (OOS) Test")
    print("=" * 72)
    print(f"Loading baseline returns: {baseline_path}")
    df = pd.read_csv(baseline_path, index_col="Date", parse_dates=True)
    print(f"  Shape: {df.shape}  Range: {df.index[0].date()} → {df.index[-1].date()}")
    print(f"  Columns: {list(df.columns)}")

    ts = timestamp()

    # ── Part 1: Simple Split ──────────────────────────────────────────────────
    split_df = simple_split_test(df, split_ratio=0.7)
    split_csv = OOS_DIR / f"simple_split_{ts}.csv"
    split_df.to_csv(split_csv, float_format="%.6f")
    print(f"  ↳ saved: {split_csv.name}")
    print_split_conclusion(split_df)

    # ── Part 2: Walk-Forward ──────────────────────────────────────────────────
    wf_df = walkforward_test(df, train_years=4, oos_years=1, step_years=1)
    wf_csv = OOS_DIR / f"walkforward_{ts}.csv"
    wf_df.to_csv(wf_csv, index=False, float_format="%.6f")
    print(f"\n  ↳ saved: {wf_csv.name}")
    print_wf_summary(wf_df)

    # ── Plots ─────────────────────────────────────────────────────────────────
    print("\n[Plotting]")
    plot_is_vs_oos(split_df, OOS_DIR / f"is_vs_oos_bars_{ts}.png")
    plot_walkforward(wf_df, OOS_DIR / f"walkforward_performance_{ts}.png")

    print("\n" + "=" * 72)
    print("OOS test complete.")
    print(f"Output dir: {OOS_DIR}")
    print("=" * 72)

    # ── 關鍵結論 ──────────────────────────────────────────────────────────────
    print("\n[KEY CONCLUSIONS]")
    for s in STRATEGIES:
        d_simple = split_df.loc[s, "degradation"]
        wf_cagr  = wf_df[f"{s}_OOS_CAGR"]
        neg_pct  = (wf_cagr < 0).mean()
        flag = "⚠️ 高退化" if (not pd.isna(d_simple) and d_simple > 0.5) else "OK"
        print(f"  {STRAT_LABEL[s]:<22} "
              f"split_degrad={d_simple:+.1%}  "
              f"WF_avg_CAGR={wf_cagr.mean():+.1%}  "
              f"WF_neg_years={neg_pct:.0%}  "
              f"[{flag}]")


if __name__ == "__main__":
    main()
