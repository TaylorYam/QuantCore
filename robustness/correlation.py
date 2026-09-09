"""
QuantCore Robustness — 雙策略相關性與分散效果分析

分析項目：
  1. 滾動相關係數（60 日）
  2. 壓力時段條件相關性（regime-dependent）
  3. 報酬歸因與 diversification return
  4. 動量因子相關性（long-short sector momentum proxy）
  5. 不同槓桿曝險水準的風險報酬比較

輸入：robustness/output/baseline_returns.csv
輸出：robustness/output/correlation/
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from robustness.utils import (
    compute_metrics,
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


OUT_DIR = OUTPUT_ROOT / "correlation"
OUT_DIR.mkdir(parents=True, exist_ok=True)

ROLL_WIN = 60
STRESS_THRESH = -0.05  # SPY 60 日累積 < -5%
MA_WIN = 200


# ── 1. 載入基準報酬 ───────────────────────────────────────────────────────────

def load_baseline() -> pd.DataFrame:
    path = OUTPUT_ROOT / "baseline_returns.csv"
    df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
    # 欄位：SR, SP500, Combo_80_50_1.3x, SPY
    return df


# ── 2. 滾動相關係數 ───────────────────────────────────────────────────────────

def rolling_correlations(df: pd.DataFrame, win: int = ROLL_WIN) -> pd.DataFrame:
    ew = 0.5 * df["SR"] + 0.5 * df["SP500"]
    rc = pd.DataFrame(index=df.index)
    rc[f"SR_vs_SP500"]      = df["SR"].rolling(win).corr(df["SP500"])
    rc[f"SR_vs_SPY"]        = df["SR"].rolling(win).corr(df["SPY"])
    rc[f"SP500_vs_SPY"]     = df["SP500"].rolling(win).corr(df["SPY"])
    rc[f"EW_Combo_vs_SPY"]  = ew.rolling(win).corr(df["SPY"])
    return rc.dropna()


def summarize_rolling(rc: pd.DataFrame) -> pd.DataFrame:
    summary = pd.DataFrame({
        "mean":   rc.mean(),
        "median": rc.median(),
        "min":    rc.min(),
        "max":    rc.max(),
        "std":    rc.std(),
    })
    return summary


# ── 3. 條件相關性 ────────────────────────────────────────────────────────────

def classify_regimes(df: pd.DataFrame) -> pd.Series:
    """Classify each day as bull / bear / sideways based on SPY."""
    spy_cum = (1 + df["SPY"]).cumprod()
    # 用 SPY price proxy（log-cumulative）計算 MA200
    ma200 = spy_cum.rolling(MA_WIN).mean()
    roll60 = df["SPY"].rolling(60).apply(lambda x: (1 + x).prod() - 1, raw=False)

    regime = pd.Series(index=df.index, dtype=object)
    regime[(spy_cum > ma200) & (roll60 > 0)] = "bull"
    regime[spy_cum < ma200] = "bear"
    regime = regime.fillna("sideways")
    return regime


def stress_periods(df: pd.DataFrame) -> pd.Series:
    roll60 = df["SPY"].rolling(60).apply(lambda x: (1 + x).prod() - 1, raw=False)
    return roll60 < STRESS_THRESH


def conditional_correlations(df: pd.DataFrame) -> pd.DataFrame:
    regime = classify_regimes(df)
    stress = stress_periods(df)

    pairs = [
        ("SR", "SP500"),
        ("SR", "SPY"),
        ("SP500", "SPY"),
    ]

    rows = []
    # Stress / non-stress
    mask_s = stress.fillna(False)
    for a, b in pairs:
        sub_s  = df.loc[mask_s, [a, b]].dropna()
        sub_ns = df.loc[~mask_s, [a, b]].dropna()
        rows.append({
            "condition": "stress (SPY 60d < -5%)",
            "pair": f"{a}_vs_{b}",
            "corr": sub_s[a].corr(sub_s[b]) if len(sub_s) > 1 else np.nan,
            "n_days": len(sub_s),
        })
        rows.append({
            "condition": "non_stress",
            "pair": f"{a}_vs_{b}",
            "corr": sub_ns[a].corr(sub_ns[b]) if len(sub_ns) > 1 else np.nan,
            "n_days": len(sub_ns),
        })

    # Regime-based
    for reg in ["bull", "bear", "sideways"]:
        mask = regime == reg
        for a, b in pairs:
            sub = df.loc[mask, [a, b]].dropna()
            rows.append({
                "condition": reg,
                "pair": f"{a}_vs_{b}",
                "corr": sub[a].corr(sub[b]) if len(sub) > 1 else np.nan,
                "n_days": len(sub),
            })

    return pd.DataFrame(rows)


# ── 4. 報酬歸因 ───────────────────────────────────────────────────────────────

def return_attribution(df: pd.DataFrame) -> pd.DataFrame:
    sr  = df["SR"].dropna()
    sp  = df["SP500"].dropna()
    common = sr.index.intersection(sp.index)
    sr, sp = sr.loc[common], sp.loc[common]

    # 50/50 等權（無槓桿）
    ew = 0.5 * sr + 0.5 * sp

    ann = 252
    sr_mu_ann  = (1 + sr).prod() ** (ann / len(sr)) - 1
    sp_mu_ann  = (1 + sp).prod() ** (ann / len(sp)) - 1
    ew_mu_ann  = (1 + ew).prod() ** (ann / len(ew)) - 1

    sr_vol = sr.std() * np.sqrt(ann)
    sp_vol = sp.std() * np.sqrt(ann)
    ew_vol = ew.std() * np.sqrt(ann)

    weighted_vol = np.sqrt(0.25 * sr_vol**2 + 0.25 * sp_vol**2)
    vol_reduction = weighted_vol - ew_vol

    # Diversification return: 0.5*σ_sr² + 0.5*σ_sp² - σ_port² (annualized variance)
    var_diff = 0.5 * sr.var() + 0.5 * sp.var() - ew.var()
    div_return = var_diff * ann / 2  # 常用近似：年化的 0.5 × var_diff

    # 另一種估法：實際 - 算數加權
    weighted_mean = 0.5 * sr_mu_ann + 0.5 * sp_mu_ann
    compound_bonus = ew_mu_ann - weighted_mean

    rows = [
        {"metric": "SR annualized return",        "value": sr_mu_ann},
        {"metric": "SP500 annualized return",     "value": sp_mu_ann},
        {"metric": "EW 50/50 annualized return",  "value": ew_mu_ann},
        {"metric": "SR contribution (0.5*SR)",    "value": 0.5 * sr_mu_ann},
        {"metric": "SP500 contribution (0.5*SP)", "value": 0.5 * sp_mu_ann},
        {"metric": "Weighted avg return",         "value": weighted_mean},
        {"metric": "Geometric bonus (EW - weighted avg)", "value": compound_bonus},
        {"metric": "SR annualized vol",           "value": sr_vol},
        {"metric": "SP500 annualized vol",        "value": sp_vol},
        {"metric": "EW annualized vol",           "value": ew_vol},
        {"metric": "Weighted vol (no diversif.)", "value": weighted_vol},
        {"metric": "Volatility reduction",        "value": vol_reduction},
        {"metric": "Diversification return (var approx)", "value": div_return},
        {"metric": "Correlation SR vs SP500",     "value": sr.corr(sp)},
    ]
    return pd.DataFrame(rows)


# ── 5. 動量因子相關性（long-short sector proxy） ─────────────────────────────

SECTOR_TICKERS = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]


def build_momentum_factor(df_index: pd.DatetimeIndex) -> pd.Series | None:
    """
    嘗試從 data/etfs/ 構造一個 long-short sector momentum factor。
    前 3 名 60 日動量 - 後 3 名，每日報酬。
    """
    etf_dir = Path(__file__).resolve().parent.parent / "data" / "etfs"
    if not etf_dir.exists():
        return None

    prices = {}
    for t in SECTOR_TICKERS:
        fp = etf_dir / f"{t}.csv"
        if fp.exists():
            try:
                d = pd.read_csv(fp, parse_dates=["Date"], index_col="Date")
                col = "Adj Close" if "Adj Close" in d.columns else ("Close" if "Close" in d.columns else d.columns[-1])
                prices[t] = d[col]
            except Exception:
                continue
    if len(prices) < 6:
        return None

    px = pd.DataFrame(prices).sort_index().dropna(how="all")
    rets = px.pct_change()
    mom = px.pct_change(60)  # 60日動量

    long_short = []
    idx = []
    for dt in rets.index:
        if dt not in mom.index:
            continue
        m_row = mom.loc[dt].dropna()
        r_row = rets.loc[dt].dropna()
        if len(m_row) < 6:
            continue
        top = m_row.nlargest(3).index
        bot = m_row.nsmallest(3).index
        top_r = r_row.reindex(top).mean()
        bot_r = r_row.reindex(bot).mean()
        long_short.append(top_r - bot_r)
        idx.append(dt)
    ls = pd.Series(long_short, index=pd.DatetimeIndex(idx), name="MomFactor")
    return ls.reindex(df_index).dropna()


def momentum_factor_correlations(df: pd.DataFrame) -> pd.DataFrame:
    mom = build_momentum_factor(df.index)
    label = "sector_long_short"
    if mom is None or len(mom) < 60:
        # Fallback：用 SR 策略自己當代理
        mom = df["SR"].copy()
        mom.name = "MomFactor"
        label = "self_proxy_SR"

    common = df.index.intersection(mom.index)
    sub = df.loc[common].copy()
    sub["Mom"] = mom.loc[common]
    sub = sub.dropna()

    rows = []
    for col in ["SR", "SP500", "SPY"]:
        x = sub["Mom"].values
        y = sub[col].values
        # Beta = cov / var
        beta = np.cov(y, x, ddof=1)[0, 1] / np.var(x, ddof=1)
        corr = np.corrcoef(y, x)[0, 1]
        rows.append({
            "series": col,
            "mom_proxy": label,
            "correlation": corr,
            "beta": beta,
            "n_days": len(sub),
        })
    return pd.DataFrame(rows)


# ── 6. 槓桿曝險比較 ───────────────────────────────────────────────────────────

def leverage_comparison(df: pd.DataFrame) -> pd.DataFrame:
    configs = [
        (1.0, 0.0),  # 100% SR
        (0.5, 0.5),  # 50/50
        (0.8, 0.5),  # baseline 1.3x
        (1.0, 0.5),  # 1.5x
        (1.0, 0.8),  # 1.8x
    ]
    sr = df["SR"].dropna()
    sp = df["SP500"].dropna()
    common = sr.index.intersection(sp.index)
    sr, sp = sr.loc[common], sp.loc[common]

    rows = []
    for a_sr, a_sp in configs:
        ret = a_sr * sr + a_sp * sp
        lev = a_sr + a_sp
        m = compute_metrics(ret)
        rows.append({
            "alloc_sr": a_sr,
            "alloc_sp": a_sp,
            "leverage": lev,
            "CAGR": m.get("ann_ret"),
            "ann_vol": m.get("ann_vol"),
            "sharpe": m.get("sharpe"),
            "sortino": m.get("sortino"),
            "max_dd": m.get("max_dd"),
            "calmar": m.get("calmar"),
        })
    return pd.DataFrame(rows)


# ── 7. 繪圖 ──────────────────────────────────────────────────────────────────

def plot_rolling_corr(rc: pd.DataFrame, path: Path):
    fig, ax = plt.subplots(figsize=(12, 6))
    colors = [ACCENT, ACCENT_2, ACCENT_3, ACCENT_4]
    for i, col in enumerate(rc.columns):
        ax.plot(rc.index, rc[col], label=col, color=colors[i % len(colors)],
                linewidth=1.2, alpha=0.85)
    ax.axhline(0, color=DARK_FG, linestyle="--", linewidth=0.8, alpha=0.5)
    ax.set_title(f"60-Day Rolling Correlations", fontsize=13, weight="bold")
    ax.set_ylabel("Correlation")
    ax.set_xlabel("Date")
    ax.legend(loc="upper left", frameon=True, facecolor=DARK_BG, edgecolor=DARK_FG)
    ax.set_ylim(-1.05, 1.05)
    save_fig(fig, path)


def plot_conditional_heatmap(cc: pd.DataFrame, path: Path):
    pivot = cc.pivot(index="condition", columns="pair", values="corr")
    # 排序
    cond_order = ["bull", "sideways", "bear", "stress (SPY 60d < -5%)", "non_stress"]
    pivot = pivot.reindex([c for c in cond_order if c in pivot.index])

    fig, ax = plt.subplots(figsize=(9, 5.5))
    data = pivot.values
    im = ax.imshow(data, cmap="RdYlBu_r", vmin=-1, vmax=1, aspect="auto")
    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels(pivot.columns, rotation=30, ha="right")
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index)
    for i in range(data.shape[0]):
        for j in range(data.shape[1]):
            v = data[i, j]
            if np.isnan(v):
                continue
            ax.text(j, i, f"{v:.2f}", ha="center", va="center",
                    color="white" if abs(v) > 0.4 else DARK_FG, fontsize=10, weight="bold")
    cbar = fig.colorbar(im, ax=ax, shrink=0.8)
    cbar.set_label("Correlation", color=DARK_FG)
    cbar.ax.yaxis.set_tick_params(color=DARK_FG)
    plt.setp(plt.getp(cbar.ax.axes, "yticklabels"), color=DARK_FG)
    ax.set_title("Conditional Correlations by Regime", fontsize=13, weight="bold")
    ax.grid(False)
    save_fig(fig, path)


def plot_leverage_scatter(lev: pd.DataFrame, path: Path):
    fig, ax = plt.subplots(figsize=(9, 6.5))
    for _, row in lev.iterrows():
        ax.scatter(row["ann_vol"], row["CAGR"], s=180 + 80 * row["leverage"],
                   color=ACCENT, alpha=0.75, edgecolors=DARK_FG, linewidth=1.2)
        label = f"SR={row['alloc_sr']:.1f} SP={row['alloc_sp']:.1f} ({row['leverage']:.1f}x)\nSharpe={row['sharpe']:.2f}"
        ax.annotate(label, (row["ann_vol"], row["CAGR"]),
                    xytext=(8, 8), textcoords="offset points", fontsize=9, color=DARK_FG)
    ax.set_xlabel("Annualized Volatility")
    ax.set_ylabel("CAGR")
    ax.set_title("Risk-Return by Leverage Configuration", fontsize=13, weight="bold")
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x*100:.0f}%"))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x*100:.0f}%"))
    save_fig(fig, path)


def plot_sr_sp_scatter(df: pd.DataFrame, path: Path):
    sub = df[["SR", "SP500"]].dropna()
    x = sub["SR"].values
    y = sub["SP500"].values
    corr = np.corrcoef(x, y)[0, 1]
    # regression line
    slope, intercept = np.polyfit(x, y, 1)

    fig, ax = plt.subplots(figsize=(8, 7))
    ax.scatter(x, y, s=8, color=ACCENT, alpha=0.35, edgecolors="none")
    xs = np.linspace(x.min(), x.max(), 100)
    ax.plot(xs, slope * xs + intercept, color=ACCENT_2, linewidth=2,
            label=f"y = {slope:.3f}x + {intercept:.5f}")
    ax.axhline(0, color=DARK_FG, linewidth=0.6, alpha=0.4)
    ax.axvline(0, color=DARK_FG, linewidth=0.6, alpha=0.4)
    ax.set_xlabel("SR Daily Return")
    ax.set_ylabel("SP500 Daily Return")
    ax.set_title(f"SR vs SP500 Daily Returns  (corr = {corr:.3f}, n = {len(sub)})",
                 fontsize=13, weight="bold")
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x*100:.1f}%"))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x*100:.1f}%"))
    ax.legend(loc="upper left", frameon=True, facecolor=DARK_BG, edgecolor=DARK_FG)
    save_fig(fig, path)


# ── 主程序 ────────────────────────────────────────────────────────────────────

def main():
    setup_dark_theme()
    ts = timestamp()

    print("=" * 70)
    print("QuantCore Correlation & Diversification Analysis")
    print("=" * 70)

    df = load_baseline()
    print(f"\n[Load] baseline_returns.csv: {len(df)} rows, "
          f"{df.index[0].date()} → {df.index[-1].date()}")
    print(f"       Columns: {list(df.columns)}")

    # 1. Rolling corr
    print("\n[1/5] Rolling 60-day correlations...")
    rc = rolling_correlations(df)
    rc_summary = summarize_rolling(rc)
    rc_path = OUT_DIR / f"rolling_corr_{ts}.csv"
    rc_out = rc.copy()
    rc_out.to_csv(rc_path)
    print(f"      Saved: {rc_path.name}  ({len(rc)} rows)")
    print("      Summary:")
    for col in rc.columns:
        print(f"        {col:24s}  mean={rc_summary.loc[col, 'mean']:+.3f}  "
              f"med={rc_summary.loc[col, 'median']:+.3f}  "
              f"min={rc_summary.loc[col, 'min']:+.3f}  max={rc_summary.loc[col, 'max']:+.3f}")

    plot_rolling_corr(rc, OUT_DIR / f"rolling_corr_timeseries_{ts}.png")

    # 2. Conditional corr
    print("\n[2/5] Conditional correlations (stress / regime)...")
    cc = conditional_correlations(df)
    cc_path = OUT_DIR / f"conditional_corr_{ts}.csv"
    cc.to_csv(cc_path, index=False)
    print(f"      Saved: {cc_path.name}")
    # Print key stress comparison
    stress_sr_sp = cc[(cc["condition"].str.startswith("stress")) & (cc["pair"] == "SR_vs_SP500")]["corr"].iloc[0]
    nonstr_sr_sp = cc[(cc["condition"] == "non_stress") & (cc["pair"] == "SR_vs_SP500")]["corr"].iloc[0]
    print(f"      SR vs SP500 in STRESS days:     {stress_sr_sp:+.3f}")
    print(f"      SR vs SP500 in NON-STRESS days: {nonstr_sr_sp:+.3f}")
    print(f"      Crisis correlation uplift:      {stress_sr_sp - nonstr_sr_sp:+.3f}")

    plot_conditional_heatmap(cc, OUT_DIR / f"conditional_corr_heatmap_{ts}.png")

    # 3. Return attribution
    print("\n[3/5] Return attribution...")
    attr = return_attribution(df)
    attr_path = OUT_DIR / f"return_attribution_{ts}.csv"
    attr.to_csv(attr_path, index=False)
    print(f"      Saved: {attr_path.name}")
    for _, row in attr.iterrows():
        print(f"        {row['metric']:42s}  {row['value']:+.4f}")

    # 4. Momentum factor
    print("\n[4/5] Momentum factor correlations...")
    mf = momentum_factor_correlations(df)
    mf_path = OUT_DIR / f"momentum_factor_corr_{ts}.csv"
    mf.to_csv(mf_path, index=False)
    print(f"      Saved: {mf_path.name}  (proxy={mf['mom_proxy'].iloc[0]})")
    for _, row in mf.iterrows():
        print(f"        {row['series']:6s}  corr={row['correlation']:+.3f}  beta={row['beta']:+.3f}")

    # 5. Leverage comparison
    print("\n[5/5] Leverage comparison...")
    lev = leverage_comparison(df)
    lev_path = OUT_DIR / f"leverage_comparison_{ts}.csv"
    lev.to_csv(lev_path, index=False)
    print(f"      Saved: {lev_path.name}")
    print(f"      {'config':<22s}{'lev':>5s}{'CAGR':>9s}{'vol':>8s}{'Sharpe':>8s}{'MaxDD':>9s}{'Calmar':>8s}")
    for _, row in lev.iterrows():
        cfg = f"SR={row['alloc_sr']:.1f} SP={row['alloc_sp']:.1f}"
        print(f"      {cfg:<22s}{row['leverage']:>5.1f}"
              f"{row['CAGR']*100:>8.1f}%{row['ann_vol']*100:>7.1f}%"
              f"{row['sharpe']:>8.2f}{row['max_dd']*100:>8.1f}%{row['calmar']:>8.2f}")

    best_sharpe = lev.loc[lev["sharpe"].idxmax()]
    best_calmar = lev.loc[lev["calmar"].idxmax()]
    print(f"      Best Sharpe: SR={best_sharpe['alloc_sr']:.1f}/SP={best_sharpe['alloc_sp']:.1f} "
          f"({best_sharpe['leverage']:.1f}x) → {best_sharpe['sharpe']:.2f}")
    print(f"      Best Calmar: SR={best_calmar['alloc_sr']:.1f}/SP={best_calmar['alloc_sp']:.1f} "
          f"({best_calmar['leverage']:.1f}x) → {best_calmar['calmar']:.2f}")

    plot_leverage_scatter(lev, OUT_DIR / f"leverage_risk_return_{ts}.png")
    plot_sr_sp_scatter(df, OUT_DIR / f"scatter_sr_vs_sp500_{ts}.png")

    print("\n" + "=" * 70)
    print("Key findings:")
    print("=" * 70)
    avg_sr_sp = rc["SR_vs_SP500"].mean()
    print(f"  1) Avg 60d corr SR vs SP500:        {avg_sr_sp:+.3f}")
    print(f"  2) Stress regime uplift:            {stress_sr_sp - nonstr_sr_sp:+.3f} "
          f"(stress={stress_sr_sp:+.3f}, calm={nonstr_sr_sp:+.3f})")
    div_ret = attr[attr["metric"] == "Diversification return (var approx)"]["value"].iloc[0]
    vol_red = attr[attr["metric"] == "Volatility reduction"]["value"].iloc[0]
    print(f"  3) Diversification return (annual): {div_ret*100:+.2f}%   "
          f"vol reduction {vol_red*100:+.2f}pp")
    print(f"  4) Best risk-adjusted leverage:     {best_sharpe['leverage']:.1f}x "
          f"(SR={best_sharpe['alloc_sr']:.1f}/SP={best_sharpe['alloc_sp']:.1f})")
    print(f"\nAll outputs → {OUT_DIR}")
    print("Done.")


if __name__ == "__main__":
    main()
