"""
QuantCore Backtest — Sector-Level Netting Research

比較兩種執行架構，輸入相同的 SR + SkewLS 訊號：

  BASELINE: 兩個策略各自獨立、所有部位都建（current production）。
            SR LONG XLK + SkewLS LONG REW 同時存在。
  NETTED  : Sector-level net 後再建倉。同 sector 互抵後：
            - Net 正 → 只持有 sector long ETF（如 XLK）部位較小
            - Net 負 → 只持有 inverse ETF / 直接 short XLC

兩種架構的 sector factor exposure **完全相同**（同一份 SR + SkewLS 訊號）。
唯一差別在執行：用到的 ticker 數量、commission、inverse ETF 的 expense ratio
+ daily compounding decay。

Usage:
  # 單次（預設 80/30）
  python -m backtest.netting_research

  # 改 alloc
  python -m backtest.netting_research --alloc-sr 0.35 --alloc-skewls 0.60

  # Sweep (A+B：3 種 alloc + 年度拆解)
  python -m backtest.netting_research --mode sweep
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategies.sector_rotation.data_loader import load_data as load_sr_data
from strategies.sector_rotation.backtest    import run_full_backtest
from strategies.sector_skew_ls               import load_data as load_skewls_data
from strategies.sector_skew_ls.backtest      import run_backtest_proxy
from strategies.sector_skew_ls.config        import (
    INVERSE_ETF,          # {long_etf: (inverse_etf, leverage)}
    DIRECT_SHORT_TICKERS, # ["XLC"]
    SECTOR_TICKERS,       # 11 SPDR sector tickers
    ACCOUNT_CAPITAL,
    COST_PER_SHARE,
    MIN_PER_ORDER,
)
from backtest.metrics import compute as compute_metrics, print_table


# ── P&L 計算（基於給定 weights）────────────────────────────────────────────

def compute_pnl(long_w, inv_w, xlc_short_w,
                sector_close, sector_open, inv_close, inv_open,
                capital, cost_per_share, min_per_order):
    """從給定的 weights 算每日淨報酬，含 per-trade commission。"""
    sec_overnight = sector_open.div(sector_close.shift(1)) - 1
    sec_intraday  = sector_close.div(sector_open) - 1
    inv_overnight = inv_open.div(inv_close.shift(1)) - 1
    inv_intraday  = inv_close.div(inv_open) - 1

    long_w_prev = long_w.shift(1).fillna(0)
    inv_w_prev  = inv_w.shift(1).fillna(0)
    xlc_w_prev  = xlc_short_w.shift(1).fillna(0)

    # XLC：sign convention 沿用 SkewLS（xlc_short_w ≤ 0 = short）
    xlc_sec_overnight = sec_overnight[xlc_short_w.columns]
    xlc_sec_intraday  = sec_intraday[xlc_short_w.columns]

    long_pnl = (long_w_prev * sec_overnight     + long_w     * sec_intraday    ).sum(axis=1)
    inv_pnl  = (inv_w_prev  * inv_overnight     + inv_w      * inv_intraday    ).sum(axis=1)
    xlc_pnl  = (xlc_w_prev  * xlc_sec_overnight + xlc_short_w* xlc_sec_intraday).sum(axis=1)

    gross_ret = long_pnl + inv_pnl + xlc_pnl

    # Commission
    now_combined = pd.concat(
        [long_w.add_prefix("L_"), inv_w.add_prefix("I_"), xlc_short_w.add_prefix("S_")],
        axis=1,
    ).fillna(0)
    prev_combined = now_combined.shift(1).fillna(0)
    delta_w = (now_combined - prev_combined).abs()
    turnover = delta_w.sum(axis=1)

    prices_long  = sector_close[long_w.columns].rename(columns=lambda c: f"L_{c}")
    prices_inv   = inv_close[inv_w.columns].rename(columns=lambda c: f"I_{c}")
    prices_short = sector_close[xlc_short_w.columns].rename(columns=lambda c: f"S_{c}")
    prices_combined = pd.concat(
        [prices_long, prices_inv, prices_short], axis=1
    ).reindex(delta_w.index).ffill()

    trade_dollars = delta_w * capital
    shares = (trade_dollars / prices_combined.replace(0, np.nan)).fillna(0)
    per_trade = np.maximum(min_per_order, shares * cost_per_share)
    per_trade = per_trade.where(shares >= 1e-9, 0)
    tc_drag = per_trade.sum(axis=1) / capital

    net_ret = (gross_ret - tc_drag).dropna()
    return {
        "net_ret":  net_ret,
        "tc_drag":  tc_drag.reindex(net_ret.index),
        "turnover": turnover.reindex(net_ret.index),
    }


# ── Positions：baseline vs netted ────────────────────────────────────────────

def build_baseline_positions(sr_w, sk_long_w, sk_inv_w, sk_xlc_w,
                              alloc_sr, alloc_skewls, common_idx):
    """獨立策略 per-ticker weights。"""
    long_w = pd.DataFrame(0.0, index=common_idx, columns=SECTOR_TICKERS)
    for s in SECTOR_TICKERS:
        if s in sr_w.columns:
            long_w[s] += alloc_sr * sr_w[s].reindex(common_idx).fillna(0)
        if s in sk_long_w.columns:
            long_w[s] += alloc_skewls * sk_long_w[s].reindex(common_idx).fillna(0)
    inv_w = alloc_skewls * sk_inv_w.reindex(common_idx).fillna(0)
    xlc_short_w = alloc_skewls * sk_xlc_w.reindex(common_idx).fillna(0)
    return long_w, inv_w, xlc_short_w


def build_netted_positions(sr_w, sk_long_w, sk_inv_w, sk_xlc_w,
                            alloc_sr, alloc_skewls, common_idx):
    """Sector-level netting → per-ticker weights。"""
    sector_exp = pd.DataFrame(0.0, index=common_idx, columns=SECTOR_TICKERS)
    for s in SECTOR_TICKERS:
        if s in sr_w.columns:
            sector_exp[s] += alloc_sr * sr_w[s].reindex(common_idx).fillna(0)
        if s in sk_long_w.columns:
            sector_exp[s] += alloc_skewls * sk_long_w[s].reindex(common_idx).fillna(0)
    for s, (inv_etf, leverage) in INVERSE_ETF.items():
        if inv_etf in sk_inv_w.columns:
            sector_exp[s] -= alloc_skewls * sk_inv_w[inv_etf].reindex(common_idx).fillna(0) * leverage
    for s in DIRECT_SHORT_TICKERS:
        if s in sk_xlc_w.columns:
            sector_exp[s] += alloc_skewls * sk_xlc_w[s].reindex(common_idx).fillna(0)

    long_w      = pd.DataFrame(0.0, index=common_idx, columns=SECTOR_TICKERS)
    inv_w       = pd.DataFrame(0.0, index=common_idx,
                               columns=[v[0] for v in INVERSE_ETF.values()])
    xlc_short_w = pd.DataFrame(0.0, index=common_idx, columns=DIRECT_SHORT_TICKERS)

    for s in SECTOR_TICKERS:
        net = sector_exp[s]
        pos = net > 0
        neg = net < 0
        long_w.loc[pos, s] = net.loc[pos]
        if s in INVERSE_ETF:
            inv_etf, leverage = INVERSE_ETF[s]
            inv_w.loc[neg, inv_etf] = (-net.loc[neg]) / leverage
        elif s in DIRECT_SHORT_TICKERS:
            xlc_short_w.loc[neg, s] = net.loc[neg]   # 保持 signed

    return long_w, inv_w, xlc_short_w, sector_exp


# ── Setup + 單次比較 ─────────────────────────────────────────────────────────

def setup():
    """跑一次 SR + SkewLS backtest，回傳共享資料 (給 sweep 重用)。"""
    sr_data = load_sr_data()
    skewls_data = load_skewls_data()
    _, sr_weights, _ = run_full_backtest(sr_data)
    skewls_result = run_backtest_proxy(skewls_data)
    return {
        "sr_weights":         sr_weights,
        "sk_long_w":          skewls_result["long_w"],
        "sk_inv_w":           skewls_result["inv_w"],
        "sk_xlc_w":           skewls_result["xlc_short_w"],
        "sector_close":       skewls_data["sector_close"],
        "sector_open":        skewls_data["sector_open"],
        "inv_close":          skewls_data["inv_close"],
        "inv_open":           skewls_data["inv_open"],
    }


def run_one(setup_data, alloc_sr, alloc_skewls, start="2019-01-01"):
    """單一 alloc 組合的 baseline vs netted 比較。"""
    sr_w     = setup_data["sr_weights"]
    sk_long_w= setup_data["sk_long_w"]
    sk_inv_w = setup_data["sk_inv_w"]
    sk_xlc_w = setup_data["sk_xlc_w"]

    common = sr_w.index.intersection(sk_long_w.index)
    if start:
        common = common[common >= pd.Timestamp(start)]

    sector_close = setup_data["sector_close"].reindex(common).ffill()
    sector_open  = setup_data["sector_open"].reindex(common).ffill()
    inv_close    = setup_data["inv_close"].reindex(common).ffill()
    inv_open     = setup_data["inv_open"].reindex(common).ffill()

    # Baseline
    long_w_b, inv_w_b, xlc_w_b = build_baseline_positions(
        sr_w, sk_long_w, sk_inv_w, sk_xlc_w, alloc_sr, alloc_skewls, common,
    )
    baseline = compute_pnl(
        long_w_b, inv_w_b, xlc_w_b,
        sector_close, sector_open, inv_close, inv_open,
        capital=ACCOUNT_CAPITAL, cost_per_share=COST_PER_SHARE, min_per_order=MIN_PER_ORDER,
    )

    # Netted
    long_w_n, inv_w_n, xlc_w_n, sector_exp = build_netted_positions(
        sr_w, sk_long_w, sk_inv_w, sk_xlc_w, alloc_sr, alloc_skewls, common,
    )
    netted = compute_pnl(
        long_w_n, inv_w_n, xlc_w_n,
        sector_close, sector_open, inv_close, inv_open,
        capital=ACCOUNT_CAPITAL, cost_per_share=COST_PER_SHARE, min_per_order=MIN_PER_ORDER,
    )

    # Sanity
    base_net_exp = long_w_b.copy()
    for s, (inv_etf, leverage) in INVERSE_ETF.items():
        if inv_etf in inv_w_b.columns:
            base_net_exp[s] -= inv_w_b[inv_etf] * leverage
    for s in DIRECT_SHORT_TICKERS:
        if s in xlc_w_b.columns:
            base_net_exp[s] += xlc_w_b[s]
    diff = (base_net_exp - sector_exp).abs().max().max()
    assert diff < 1e-9, f"Sector exposure mismatch: {diff:.2e}"

    return {
        "alloc_sr":     alloc_sr,
        "alloc_skewls": alloc_skewls,
        "baseline":     baseline,
        "netted":       netted,
        "positions": {
            "inv_w_b": inv_w_b, "inv_w_n": inv_w_n,
            "xlc_w_b": xlc_w_b, "xlc_w_n": xlc_w_n,
            "long_w_b": long_w_b, "long_w_n": long_w_n,
        },
    }


# ── 年度拆解 ─────────────────────────────────────────────────────────────────

def compare_yearly(baseline_ret, netted_ret) -> pd.DataFrame:
    yr_b = (1 + baseline_ret).resample("YE").prod() - 1
    yr_n = (1 + netted_ret).resample("YE").prod() - 1
    yr_b.index = yr_b.index.year
    yr_n.index = yr_n.index.year
    return pd.DataFrame({
        "BASELINE": yr_b,
        "NETTED":   yr_n,
        "Δ (bps)":  (yr_n - yr_b) * 10000,
    })


def print_yearly(yr_df: pd.DataFrame, label: str = ""):
    print(f"\n  ── Yearly breakdown {label}─" + "─" * (40 - len(label)))
    print(f"    {'Year':<6} {'BASELINE':>10} {'NETTED':>10} {'Δ (bps)':>10}")
    cum_delta = 0
    for year, row in yr_df.iterrows():
        b_pct = f"{row['BASELINE']*100:+7.2f}%"
        n_pct = f"{row['NETTED']*100:+7.2f}%"
        d_bps = row["Δ (bps)"]
        cum_delta += d_bps
        mark = "✓" if d_bps > 0 else ("=" if abs(d_bps) < 5 else "✗")
        print(f"    {year:<6} {b_pct:>10} {n_pct:>10} {d_bps:>+9.1f}  {mark}")
    n_yrs = len(yr_df)
    print(f"    {'─'*6} {'─'*10} {'─'*10} {'─'*10}")
    print(f"    {'Avg':<6} {'':>10} {'':>10} {cum_delta/n_yrs:>+9.1f}  (avg/yr)")


# ── 單次模式 ─────────────────────────────────────────────────────────────────

def run_single(alloc_sr=0.80, alloc_skewls=0.30, start="2019-01-01"):
    print("=" * 75)
    print(f"  Sector-Level Netting Research — SINGLE")
    print(f"  alloc_sr={alloc_sr}, alloc_skewls={alloc_skewls}, start={start}")
    print("=" * 75)

    print("\nLoading + running strategies...")
    setup_data = setup()
    r = run_one(setup_data, alloc_sr, alloc_skewls, start)

    base_ret = r["baseline"]["net_ret"]
    net_ret  = r["netted"]["net_ret"]

    m_base = compute_metrics(base_ret, label="BASELINE")
    m_net  = compute_metrics(net_ret,  label="NETTED")
    print_table([m_base, m_net])

    # Year-by-year
    yr_df = compare_yearly(base_ret, net_ret)
    print_yearly(yr_df)

    # Position / friction stats
    p = r["positions"]
    print(f"\n  ── Position / friction ─────────────────────────────")
    inv_b = p["inv_w_b"].sum(axis=1).mean() * 100
    inv_n = p["inv_w_n"].sum(axis=1).mean() * 100
    print(f"    Avg inverse ETF % NAV : {inv_b:.2f}% → {inv_n:.2f}%   (saved {inv_b-inv_n:+.2f}%)")
    tc_b = r["baseline"]["tc_drag"].mean() * 252 * 100
    tc_n = r["netted"]["tc_drag"].mean()   * 252 * 100
    print(f"    TC drag (annual)      : {tc_b:.3f}% → {tc_n:.3f}%   (saved {tc_b-tc_n:+.3f}%/yr)")

    # CSV
    out_dir = ROOT / "backtest" / "reports"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"netting_single_sr{alloc_sr:.2f}_sk{alloc_skewls:.2f}.csv"
    pd.DataFrame({
        "baseline_ret": base_ret,
        "netted_ret":   net_ret,
    }).to_csv(out_path)
    print(f"\n  CSV: {out_path}")


# ── Sweep 模式 (A + B) ───────────────────────────────────────────────────────

def run_sweep(start="2019-01-01"):
    """A+B: 多 alloc + 年度拆解。"""
    allocs = [
        (0.80, 0.30, "Taylor proposal"),
        (0.80, 0.50, "Higher SkewLS"),
        (0.35, 0.60, "Yvonne actual"),
        (0.00, 1.00, "Pure SkewLS"),
    ]

    print("=" * 75)
    print(f"  Sector-Level Netting Research — SWEEP (A+B)")
    print(f"  Start: {start}")
    print("=" * 75)

    print("\nLoading + running strategies (shared)...")
    setup_data = setup()
    print("  Done.")

    all_results = []
    for (a_sr, a_skewls, label) in allocs:
        print(f"\n{'═' * 75}")
        print(f"  {label}:  SR {a_sr:.2f} × SP500 implicit-skip × SkewLS {a_skewls:.2f}")
        print(f"{'═' * 75}")
        r = run_one(setup_data, a_sr, a_skewls, start)
        all_results.append((label, r))

        base_ret = r["baseline"]["net_ret"]
        net_ret  = r["netted"]["net_ret"]
        m_base = compute_metrics(base_ret, label="BASELINE")
        m_net  = compute_metrics(net_ret,  label="NETTED")

        # Summary one-liner
        ds = m_net["sharpe"]  - m_base["sharpe"]
        da = (m_net["ann_ret"]- m_base["ann_ret"]) * 10000
        dc = m_net["calmar"]  - m_base["calmar"]
        df_  = m_net["final_1m"] - m_base["final_1m"]
        print(f"\n  Sharpe : {m_base['sharpe']:.3f} → {m_net['sharpe']:.3f}  (Δ {ds:+.3f})")
        print(f"  Annual : {m_base['ann_ret']*100:+.2f}% → {m_net['ann_ret']*100:+.2f}%  (Δ {da:+.1f} bps)")
        print(f"  Calmar : {m_base['calmar']:.3f} → {m_net['calmar']:.3f}  (Δ {dc:+.3f})")
        print(f"  $1M end: ${m_base['final_1m']:,.0f} → ${m_net['final_1m']:,.0f}  (Δ ${df_:+,.0f})")

        # Position stats
        inv_b = r["positions"]["inv_w_b"].sum(axis=1).mean() * 100
        inv_n = r["positions"]["inv_w_n"].sum(axis=1).mean() * 100
        print(f"  inv-ETF% NAV: {inv_b:.2f}% → {inv_n:.2f}%  (saved {inv_b-inv_n:+.2f}pp)")

        # Year-by-year
        yr_df = compare_yearly(base_ret, net_ret)
        print_yearly(yr_df)

    # ── Final sweep table ────────────────────────────────────────────
    print()
    print("=" * 75)
    print("  SWEEP FINAL TABLE")
    print("=" * 75)
    print(f"  {'Alloc Combo':<30} {'BSh':>5} {'NSh':>5} {'Δ':>6}  "
          f"{'BAnn':>7} {'NAnn':>7} {'Δ(bp)':>7}  {'Δinv%':>6}")
    for label, r in all_results:
        a_sr, a_sk = r["alloc_sr"], r["alloc_skewls"]
        m_b = compute_metrics(r["baseline"]["net_ret"], label="b")
        m_n = compute_metrics(r["netted"]["net_ret"],   label="n")
        inv_b = r["positions"]["inv_w_b"].sum(axis=1).mean() * 100
        inv_n = r["positions"]["inv_w_n"].sum(axis=1).mean() * 100
        combo = f"SR{a_sr:.2f}+SK{a_skewls if False else a_sk:.2f} {label[:12]}"
        print(f"  {combo:<30} "
              f"{m_b['sharpe']:>5.2f} {m_n['sharpe']:>5.2f} {m_n['sharpe']-m_b['sharpe']:>+6.3f}  "
              f"{m_b['ann_ret']*100:>+6.2f}% {m_n['ann_ret']*100:>+6.2f}% "
              f"{(m_n['ann_ret']-m_b['ann_ret'])*10000:>+7.1f}  "
              f"{inv_n-inv_b:>+6.2f}")
    print()
    print("  ✓ = netted wins  ✗ = netted loses  = ≈ flat (<5 bps)")
    print()


# ── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode",         choices=["single", "sweep"], default="single")
    ap.add_argument("--alloc-sr",     type=float, default=0.80)
    ap.add_argument("--alloc-skewls", type=float, default=0.30)
    ap.add_argument("--start",        type=str,   default="2019-01-01")
    args = ap.parse_args()

    if args.mode == "sweep":
        run_sweep(start=args.start)
    else:
        run_single(args.alloc_sr, args.alloc_skewls, args.start)
