"""
Walk-Forward A/B — SP500 Momentum「噴出進場封鎖」(停利研究關卡二)

停利研究(robustness/takeprofit_study_design.md)關卡一結果:停利出場
零貢獻(D2 全輸 baseline),殘留訊號 = kurt60/skew60 > P90 進場封鎖
(D1_block in-sample Sharpe 1.256 vs baseline 1.060)。依預登記,勝出
參數(已退化為純封鎖)過 WF gate。

Variant A = baseline(無封鎖)
Variant B = block_only(kurt60 或 skew60 > P90 不佔補位名額)

協議完全比照 ADR-0005 的 wf_ema_entry_filter.py:
Pardo rolling WF,N=8 主跑 + N=7 敏感度,IS:OOS = 3:1,fitness = IS Sharpe,
PASS gate: pardo_chain > 0.50 且 sharpe_chain > 0.40。

差異僅一處(lookahead 防護):B 的門檻**每折以 IS 窗內的診斷快照重新
錨定 P90**(diagnostic_snapshots.csv 來自 baseline run,與封鎖規則無關;
快照池 = bull_confirmed 且 pnl>0 且 date ∈ IS 窗),OOS 沿用該折 IS
錨定値(參數凍結於 IS)。全樣本錨定的 P90 只在關卡一使用,WF 不用。

資料凍結 STUDY_END=2026-07-09(takeprofit_common)。

用法:
  python -m robustness.wf_takeprofit_block --factor kurt60
  python -m robustness.wf_takeprofit_block --factor skew60
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, "C:/QuantCore-master")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

from strategies.sp500_momentum import config as cfg                 # noqa: E402
from strategies.sp500_momentum import utils as sp_utils             # noqa: E402
from strategies.sp500_momentum.selection import SelectionEngine     # noqa: E402
from robustness.takeprofit_common import STUDY_END                  # noqa: E402
from robustness.takeprofit_arms import (                            # noqa: E402
    TakeProfitBacktester, FactorCache, verify_source_sync,
)
from robustness.utils import OUTPUT_ROOT                            # noqa: E402

OUT_DIR = Path("C:/QuantCore-master/reports/sp500_momentum")
OUT_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOTS_FP = OUTPUT_ROOT / "takeprofit" / "diagnostic_snapshots.csv"
RUN_DATE = "2026-07-11"          # 固定,避免跨日重跑改名
MIN_POOL = 30                    # 每折錨定池最低樣本數

spy = sp_utils.load_benchmark(cfg.ETF_DIR / "SPY.csv")
sso = sp_utils.load_benchmark(cfg.ETF_DIR / "SSO.csv")

START = pd.Timestamp("2018-07-01")
END   = pd.Timestamp(STUDY_END)
span = spy.index[(spy.index >= START) & (spy.index <= END)]
T = len(span)

selector = SelectionEngine()          # 共用 cache(跨視窗/variant/因子)
factor_cache = FactorCache(selector)  # 因子只依 (ticker, signal_date)

_snaps = pd.read_csv(SNAPSHOTS_FP, parse_dates=["date"])
_pool_all = _snaps[_snaps["bull_confirmed"] & (_snaps["pnl_pct"] > 0)]


def anchor_p90(factor: str, i0: pd.Timestamp, i1: pd.Timestamp) -> float | None:
    """IS 窗內重新錨定 P90(僅用 date ∈ [i0, i1] 的快照 — 無 lookahead)。"""
    vals = _pool_all.loc[
        (_pool_all["date"] >= i0) & (_pool_all["date"] <= i1), factor].dropna()
    if len(vals) < MIN_POOL:
        return None
    return float(np.percentile(vals, 90))


def realized_pf(trades) -> float:
    pnls = [t.get("PnL") for t in trades if t.get("PnL") is not None]
    pos = sum(p for p in pnls if p > 0)
    neg = abs(sum(p for p in pnls if p < 0))
    if neg == 0:
        return float("inf") if pos > 0 else 0.0
    return pos / neg


def run_window(block: bool, factor: str, threshold: float | None,
               d0: pd.Timestamp, d1: pd.Timestamp) -> dict:
    kwargs = dict(tp_mode=None)
    if block and threshold is not None:
        kwargs = dict(tp_mode="block_only", factor_key=factor,
                      threshold=threshold, factor_cache=factor_cache)
    bt = TakeProfitBacktester(
        start_date=str(d0.date()), end_date=str(d1.date()),
        initial_capital=cfg.INITIAL_CASH, compounding=True,
        selector=selector, spy_df=spy, sso_df=sso, write_reports=False,
        **kwargs)
    bt.run()
    eq = bt.get_equity_series()
    n_days = int(((span >= d0) & (span <= d1)).sum())
    if eq is None or eq.empty or len(eq) < 2:
        return dict(np_pct=0.0, sharpe=0.0, maxdd=0.0, pf=0.0,
                    days=n_days, trades=len(bt.trades), blocks=0)
    ret = eq.pct_change().dropna()
    sharpe = float(ret.mean() / ret.std() * np.sqrt(252)) if ret.std() > 0 else 0.0
    return dict(
        np_pct=float(eq.iloc[-1] / eq.iloc[0] - 1),
        sharpe=sharpe,
        maxdd=float((eq / eq.cummax() - 1).min()),
        pf=realized_pf(bt.trades),
        days=n_days,
        trades=len(bt.trades),
        blocks=len(bt.block_events),
    )


def make_folds(n: int):
    oos = T // (3 + n)
    is_ = 3 * oos
    offset = T - (3 + n) * oos        # 殘端丟最舊一頭,OOS 對齊最新資料
    folds = []
    for k in range(n):
        a = offset + k * oos
        folds.append((span[a], span[a + is_ - 1], span[a + is_], span[a + is_ + oos - 1]))
    return folds, is_, oos


def wf(n: int, tag: str, factor: str):
    folds, is_td, oos_td = make_folds(n)
    print(f"SETUP|{tag}|factor={factor}|T_td={T}|IS_td={is_td}(≈{is_td/252:.1f}y)"
          f"|OOS_td={oos_td}(≈{oos_td*365/252:.0f} cal d)|folds={n}|fits={n*2}",
          flush=True)
    rows = []
    for k, (i0, i1, o0, o1) in enumerate(folds, 1):
        th = anchor_p90(factor, i0, i1)
        try:
            a_is  = run_window(False, factor, None, i0, i1)
            a_oos = run_window(False, factor, None, o0, o1)
            b_is  = run_window(True,  factor, th,   i0, i1)
            b_oos = run_window(True,  factor, th,   o0, o1)
        except Exception as e:
            print(f"{tag}|k={k}|ERROR {e!r}", flush=True)
            continue
        winner = "B" if b_is["sharpe"] >= a_is["sharpe"] else "A"
        w_is, w_oos = (b_is, b_oos) if winner == "B" else (a_is, a_oos)
        rows.append(dict(
            fold=k, is_begin=i0.date(), is_end=i1.date(),
            oos_begin=o0.date(), oos_end=o1.date(),
            threshold=th if th is not None else np.nan, winner=winner,
            A_IS_np=a_is["np_pct"], A_IS_sharpe=a_is["sharpe"], A_IS_maxdd=a_is["maxdd"], A_IS_pf=a_is["pf"],
            B_IS_np=b_is["np_pct"], B_IS_sharpe=b_is["sharpe"], B_IS_maxdd=b_is["maxdd"], B_IS_pf=b_is["pf"],
            A_OOS_np=a_oos["np_pct"], A_OOS_sharpe=a_oos["sharpe"], A_OOS_maxdd=a_oos["maxdd"], A_OOS_pf=a_oos["pf"],
            B_OOS_np=b_oos["np_pct"], B_OOS_sharpe=b_oos["sharpe"], B_OOS_maxdd=b_oos["maxdd"], B_OOS_pf=b_oos["pf"],
            B_IS_blocks=b_is["blocks"], B_OOS_blocks=b_oos["blocks"],
            W_IS_np=w_is["np_pct"], W_IS_sharpe=w_is["sharpe"],
            W_OOS_np=w_oos["np_pct"], W_OOS_sharpe=w_oos["sharpe"],
            IS_days=is_td, OOS_days=oos_td,
        ))
        th_s = f"{th:.3f}" if th is not None else "NA"
        print(f"{tag}|k={k}|IS {i0.date()}..{i1.date()}|OOS {o0.date()}..{o1.date()}"
              f"|th={th_s}"
              f"|A_IS np={a_is['np_pct']:+.1%} sh={a_is['sharpe']:.2f}"
              f"|B_IS np={b_is['np_pct']:+.1%} sh={b_is['sharpe']:.2f}"
              f"|A_OOS np={a_oos['np_pct']:+.1%} sh={a_oos['sharpe']:.2f}"
              f"|B_OOS np={b_oos['np_pct']:+.1%} sh={b_oos['sharpe']:.2f}"
              f"|B_blocks IS/OOS={b_is['blocks']}/{b_oos['blocks']}"
              f"|winner={winner}", flush=True)

    df = pd.DataFrame(rows)

    def pardo(np_is_col, np_oos_col):
        is_rate = df[np_is_col].sum() / df["IS_days"].sum()
        oos_rate = df[np_oos_col].sum() / df["OOS_days"].sum()
        return oos_rate / is_rate if is_rate > 0 else float("nan")

    def sharpe_wfe(sh_is_col, sh_oos_col):
        m_is = df[sh_is_col].mean()
        return df[sh_oos_col].mean() / m_is if m_is > 0 else float("nan")

    pardo_chain = pardo("W_IS_np", "W_OOS_np")
    sharpe_chain = sharpe_wfe("W_IS_sharpe", "W_OOS_sharpe")
    agg = dict(
        pardo_chain=pardo_chain, sharpe_chain=sharpe_chain,
        pardo_A=pardo("A_IS_np", "A_OOS_np"), sharpe_A=sharpe_wfe("A_IS_sharpe", "A_OOS_sharpe"),
        pardo_B=pardo("B_IS_np", "B_OOS_np"), sharpe_B=sharpe_wfe("B_IS_sharpe", "B_OOS_sharpe"),
        is_wins_A=int((df.winner == "A").sum()), is_wins_B=int((df.winner == "B").sum()),
        oos_np_wins_A=int((df.A_OOS_np > df.B_OOS_np).sum()),
        oos_np_wins_B=int((df.B_OOS_np > df.A_OOS_np).sum()),
        oos_sh_wins_A=int((df.A_OOS_sharpe > df.B_OOS_sharpe).sum()),
        oos_sh_wins_B=int((df.B_OOS_sharpe > df.A_OOS_sharpe).sum()),
        sum_oos_np_A=float(df.A_OOS_np.sum()), sum_oos_np_B=float(df.B_OOS_np.sum()),
        chain_loss_folds=int((df.W_OOS_np < 0).sum()),
    )
    status = ("PASS" if pardo_chain > 0.50 and sharpe_chain > 0.40
              else "BORDERLINE" if pardo_chain > 0.30 else "FAIL")
    print(f"{tag}|AGG|pardo_chain={pardo_chain:.3f}|sharpe_chain={sharpe_chain:.3f}"
          f"|pardo_A={agg['pardo_A']:.3f}|sharpe_A={agg['sharpe_A']:.3f}"
          f"|pardo_B={agg['pardo_B']:.3f}|sharpe_B={agg['sharpe_B']:.3f}"
          f"|IS_wins A={agg['is_wins_A']} B={agg['is_wins_B']}"
          f"|OOS_np_wins A={agg['oos_np_wins_A']} B={agg['oos_np_wins_B']}"
          f"|OOS_sh_wins A={agg['oos_sh_wins_A']} B={agg['oos_sh_wins_B']}"
          f"|ΣOOS_np A={agg['sum_oos_np_A']:+.1%} B={agg['sum_oos_np_B']:+.1%}"
          f"|chain_loss_folds={agg['chain_loss_folds']}/{n}|STATUS={status}", flush=True)

    csv = OUT_DIR / f"v01_sp500mom_tpblock_{factor}_WF_N{n}_{status}_{RUN_DATE}.csv"
    df.to_csv(csv, index=False)
    print(f"{tag}|CSV|{csv}", flush=True)
    return agg


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--factor", default="kurt60", choices=["kurt60", "skew60"])
    args = ap.parse_args()

    verify_source_sync()
    # 上游 ADR-0011 flag 關閉:A/B 的 variant 封鎖由本腳本 block_only 機制
    # 提供,live flag 開著會讓 variant A(baseline)也被封鎖,A/B 失義。
    cfg.BLOWOFF_ENTRY_BLOCK = False
    wf(8, "WF8", args.factor)
    wf(7, "WF7", args.factor)
    print("DONE", flush=True)
