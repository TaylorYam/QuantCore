"""
QuantCore Backtest — Minimum Position Size Threshold Research

研究問題：如果 SkewLS 部位（占 NAV）小於 X%，就不建倉，這樣會省 friction、
但會不會切掉真正想要的訊號？

Filter 邏輯（每日、每個 SkewLS ticker）：
  if abs(weight) × alloc_skewls < threshold_in_nav:
      weight = 0
  剩下的權重保持原樣（不 redistribute），少掉的曝險變現金。

Sweep：0% / 0.5% / 1% / 2% / 3% / 5% / 7% / 10% of NAV
  0% = baseline，沒任何 filter（current production behavior）

兩個 scenarios：
  A. SkewLS-only (alloc_skewls=1.0) — 純看 threshold 對 SkewLS 自身影響
  B. Combined Yvonne (SR 0.35 + SkewLS 0.60) — 在實際 portfolio 裡的影響

Usage:
  python -m backtest.min_position_research                   # 兩個 scenario 都跑
  python -m backtest.min_position_research --scenario A      # 只跑 standalone
  python -m backtest.min_position_research --scenario B      # 只跑 combined
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategies.sector_skew_ls.config import (
    ACCOUNT_CAPITAL,
    COST_PER_SHARE,
    MIN_PER_ORDER,
)
from backtest.metrics import compute as compute_metrics
from backtest.netting_research import (
    setup,
    compute_pnl,
    build_baseline_positions,
)


THRESHOLDS = [0.0, 0.005, 0.01, 0.02, 0.03, 0.05, 0.07, 0.10]  # of NAV


def apply_min_filter(weights_df: pd.DataFrame,
                      weight_threshold: float) -> pd.DataFrame:
    """zero out 部位 |weight| < weight_threshold（strategy-internal scale）。"""
    if weight_threshold <= 0:
        return weights_df.copy()
    out = weights_df.copy()
    mask = out.abs() < weight_threshold
    out[mask] = 0.0
    return out


def run_one_threshold(setup_data, alloc_sr, alloc_skewls, threshold_nav,
                       start="2019-01-01"):
    """跑單一 threshold。回傳 metrics + 統計。"""
    sr_w     = setup_data["sr_weights"]
    sk_long  = setup_data["sk_long_w"]
    sk_inv   = setup_data["sk_inv_w"]
    sk_xlc   = setup_data["sk_xlc_w"]

    common = sr_w.index.intersection(sk_long.index)
    if start:
        common = common[common >= pd.Timestamp(start)]

    sector_close = setup_data["sector_close"].reindex(common).ffill()
    sector_open  = setup_data["sector_open"].reindex(common).ffill()
    inv_close    = setup_data["inv_close"].reindex(common).ffill()
    inv_open     = setup_data["inv_open"].reindex(common).ffill()

    # Convert NAV threshold → SkewLS-internal weight threshold
    weight_th = threshold_nav / alloc_skewls if alloc_skewls > 0 else 0

    sk_long_f = apply_min_filter(sk_long.reindex(common).fillna(0), weight_th)
    sk_inv_f  = apply_min_filter(sk_inv.reindex(common).fillna(0),  weight_th)
    sk_xlc_f  = apply_min_filter(sk_xlc.reindex(common).fillna(0),  weight_th)

    long_w, inv_w, xlc_w = build_baseline_positions(
        sr_w, sk_long_f, sk_inv_f, sk_xlc_f,
        alloc_sr, alloc_skewls, common,
    )
    pnl = compute_pnl(
        long_w, inv_w, xlc_w,
        sector_close, sector_open, inv_close, inv_open,
        capital=ACCOUNT_CAPITAL,
        cost_per_share=COST_PER_SHARE,
        min_per_order=MIN_PER_ORDER,
    )

    m = compute_metrics(pnl["net_ret"], label=f"th={threshold_nav*100:.1f}%")

    # 統計：filter 掉幾個部位
    n_long_orig = (sk_long.reindex(common).fillna(0).abs() > 1e-6).sum(axis=1).mean()
    n_long_f    = (sk_long_f.abs() > 1e-6).sum(axis=1).mean()
    n_inv_orig  = (sk_inv.reindex(common).fillna(0).abs() > 1e-6).sum(axis=1).mean()
    n_inv_f     = (sk_inv_f.abs() > 1e-6).sum(axis=1).mean()
    n_xlc_orig  = (sk_xlc.reindex(common).fillna(0).abs() > 1e-6).sum(axis=1).mean()
    n_xlc_f     = (sk_xlc_f.abs() > 1e-6).sum(axis=1).mean()
    n_skewls_orig = n_long_orig + n_inv_orig + n_xlc_orig
    n_skewls_f    = n_long_f + n_inv_f + n_xlc_f

    # SkewLS 實際 deployed % NAV（filter 後剩多少）
    skewls_deployed_orig = alloc_skewls * (
        sk_long.reindex(common).fillna(0).abs().sum(axis=1)
        + sk_inv.reindex(common).fillna(0).abs().sum(axis=1)
        + sk_xlc.reindex(common).fillna(0).abs().sum(axis=1)
    ).mean() * 100
    skewls_deployed_f = alloc_skewls * (
        sk_long_f.abs().sum(axis=1)
        + sk_inv_f.abs().sum(axis=1)
        + sk_xlc_f.abs().sum(axis=1)
    ).mean() * 100

    return {
        "threshold_nav":          threshold_nav,
        "metrics":                m,
        "tc_drag_pct_yr":         pnl["tc_drag"].mean() * 252 * 100,
        "n_skewls_orig":          n_skewls_orig,
        "n_skewls_filtered":      n_skewls_f,
        "skewls_deployed_orig":   skewls_deployed_orig,
        "skewls_deployed_filtered": skewls_deployed_f,
        "ret":                    pnl["net_ret"],
    }


def print_sweep_table(results, title):
    print()
    print(f"  {title}")
    print(f"  " + "═" * 95)
    print(f"  {'Thresh':<8} {'Sharpe':>7} {'Calmar':>7} {'Annual':>9} {'MDD':>9} "
          f"{'TC %/yr':>9} {'#Pos':>6} {'Deployed':>10}")
    print(f"  " + "─" * 95)
    base = results[0]   # threshold = 0 (no filter)
    base_sharpe = base["metrics"]["sharpe"]
    base_ann = base["metrics"]["ann_ret"]
    for r in results:
        th = r["threshold_nav"] * 100
        m  = r["metrics"]
        delta_sharpe = m["sharpe"] - base_sharpe
        delta_ann_bps = (m["ann_ret"] - base_ann) * 10000
        sharpe_str = f"{m['sharpe']:.3f}"
        ann_str = f"{m['ann_ret']*100:+6.2f}%"
        # mark winners
        if abs(th - 0) < 1e-6:
            mark = " (baseline)"
        elif delta_sharpe > 0.005 and delta_ann_bps > 5:
            mark = f" ✓ Δsh{delta_sharpe:+.3f} Δann{delta_ann_bps:+.0f}bp"
        elif delta_sharpe < -0.005 or delta_ann_bps < -5:
            mark = f" ✗ Δsh{delta_sharpe:+.3f} Δann{delta_ann_bps:+.0f}bp"
        else:
            mark = f" = Δsh{delta_sharpe:+.3f} Δann{delta_ann_bps:+.0f}bp"

        print(f"  {th:<7.2f}% {sharpe_str:>7} {m['calmar']:>7.3f} "
              f"{ann_str:>9} {m['max_dd']*100:>+8.2f}% "
              f"{r['tc_drag_pct_yr']:>8.3f}% {r['n_skewls_filtered']:>6.1f} "
              f"{r['skewls_deployed_filtered']:>9.2f}%  {mark}")


def print_yearly_for_best(results, baseline_ret):
    """印 baseline vs best threshold 的年度拆解。"""
    # Find best by Sharpe
    best = max(results[1:], key=lambda r: r["metrics"]["sharpe"])
    best_th = best["threshold_nav"] * 100
    best_ret = best["ret"]

    yr_b = (1 + baseline_ret).resample("YE").prod() - 1
    yr_t = (1 + best_ret).resample("YE").prod() - 1
    yr_b.index = yr_b.index.year
    yr_t.index = yr_t.index.year

    print()
    print(f"  ── Year-by-year: baseline vs best Sharpe (threshold={best_th:.1f}%)")
    print(f"  " + "─" * 60)
    print(f"  {'Year':<6} {'BASELINE':>10} {'th='+f'{best_th:.1f}%':>10} {'Δ (bps)':>10}")
    cum_d = 0
    n = 0
    for year in yr_b.index:
        b = yr_b[year]
        t = yr_t.get(year, np.nan)
        d = (t - b) * 10000 if not pd.isna(t) else 0
        cum_d += d
        n += 1
        m = "✓" if d > 0 else ("=" if abs(d) < 5 else "✗")
        print(f"  {year:<6} {b*100:>+8.2f}% {t*100:>+8.2f}%  {d:>+9.1f}  {m}")
    print(f"  {'─'*6} {'─'*10} {'─'*10} {'─'*10}")
    print(f"  {'avg/yr':<6} {'':>10} {'':>10}  {cum_d/n:>+9.1f}")


def run_scenario(setup_data, scenario_name, alloc_sr, alloc_skewls,
                 start="2019-01-01"):
    print()
    print("=" * 95)
    print(f"  Scenario {scenario_name}: SR={alloc_sr:.2f}, SkewLS={alloc_skewls:.2f}")
    print("=" * 95)

    results = []
    for th in THRESHOLDS:
        r = run_one_threshold(setup_data, alloc_sr, alloc_skewls, th, start)
        results.append(r)

    title = f"Threshold sweep — alloc_sr={alloc_sr:.2f}, alloc_skewls={alloc_skewls:.2f}"
    print_sweep_table(results, title)

    # 找最佳 + 年度
    print_yearly_for_best(results, results[0]["ret"])

    # SkewLS-original position count
    n_orig = results[0]["n_skewls_orig"]
    print(f"\n  Avg SkewLS positions in original signal: {n_orig:.1f}")
    print(f"  (deployed % NAV at threshold 0 = {results[0]['skewls_deployed_orig']:.2f}%)")

    return results


def main(scenario: str = "both", start: str = "2019-01-01"):
    print("=" * 95)
    print(f"  Minimum Position Size Threshold Research")
    print(f"  Start: {start}")
    print(f"  Thresholds: {[f'{t*100:.1f}%' for t in THRESHOLDS]}")
    print("=" * 95)

    print("\nLoading + running strategies (shared)...")
    setup_data = setup()
    print("  Done.")

    all_scenarios = {}

    if scenario in ("A", "both"):
        all_scenarios["A"] = run_scenario(
            setup_data, "A (SkewLS only)",
            alloc_sr=0.0, alloc_skewls=1.0, start=start,
        )

    if scenario in ("B", "both"):
        all_scenarios["B"] = run_scenario(
            setup_data, "B (Yvonne actual)",
            alloc_sr=0.35, alloc_skewls=0.60, start=start,
        )

    # 存 CSV
    out_dir = ROOT / "backtest" / "reports"
    out_dir.mkdir(exist_ok=True)

    for sc_name, results in all_scenarios.items():
        rows = []
        for r in results:
            m = r["metrics"]
            rows.append({
                "threshold_nav_pct":     r["threshold_nav"] * 100,
                "sharpe":                m["sharpe"],
                "calmar":                m["calmar"],
                "annual_pct":            m["ann_ret"] * 100,
                "max_dd_pct":            m["max_dd"] * 100,
                "tc_drag_pct_per_yr":    r["tc_drag_pct_yr"],
                "n_skewls_positions":    r["n_skewls_filtered"],
                "skewls_deployed_pct":   r["skewls_deployed_filtered"],
                "final_1m":              m["final_1m"],
            })
        df = pd.DataFrame(rows)
        path = out_dir / f"min_position_scenario_{sc_name}.csv"
        df.to_csv(path, index=False)
        print(f"\n  CSV saved: {path}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=["A", "B", "both"], default="both")
    ap.add_argument("--start",    type=str, default="2019-01-01")
    args = ap.parse_args()
    main(args.scenario, args.start)
