"""
Walk-Forward A/B — SP500 Momentum「EMA 進場過濾」(ADR-0005 候選)

Variant A = 無進場 EMA 過濾（HEAD 47e3115 行為，monkeypatch passthrough 重現）
Variant B = 進場 EMA 過濾（工作目錄現行 code：utils.filter_entry_eligible）

Pardo rolling WF：N=8 主跑 + N=7 敏感度，IS:OOS = 3:1，fitness = IS Sharpe。
共用一個 SelectionEngine（scan/metrics cache 跨視窗、跨 variant 重用 —
過濾發生在 scan 之後，不影響 cache 正確性）。不碰 git、不碰 live 設定。

輸出：
  stdout 標記行（SETUP|/WF8|/WF7|/VALID|）
  reports/sp500_momentum/v01_sp500mom_emaentry_WF_N{8,7}_<STATUS>_<date>.csv
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows console 預設 cp950
sys.path.insert(0, "C:/QuantCore-master")

import numpy as np
import pandas as pd

from strategies.sp500_momentum import config as cfg, utils
from strategies.sp500_momentum.selection import SelectionEngine
from strategies.sp500_momentum.backtester import SP500Backtester

OUT_DIR = Path("C:/QuantCore-master/reports/sp500_momentum")
OUT_DIR.mkdir(parents=True, exist_ok=True)
RUN_DATE = "2026-06-10"          # 由 caller 固定，避免跨日重跑改名

spy = utils.load_benchmark(cfg.ETF_DIR / "SPY.csv")
sso = utils.load_benchmark(cfg.ETF_DIR / "SSO.csv")

START = pd.Timestamp("2018-07-01")
span = spy.index[(spy.index >= START)]
T = len(span)

selector = SelectionEngine()      # 共用 cache

def set_variant(ema_filter_on: bool):
    """切換 config.ENTRY_EMA_FILTER（signals 與 backtester 都在 call time 讀此 flag）。"""
    cfg.ENTRY_EMA_FILTER = ema_filter_on


def realized_pf(trades) -> float:
    pnls = [t.get("PnL") for t in trades if t.get("PnL") is not None]
    pos = sum(p for p in pnls if p > 0)
    neg = abs(sum(p for p in pnls if p < 0))
    if neg == 0:
        return float("inf") if pos > 0 else 0.0
    return pos / neg


def run_window(ema_on: bool, d0: pd.Timestamp, d1: pd.Timestamp) -> dict:
    set_variant(ema_on)
    bt = SP500Backtester(
        start_date=str(d0.date()), end_date=str(d1.date()),
        initial_capital=cfg.INITIAL_CASH, compounding=True,
        selector=selector, spy_df=spy, sso_df=sso, write_reports=False,
    )
    bt.run()
    eq = bt.get_equity_series()
    n_days = int(((span >= d0) & (span <= d1)).sum())
    if eq is None or eq.empty or len(eq) < 2:
        return dict(np_pct=0.0, sharpe=0.0, maxdd=0.0, pf=0.0,
                    days=n_days, trades=len(bt.trades))
    ret = eq.pct_change().dropna()
    sharpe = float(ret.mean() / ret.std() * np.sqrt(252)) if ret.std() > 0 else 0.0
    return dict(
        np_pct=float(eq.iloc[-1] / eq.iloc[0] - 1),
        sharpe=sharpe,
        maxdd=float((eq / eq.cummax() - 1).min()),
        pf=realized_pf(bt.trades),
        days=n_days,
        trades=len(bt.trades),
    )


def make_folds(n: int):
    oos = T // (3 + n)
    is_ = 3 * oos
    offset = T - (3 + n) * oos        # 殘端丟在最舊的一頭，OOS 對齊最新資料
    folds = []
    for k in range(n):
        a = offset + k * oos
        folds.append((span[a], span[a + is_ - 1], span[a + is_], span[a + is_ + oos - 1]))
    return folds, is_, oos


def wf(n: int, tag: str):
    folds, is_td, oos_td = make_folds(n)
    print(f"SETUP|{tag}|T_td={T}|IS_td={is_td}(≈{is_td/252:.1f}y)|OOS_td={oos_td}"
          f"(≈{oos_td*365/252:.0f} cal d)|folds={n}|fits={n*2}", flush=True)
    rows = []
    for k, (i0, i1, o0, o1) in enumerate(folds, 1):
        try:
            a_is = run_window(False, i0, i1)
            a_oos = run_window(False, o0, o1)
            b_is = run_window(True, i0, i1)
            b_oos = run_window(True, o0, o1)
        except Exception as e:
            print(f"{tag}|k={k}|ERROR {e!r}", flush=True)
            continue
        winner = "B" if b_is["sharpe"] >= a_is["sharpe"] else "A"
        w_is, w_oos = (b_is, b_oos) if winner == "B" else (a_is, a_oos)
        rows.append(dict(
            fold=k, is_begin=i0.date(), is_end=i1.date(),
            oos_begin=o0.date(), oos_end=o1.date(), winner=winner,
            A_IS_np=a_is["np_pct"], A_IS_sharpe=a_is["sharpe"], A_IS_maxdd=a_is["maxdd"], A_IS_pf=a_is["pf"],
            B_IS_np=b_is["np_pct"], B_IS_sharpe=b_is["sharpe"], B_IS_maxdd=b_is["maxdd"], B_IS_pf=b_is["pf"],
            A_OOS_np=a_oos["np_pct"], A_OOS_sharpe=a_oos["sharpe"], A_OOS_maxdd=a_oos["maxdd"], A_OOS_pf=a_oos["pf"],
            B_OOS_np=b_oos["np_pct"], B_OOS_sharpe=b_oos["sharpe"], B_OOS_maxdd=b_oos["maxdd"], B_OOS_pf=b_oos["pf"],
            W_IS_np=w_is["np_pct"], W_IS_sharpe=w_is["sharpe"],
            W_OOS_np=w_oos["np_pct"], W_OOS_sharpe=w_oos["sharpe"],
            IS_days=is_td, OOS_days=oos_td,
        ))
        print(f"{tag}|k={k}|IS {i0.date()}..{i1.date()}|OOS {o0.date()}..{o1.date()}"
              f"|A_IS np={a_is['np_pct']:+.1%} sh={a_is['sharpe']:.2f}"
              f"|B_IS np={b_is['np_pct']:+.1%} sh={b_is['sharpe']:.2f}"
              f"|A_OOS np={a_oos['np_pct']:+.1%} sh={a_oos['sharpe']:.2f}"
              f"|B_OOS np={b_oos['np_pct']:+.1%} sh={b_oos['sharpe']:.2f}"
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

    csv = OUT_DIR / f"v01_sp500mom_emaentry_WF_N{n}_{status}_{RUN_DATE}.csv"
    df.to_csv(csv, index=False)
    print(f"{tag}|CSV|{csv}", flush=True)
    return agg


if __name__ == "__main__":
    wf(8, "WF8")
    wf(7, "WF7")
    # 等價驗證：flag-off 全期 ≈ 無過濾基準（資料截至當日）
    full = run_window(False, span[0], span[-1])
    print(f"VALID|A_full np={full['np_pct']:+.2%} → final=${cfg.INITIAL_CASH*(1+full['np_pct']):,.0f}"
          f"|trades={full['trades']}", flush=True)
    print("DONE", flush=True)
