"""
QuantCore Robustness — 停利研究 Phase 1:診斷 pass(門檻錨定)
================================================================
Pre-registered 設計見 robustness/takeprofit_study_design.md。

本腳本**不看策略 P&L、不跑任何停利規則** — 只回答一個問題:
歷史上 baseline 策略「獲利中持股」的三個噴出因子(乖離/skew/kurt)
分布長什麼樣,P80/P90/P95 對應的門檻値是多少。

作法:跑一次 baseline 回測(引擎零改動,subclass 只做包裝掛鉤),
每個週三 rebalance 前快照持股:
  - 回合首筆進場價(episode entry)→ 未實現獲利 %
    (回合/因子語意 = robustness/takeprofit_common.py,含 2026-07-10
     對抗審查兩項修正:續抱不重設回合、乖離單位一致)
  - 佐證欄位:atr_pct(乖離未正規化的波動族群檢查)、rank、adj_slope

因子一律以 signal_date(週三前最後已收盤日)資料計算 — 與 live
「前收 (Reference Close)」語意一致(ADR-0003),無 lookahead。

輸出:
  robustness/output/takeprofit/diagnostic_snapshots.csv   全部快照
  robustness/output/takeprofit/anchor_thresholds.json     P80/90/95 門檻
  robustness/output/takeprofit/diag_*.png                 分布圖 + 案例圖

用法:
  python -m robustness.takeprofit_diagnostic

不修改 strategies/、backtest/、automation/ 下任何檔案。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from robustness.utils import (            # noqa: E402
    OUTPUT_ROOT, compute_metrics, setup_dark_theme, save_fig,
    ACCENT, ACCENT_2, ACCENT_3, ACCENT_4,
)
from robustness.takeprofit_common import (                           # noqa: E402
    EpisodeTrackingMixin, blowoff_factors, SKEW_WINDOWS, FACTOR_NAMES, STUDY_END,
)
from strategies.sp500_momentum import config as sp_cfg              # noqa: E402
from strategies.sp500_momentum.backtester import SP500Backtester    # noqa: E402

OUT_DIR = OUTPUT_ROOT / "takeprofit"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SNAPSHOTS_FP  = OUT_DIR / "diagnostic_snapshots.csv"
THRESHOLDS_FP = OUT_DIR / "anchor_thresholds.json"

FACTORS     = FACTOR_NAMES
PERCENTILES = [50, 80, 90, 95]
PROFIT_PRECONDITIONS = [0.20, 0.40]          # 已登記 grid(設計文件)

CASE_TICKERS = {"STX", "WDC"}


# ── 診斷引擎(包裝,不複製上游方法體)────────────────────────────────────────

class DiagnosticBacktester(EpisodeTrackingMixin, SP500Backtester):
    """baseline 引擎 + 回合進場價追蹤(takeprofit_common)+ 週三快照。

    所有 override 都是 super() 包裝 — 交易語意零改動,回測結果
    必須與 baseline 完全相同(main() 以權益指標對照驗證)。
    """

    def __init__(self, *args, **kwargs):
        self.diag_rows: list[dict] = []
        super().__init__(*args, **kwargs)

    # -- 快照掛鉤 --------------------------------------------------------------

    def _unified_rebalance(self, date, signal_date, is_rotation_week):
        self._collect_snapshot(date, signal_date)
        super()._unified_rebalance(date, signal_date, is_rotation_week)

    def _collect_snapshot(self, date, signal_date):
        held = [t for t in self.holdings
                if not self._is_dip(t) and t != "SPY" and self.holdings[t] > 0]
        if not held:
            return

        bull_confirmed = self.bull_weeks_counter >= 2
        ranked = self.selector.scan_market(signal_date, sp_cfg.LOOKBACK)
        rank_of = {x["ticker"]: i + 1 for i, x in enumerate(ranked)}
        metric_of = {x["ticker"]: x for x in ranked}

        for ticker in held:
            entry = self.episode_entry.get(ticker)
            if entry is None:
                continue   # 理論上不會發生;防禦性略過

            df = self.selector._get_ticker_data(ticker)
            if df is None or df.empty:
                continue
            hist = df.loc[:signal_date]

            factors = blowoff_factors(hist)
            if factors is None:
                continue

            price = float(hist["Close"].iloc[-1])
            if price <= 0:
                continue

            row = {
                "date":           date,
                "signal_date":    signal_date,
                "ticker":         ticker,
                "bull_confirmed": bull_confirmed,
                "entry_date":     entry["date"],
                "entry_price":    entry["price"],
                "price":          price,
                "pnl_pct":        price / entry["price"] - 1.0,
                "weeks_held":     (date - entry["date"]).days / 7.0,
                "rank":           rank_of.get(ticker, np.nan),
                "adj_slope":      metric_of.get(ticker, {}).get("adj_slope", np.nan),
                "atr_pct":        metric_of.get(ticker, {}).get("atr_pct", np.nan),
                **factors,
            }
            self.diag_rows.append(row)


# ── 分析 ──────────────────────────────────────────────────────────────────────

def anchor_thresholds(pool: pd.DataFrame) -> dict:
    out = {}
    for f in FACTORS:
        vals = pool[f].dropna()
        out[f] = {f"P{p}": float(np.percentile(vals, p)) for p in PERCENTILES}
        out[f]["n"] = int(len(vals))
    return out


def trigger_rates(pool: pd.DataFrame, thresholds: dict) -> pd.DataFrame:
    """各(因子, P-level, 獲利前提)組合的觸發率 — 每持股週的觸發機率。
    純事件計數,不涉及後續報酬。"""
    rows = []
    for f in FACTORS:
        for p in (80, 90, 95):
            th = thresholds[f][f"P{p}"]
            for x in PROFIT_PRECONDITIONS:
                mask = (pool["pnl_pct"] >= x) & (pool[f] > th)
                rows.append({
                    "factor": f, "plevel": f"P{p}", "threshold": round(th, 4),
                    "profit_min": x,
                    "trigger_rate": round(float(mask.mean()), 4),
                    "n_triggers": int(mask.sum()),
                })
    return pd.DataFrame(rows)


def vol_cluster_check(pool: pd.DataFrame) -> dict:
    """乖離未正規化的已知風險:因子高分位是否集中在高波動族群。"""
    out = {}
    for f in FACTORS:
        sub = pool[[f, "atr_pct"]].dropna()
        if len(sub) < 30:
            out[f] = None
            continue
        rho = float(sub[f].rank().corr(sub["atr_pct"].rank()))
        top = sub[sub[f] >= sub[f].quantile(0.9)]
        out[f] = {
            "spearman_vs_atr_pct": round(rho, 3),
            "top_decile_mean_atr_pct": round(float(top["atr_pct"].mean()), 4),
            "pool_mean_atr_pct": round(float(sub["atr_pct"].mean()), 4),
        }
    return out


def make_charts(pool: pd.DataFrame, snaps: pd.DataFrame, thresholds: dict):
    import matplotlib.pyplot as plt
    setup_dark_theme()

    # 因子分布 + 分位數線
    fig, axes = plt.subplots(2, 3, figsize=(15, 8))
    for ax, f in zip(axes.flat, FACTORS):
        vals = pool[f].dropna()
        ax.hist(vals, bins=60, color=ACCENT, alpha=0.8)
        for p, c in zip((80, 90, 95), (ACCENT_3, ACCENT_2, ACCENT_4)):
            ax.axvline(thresholds[f][f"P{p}"], color=c, ls="--", lw=1.2,
                       label=f"P{p}={thresholds[f][f'P{p}']:.3f}")
        ax.set_title(f"{f}  (n={len(vals)})")
        ax.legend(fontsize=7)
    axes.flat[-1].axis("off")
    fig.suptitle("Blowoff factors — profitable bull-regime holdings", y=1.0)
    fig.tight_layout()
    save_fig(fig, OUT_DIR / "diag_factor_distributions.png")

    # STX / WDC 案例:乖離軌跡(全快照,不限獲利中)
    fig, axes = plt.subplots(2, 1, figsize=(14, 8), sharex=False)
    for ax, tk in zip(axes, sorted(CASE_TICKERS)):
        sub = snaps[snaps["ticker"] == tk].sort_values("date")
        if sub.empty:
            ax.set_title(f"{tk} — no snapshots")
            continue
        ax.plot(sub["date"], sub["bias"] * 100, color=ACCENT, marker="o",
                ms=3, lw=1, label="bias %")
        ax2 = ax.twinx()
        ax2.plot(sub["date"], sub["pnl_pct"] * 100, color=ACCENT_4, lw=1,
                 alpha=0.7, label="pnl %")
        ax2.grid(False)
        for p, c in zip((80, 90, 95), (ACCENT_3, ACCENT_2, "#ffffff")):
            ax.axhline(thresholds["bias"][f"P{p}"] * 100, color=c, ls="--", lw=0.9)
        ax.set_title(f"{tk} — weekly bias % (left) vs unrealized PnL % (right)")
        ax.legend(loc="upper left", fontsize=8)
        ax2.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    save_fig(fig, OUT_DIR / "diag_case_stx_wdc.png")


def main():
    print("=" * 72)
    print("停利研究 Phase 1 — 診斷 pass(門檻錨定,不看 P&L)")
    print("=" * 72)

    # 上游 ADR-0011 flag 關閉:錨定池定義於「部署前 baseline」的持股,
    # flag 開著重跑會混入封鎖後語意、門檻定義漂移。
    sp_cfg.BLOWOFF_ENTRY_BLOCK = False
    bt = DiagnosticBacktester(end_date=STUDY_END, write_reports=True)
    bt.run()

    # 引擎保真:包裝不得改變 baseline 行為 — 與已知 baseline 指標對照
    eq = bt.get_equity_series()
    m = compute_metrics(eq.pct_change().dropna())
    print(f"\n[fidelity] baseline run: CAGR={m['ann_ret']*100:.2f}%  "
          f"Sharpe={m['sharpe']:.3f}  MaxDD={m['max_dd']*100:.2f}%  "
          f"final=${eq.iloc[-1]:,.0f}")

    snaps = pd.DataFrame(bt.diag_rows)
    snaps.to_csv(SNAPSHOTS_FP, index=False)
    print(f"[save] {len(snaps)} snapshots -> {SNAPSHOTS_FP}")

    # 錨定池:bull regime(規則生效域)且獲利中
    pool = snaps[snaps["bull_confirmed"] & (snaps["pnl_pct"] > 0)].copy()
    print(f"[pool] bull+profitable holding-weeks: {len(pool)} "
          f"({len(pool)/len(snaps)*100:.0f}% of snapshots), "
          f"tickers={pool['ticker'].nunique()}")

    thresholds = anchor_thresholds(pool)
    rates = trigger_rates(pool, thresholds)
    volchk = vol_cluster_check(pool)

    payload = {
        "generated": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "design": "robustness/takeprofit_study_design.md",
        "pool": {"n": int(len(pool)),
                 "definition": "bull_confirmed AND pnl_pct>0, weekly Wednesday snapshots",
                 "period": f"{snaps['date'].min()} .. {snaps['date'].max()}"},
        "thresholds": thresholds,
        "vol_cluster_check": volchk,
        "fidelity": {k: round(float(v), 4) for k, v in m.items()
                     if isinstance(v, (int, float, np.floating))},
    }
    THRESHOLDS_FP.write_text(json.dumps(payload, indent=2, default=str),
                             encoding="utf-8")
    rates.to_csv(OUT_DIR / "trigger_rates.csv", index=False)

    print("\n──門檻錨定(P80/P90/P95)──")
    for f in FACTORS:
        t = thresholds[f]
        print(f"  {f:7s}: P80={t['P80']:+.4f}  P90={t['P90']:+.4f}  "
              f"P95={t['P95']:+.4f}  (n={t['n']})")

    print("\n──波動族群檢查(Spearman vs atr_pct)──")
    for f, v in volchk.items():
        if v:
            print(f"  {f:7s}: rho={v['spearman_vs_atr_pct']:+.3f}  "
                  f"top-decile atr%={v['top_decile_mean_atr_pct']:.3f} "
                  f"vs pool {v['pool_mean_atr_pct']:.3f}")

    print("\n──觸發率(pnl>=X 且 factor>門檻,佔錨定池比例)──")
    piv = rates.pivot_table(index=["factor", "plevel"],
                            columns="profit_min", values="trigger_rate")
    print(piv.to_string())

    # STX/WDC 案例快照存檔(報告用)
    case = snaps[snaps["ticker"].isin(CASE_TICKERS)].sort_values(["ticker", "date"])
    case.to_csv(OUT_DIR / "case_stx_wdc.csv", index=False)

    make_charts(pool, snaps, thresholds)
    print(f"\n[done] outputs in {OUT_DIR}")


if __name__ == "__main__":
    main()
