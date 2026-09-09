"""
停利研究附錄 — 新 SP500(F3: +kurt60>P90 進場封鎖)對三策略投組的影響試算
================================================================
問題:若部署新 stock mom,三策略 alloc 與槓桿的最適解會不會移動?
以 10K 帳戶(NAV $10,000)試算調整前後對整個投組的影響。

方法:
  1. 三策略日報酬 return-level 組合(ADR-0001 factor-exposure 語意,
     與 backtest/combo.py 同源)。SP500 舊 = A0 baseline、新 = D1(F3)
     equity 曲線直接載入(凍結窗 2018-07→2026-07-09)。
  2. 現行三帳戶 alloc profile:舊 vs 新 SP500 對照(exact combine_monthly,
     含月初重設 + IB 分級融資成本,initial_equity=10K)。
  3. Alloc 網格掃描(step 0.05,sum ≤ 1.5):先用「每日再平衡固定權重」
     向量化近似排序,top 候選 + 各槓桿檔位最優再用 exact combine_monthly
     覆核。目標 = Sharpe(Calmar 並列參考)。
  4. 10K 實務換算:sleeve 金額、整數執行稅提醒(frac_sizing 研究)。

In-sample 警語:alloc 掃描是同一段 8 年資料上的最適化 —「最適點的移動
方向」比「最適點的精確値」可信。不動 live 設定。

用法: python -m robustness.takeprofit_portfolio_impact
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np                                                   # noqa: E402
import pandas as pd                                                  # noqa: E402

from backtest.combo import run_sr, run_skewls, combine_monthly       # noqa: E402
from backtest.leverage import ib_blended_rate                        # noqa: E402
from robustness.utils import OUTPUT_ROOT, compute_metrics            # noqa: E402
from robustness.takeprofit_common import STUDY_END                   # noqa: E402

ARMS_DIR = OUTPUT_ROOT / "takeprofit" / "arms"
OUT_FP   = OUTPUT_ROOT / "takeprofit" / "portfolio_impact.csv"

NAV = 10_000.0
ACCOUNTS = {                       # 現行 live alloc(accounts.json,唯讀)
    "Taylor": (0.70, 0.50, 0.20),
    "Rosa":   (0.60, 0.30, 0.40),
    "Yvonne": (0.35, 0.25, 0.60),
}
GRID_STEP = 0.05
LEV_CAP   = 1.5


def _dedup(s: pd.Series) -> pd.Series:
    """去除重複日期(keep last)— backtester history 對 end_date 會記兩筆,
    combo.to_equity_curves 亦有同樣的已知處理。"""
    return s[~s.index.duplicated(keep="last")].sort_index()


def load_sp500_returns(name: str) -> pd.Series:
    eq = pd.read_csv(ARMS_DIR / f"equity_{name}.csv",
                     index_col=0, parse_dates=True).iloc[:, 0]
    ret = _dedup(eq).pct_change().dropna()
    ret.name = name
    return ret


def metrics_row(ret: pd.Series) -> dict:
    m = compute_metrics(ret.dropna())
    return {"cagr": float(m["ann_ret"]), "sharpe": float(m["sharpe"]),
            "max_dd": float(m["max_dd"]), "calmar": float(m["calmar"])}


def exact_combo(R: pd.DataFrame, a_sr: float, a_sp: float, a_sk: float,
                sp_col: str) -> dict:
    ret = combine_monthly(
        R["sr"], R[sp_col], alloc_sr=a_sr, alloc_sp=a_sp,
        ret_skewls=R["skewls"] if a_sk > 0 else None, alloc_skewls=a_sk,
        initial_equity=NAV)
    row = metrics_row(ret)
    row.update(alloc_sr=a_sr, alloc_sp500=a_sp, alloc_skewls=a_sk,
               leverage=round(a_sr + a_sp + a_sk, 2))
    return row


def sweep_fast(R: pd.DataFrame, sp_col: str) -> pd.DataFrame:
    """每日再平衡固定權重近似(排序用)。財務成本依 10K 帳戶分級利率。"""
    grid = np.round(np.arange(0.0, 1.0 + 1e-9, GRID_STEP), 2)
    mat = R[["sr", sp_col, "skewls"]].to_numpy()
    combos = [(a, b, c) for a in grid for b in grid for c in grid
              if 0.5 <= a + b + c <= LEV_CAP + 1e-9]
    W = np.array(combos)                                # (n, 3)
    lev = W.sum(axis=1)
    borrow = np.maximum(0.0, lev - 1.0) * NAV
    fin_daily = np.array([ib_blended_rate(b) for b in borrow]) \
        * np.maximum(0.0, lev - 1.0) / 252.0
    port = mat @ W.T - fin_daily                        # (T, n)
    mu = port.mean(axis=0) * 252
    sd = port.std(axis=0, ddof=1) * np.sqrt(252)
    sharpe = np.where(sd > 0, mu / sd, np.nan)
    df = pd.DataFrame(W, columns=["alloc_sr", "alloc_sp500", "alloc_skewls"])
    df["leverage"] = np.round(lev, 2)
    df["sharpe_fast"] = sharpe
    return df


def main():
    print("[load] SP500 old (A0) / new (D1=F3) equity curves...")
    sp_old = load_sp500_returns("A0_baseline")
    sp_new = load_sp500_returns("D1_block_kurt60_P90")

    print("[run] SR / SkewLS daily returns (2018-07-01 -> STUDY_END)...")
    ret_sr = _dedup(run_sr("2018-07-01", STUDY_END))
    ret_sk = _dedup(run_skewls("2018-07-01", STUDY_END))

    R = pd.DataFrame({"sr": ret_sr, "sp_old": sp_old, "sp_new": sp_new,
                      "skewls": ret_sk}).dropna()
    print(f"[align] common window {R.index[0].date()} .. {R.index[-1].date()}"
          f"  ({len(R)} days)")

    print("\n──策略相關性(日報酬)──")
    print(R.corr().round(3).to_string())
    print("\n──單策略指標──")
    for c in R.columns:
        m = metrics_row(R[c])
        print(f"  {c:7s}: CAGR={m['cagr']*100:6.2f}%  Sharpe={m['sharpe']:.3f}  "
              f"MDD={m['max_dd']*100:.2f}%")

    # ── Part A: 現行三帳戶 profile,舊 vs 新 ──
    rows = []
    print("\n──現行 alloc profile(10K 帳戶,exact monthly combine)──")
    for name, (a, b, c) in ACCOUNTS.items():
        old = exact_combo(R, a, b, c, "sp_old"); old["case"] = f"{name}_old"
        new = exact_combo(R, a, b, c, "sp_new"); new["case"] = f"{name}_new"
        rows += [old, new]
        d_cagr = (new["cagr"] - old["cagr"]) * 100
        d_dd   = (new["max_dd"] - old["max_dd"]) * 100
        print(f"  {name} {a}/{b}/{c} (lev {a+b+c:.2f}x): "
              f"Sharpe {old['sharpe']:.3f}→{new['sharpe']:.3f}  "
              f"CAGR {old['cagr']*100:.2f}%→{new['cagr']*100:.2f}% ({d_cagr:+.2f}pp)  "
              f"MDD {old['max_dd']*100:.2f}%→{new['max_dd']*100:.2f}% ({d_dd:+.2f}pp)  "
              f"$PnL/yr ${NAV*old['cagr']:,.0f}→${NAV*new['cagr']:,.0f}")

    # ── Part B: alloc 掃描(fast 排序 + exact 覆核)──
    print("\n[sweep] grid step 0.05, 0.5 <= leverage <= 1.5 ...")
    best_rows = []
    for sp_col, tag in [("sp_old", "old"), ("sp_new", "new")]:
        fast = sweep_fast(R, sp_col)
        # 各槓桿檔位的 fast 最優 + 全域 top3 → exact 覆核
        cand = []
        for lv in (1.0, 1.2, 1.3, 1.4, 1.5):
            sub = fast[np.isclose(fast["leverage"], lv)]
            cand.append(sub.nlargest(1, "sharpe_fast"))
        cand.append(fast.nlargest(3, "sharpe_fast"))
        cand = pd.concat(cand).drop_duplicates(
            subset=["alloc_sr", "alloc_sp500", "alloc_skewls"])
        for _, r in cand.iterrows():
            row = exact_combo(R, float(r["alloc_sr"]), float(r["alloc_sp500"]),
                              float(r["alloc_skewls"]), sp_col)
            row["case"] = f"opt_{tag}"
            best_rows.append(row)
        print(f"  [{tag}] {len(cand)} candidates exact-verified")

    df = pd.DataFrame(rows + best_rows)
    df.to_csv(OUT_FP, index=False)

    print("\n──最適 alloc(exact 覆核後,依 Sharpe)──")
    for tag in ("old", "new"):
        sub = (df[df["case"] == f"opt_{tag}"]
               .sort_values("sharpe", ascending=False))
        print(f"  [{tag}]")
        for _, r in sub.iterrows():
            print(f"    SR {r['alloc_sr']:.2f} / SP500 {r['alloc_sp500']:.2f} / "
                  f"SkewLS {r['alloc_skewls']:.2f}  (lev {r['leverage']:.2f}x)  "
                  f"Sharpe={r['sharpe']:.3f}  CAGR={r['cagr']*100:.2f}%  "
                  f"MDD={r['max_dd']*100:.2f}%")

    print(f"\n[done] -> {OUT_FP}")


if __name__ == "__main__":
    main()
