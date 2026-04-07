"""
QuantCore Robustness — 共用工具模組
封裝現有策略引擎，供所有穩健性分析腳本使用。

核心函數：
  - run_sr_backtest(...)       : 跑 Sector Rotation，回傳每日報酬 Series
  - run_sp500_backtest(...)    : 跑 SP500 Momentum，回傳每日報酬 Series
  - run_combo(...)             : 跑組合（80% SR + 50% SP500 = 1.3x 預設）
  - compute_metrics(...)       : 計算績效指標（CAGR, Sharpe, MaxDD, Calmar 等）
  - setup_dark_theme()         : 深色圖表主題
  - save_fig(...)              : 儲存圖表（PNG, DPI 150）

不修改 strategies/、backtest/、automation/ 下任何檔案。
"""

from __future__ import annotations

import sys
import io
import contextlib
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib as mpl

# 確保能 import QuantCore 主專案
PROJECT_ROOT = Path("C:/QuantCore-master")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

OUTPUT_ROOT = PROJECT_ROOT / "robustness" / "output"
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

# 時間戳
def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# ── 深色主題 ──────────────────────────────────────────────────────────────────

DARK_BG     = "#1e1e1e"
DARK_FG     = "#e0e0e0"
DARK_GRID   = "#3a3a3a"
ACCENT      = "#4db8ff"
ACCENT_2    = "#ff6b6b"
ACCENT_3    = "#ffd866"
ACCENT_4    = "#a9dc76"

def setup_dark_theme():
    """套用統一深色主題。"""
    mpl.rcParams.update({
        "figure.facecolor":  DARK_BG,
        "axes.facecolor":    DARK_BG,
        "savefig.facecolor": DARK_BG,
        "axes.edgecolor":    DARK_FG,
        "axes.labelcolor":   DARK_FG,
        "axes.titlecolor":   DARK_FG,
        "xtick.color":       DARK_FG,
        "ytick.color":       DARK_FG,
        "text.color":        DARK_FG,
        "grid.color":        DARK_GRID,
        "grid.alpha":        0.4,
        "axes.grid":         True,
        "figure.dpi":        100,
        "savefig.dpi":       150,
        "font.size":         10,
    })


def save_fig(fig, path: str | Path, dpi: int = 150):
    """儲存圖表並關閉。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=dpi, bbox_inches="tight", facecolor=DARK_BG)
    plt.close(fig)


# ── 靜音輔助 ──────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def suppress_stdout(enabled: bool = True):
    """抑制 stdout。"""
    if not enabled:
        yield
        return
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        yield
    finally:
        sys.stdout = old


# ── SR 策略回測（帶參數覆寫） ─────────────────────────────────────────────────

def run_sr_backtest(
    start_date: str | None = None,
    end_date:   str | None = None,
    mom_lookbacks: dict | None = None,
    zscore_thresh: float | None = None,
    rebal_weekday: int | None = None,
    rebal_freq:    int | None = None,
    tc: float | None = None,
    quiet: bool = True,
) -> pd.Series:
    """
    跑 Sector Rotation MOM，支援覆寫主要參數。

    Parameters
    ----------
    mom_lookbacks : 例如 {60: 0.1, 20: 0.2, 10: 0.3, 5: 0.4}
                    None 則用預設
    zscore_thresh : Z-score 門檻（基準 0.4）
    rebal_weekday : 再平衡星期（基準 1）
    rebal_freq    : 再平衡週頻率（基準 1）
    tc            : 單向交易成本（基準 0.00075）
    """
    from strategies.sector_rotation import config as sr_cfg
    from strategies.sector_rotation.data_loader import load_data
    from strategies.sector_rotation.signal   import compute_mom_weights
    from strategies.sector_rotation.backtest import run_backtest

    # 暫時覆寫 MOM_LOOKBACKS（signal.py 直接從 config 讀）
    original_lookbacks = sr_cfg.MOM_LOOKBACKS
    if mom_lookbacks is not None:
        sr_cfg.MOM_LOOKBACKS = mom_lookbacks

    try:
        with suppress_stdout(quiet):
            data = load_data()

            # 日期截取
            if start_date or end_date:
                sd = pd.to_datetime(start_date) if start_date else None
                ed = pd.to_datetime(end_date)   if end_date   else None
                for key in ("etf_close", "etf_open"):
                    df = data[key]
                    if sd is not None: df = df[df.index >= sd]
                    if ed is not None: df = df[df.index <= ed]
                    data[key] = df

            weights = compute_mom_weights(
                data["etf_close"],
                zscore_thresh=zscore_thresh,
                rebal_weekday=rebal_weekday,
                rebal_freq=rebal_freq,
            )
            net_ret = run_backtest(
                weights, data["etf_close"], data["etf_open"], tc=tc
            )
            net_ret.name = "SR"
            return net_ret
    finally:
        sr_cfg.MOM_LOOKBACKS = original_lookbacks


# ── SP500 策略回測（帶參數覆寫） ──────────────────────────────────────────────

_SP500_SELECTOR_CACHE = None

def _get_sp500_selector():
    """共用 SelectionEngine，避免重複載入成分股資料。"""
    global _SP500_SELECTOR_CACHE
    if _SP500_SELECTOR_CACHE is None:
        from strategies.sp500_momentum.selection import SelectionEngine
        _SP500_SELECTOR_CACHE = SelectionEngine()
    return _SP500_SELECTOR_CACHE


def run_sp500_backtest(
    start_date: str | None = None,
    end_date:   str | None = None,
    lookback:   int | None = None,
    target_holdings: int | None = None,
    stop_loss_pct: float | None = None,
    exit_ema:   int | None = None,
    sell_rank_threshold: int | None = None,
    max_adj_slope: float | None = None,
    commission: float | None = None,
    reuse_selector: bool = True,
    quiet: bool = True,
) -> pd.Series:
    """
    跑 SP500 Momentum，支援覆寫主要參數。

    Parameters
    ----------
    lookback              : 動能週期（基準 90）
    target_holdings       : 持股數（基準 4）
    stop_loss_pct         : 停損比例（基準 0.20）
    exit_ema              : EMA 週期（基準 50）
    sell_rank_threshold   : Rank buffer（基準 20）
    max_adj_slope         : 過熱保護（基準 1.5）
    commission            : 單向佣金（基準 0.00075）
    reuse_selector        : 是否重用全域 selector（加速）

    Returns
    -------
    每日報酬率 Series
    """
    from strategies.sp500_momentum import config as sp_cfg
    from strategies.sp500_momentum.backtester import SP500Backtester
    from strategies.sp500_momentum.selection import SelectionEngine
    from strategies.sp500_momentum import utils as sp_utils

    # 暫時覆寫 config
    orig = {
        "LOOKBACK":              sp_cfg.LOOKBACK,
        "LOOKBACK_ENTRY":        sp_cfg.LOOKBACK_ENTRY,
        "LOOKBACK_EXIT":         sp_cfg.LOOKBACK_EXIT,
        "TARGET_HOLDINGS":       sp_cfg.TARGET_HOLDINGS,
        "STOP_LOSS_PCT":         sp_cfg.STOP_LOSS_PCT,
        "EXIT_EMA":              sp_cfg.EXIT_EMA,
        "SELL_RANK_THRESHOLD":   sp_cfg.SELL_RANK_THRESHOLD,
        "MAX_ADJ_SLOPE":         sp_cfg.MAX_ADJ_SLOPE,
        "COMMISSION":            sp_cfg.COMMISSION,
    }

    try:
        if lookback is not None:
            sp_cfg.LOOKBACK       = lookback
            sp_cfg.LOOKBACK_ENTRY = lookback
            sp_cfg.LOOKBACK_EXIT  = lookback
        if target_holdings is not None:
            sp_cfg.TARGET_HOLDINGS = target_holdings
        if stop_loss_pct is not None:
            sp_cfg.STOP_LOSS_PCT = stop_loss_pct
        if exit_ema is not None:
            sp_cfg.EXIT_EMA = exit_ema
        if sell_rank_threshold is not None:
            sp_cfg.SELL_RANK_THRESHOLD = sell_rank_threshold
        if max_adj_slope is not None:
            sp_cfg.MAX_ADJ_SLOPE = max_adj_slope
        if commission is not None:
            sp_cfg.COMMISSION = commission

        # 若 EMA period 改了，須重載 selector（否則預計算的 EMA 不對）
        ema_changed = (exit_ema is not None and exit_ema not in SelectionEngine.PRECOMPUTED_EMA_PERIODS)

        with suppress_stdout(quiet):
            spy_df = sp_utils.load_benchmark(sp_cfg.ETF_DIR / "SPY.csv")
            sso_df = sp_utils.load_benchmark(sp_cfg.ETF_DIR / "SSO.csv")

            if reuse_selector and not ema_changed:
                selector = _get_sp500_selector()
            else:
                selector = SelectionEngine()
                if ema_changed and exit_ema is not None:
                    # 把新 EMA 週期加到預計算列表
                    SelectionEngine.PRECOMPUTED_EMA_PERIODS = list(set(
                        SelectionEngine.PRECOMPUTED_EMA_PERIODS + [exit_ema]
                    ))
                    selector = SelectionEngine()

            bt = SP500Backtester(
                start_date=start_date,
                end_date=end_date,
                initial_capital=sp_cfg.INITIAL_CASH,
                compounding=True,
                selector=selector,
                spy_df=spy_df,
                sso_df=sso_df,
                write_reports=False,
            )
            bt.run()

            eq = bt.get_equity_series()
            ret = eq.pct_change().dropna()
            ret.name = "SP500"
            return ret
    finally:
        for k, v in orig.items():
            setattr(sp_cfg, k, v)


# ── 組合（帶槓桿） ────────────────────────────────────────────────────────────

def run_combo(
    ret_sr: pd.Series | None = None,
    ret_sp: pd.Series | None = None,
    alloc_sr: float = 0.8,
    alloc_sp: float = 0.5,
    start_date: str | None = None,
    end_date:   str | None = None,
    initial_equity: float = 1_000_000,
    quiet: bool = True,
) -> pd.Series:
    """
    跑組合策略（預設 80% SR + 50% SP500 = 1.3x 槓桿）。
    """
    from backtest.combo import combine_monthly

    if ret_sr is None:
        ret_sr = run_sr_backtest(start_date, end_date, quiet=quiet)
    if ret_sp is None:
        ret_sp = run_sp500_backtest(start_date, end_date, quiet=quiet)

    common = ret_sr.index.intersection(ret_sp.index)
    ret_sr = ret_sr.loc[common]
    ret_sp = ret_sp.loc[common]

    combo = combine_monthly(
        ret_sr, ret_sp, alloc_sr=alloc_sr, alloc_sp=alloc_sp,
        initial_equity=initial_equity,
    )
    return combo


# ── SPY 基準 ──────────────────────────────────────────────────────────────────

def load_spy_returns(start_date: str | None = None,
                     end_date:   str | None = None) -> pd.Series:
    """載入 SPY 基準的每日報酬率。"""
    from strategies.sector_rotation.data_loader import load_data
    data = load_data()
    spy = data["spy_close"]
    if start_date:
        spy = spy[spy.index >= pd.to_datetime(start_date)]
    if end_date:
        spy = spy[spy.index <= pd.to_datetime(end_date)]
    ret = spy.pct_change().dropna()
    ret.name = "SPY"
    return ret


# ── 績效指標 ──────────────────────────────────────────────────────────────────

def compute_metrics(ret: pd.Series, ann: int = 252) -> dict:
    """
    計算常用績效指標。

    Returns
    -------
    dict with keys:
      start, end, days, total_ret, ann_ret, ann_vol, sharpe, sortino,
      calmar, max_dd, max_dd_duration, win_rate_m, best_month, worst_month
    """
    ret = ret.dropna()
    if len(ret) == 0:
        return {}

    n      = len(ret)
    cum    = (1 + ret).cumprod()
    total  = cum.iloc[-1] - 1
    ann_ret = (1 + ret).prod() ** (ann / n) - 1
    ann_vol = ret.std() * np.sqrt(ann)
    sharpe  = ann_ret / ann_vol if ann_vol > 0 else np.nan

    # Drawdown
    dd      = cum / cum.cummax() - 1
    max_dd  = dd.min()
    calmar  = ann_ret / abs(max_dd) if max_dd != 0 else np.nan

    # Drawdown duration（最長連續處於回撤的天數）
    in_dd        = dd < 0
    max_dd_dur   = 0
    cur          = 0
    for v in in_dd.values:
        if v:
            cur += 1
            max_dd_dur = max(max_dd_dur, cur)
        else:
            cur = 0

    # Sortino
    down      = ret[ret < 0]
    down_vol  = down.std() * np.sqrt(ann) if len(down) > 0 else np.nan
    sortino   = ann_ret / down_vol if (down_vol and down_vol > 0) else np.nan

    # Monthly
    monthly     = (1 + ret).resample("ME").prod() - 1
    win_rate_m  = (monthly > 0).mean()
    best_month  = monthly.max() if len(monthly) else np.nan
    worst_month = monthly.min() if len(monthly) else np.nan

    return {
        "start":           ret.index[0].date().isoformat(),
        "end":             ret.index[-1].date().isoformat(),
        "days":            n,
        "total_ret":       total,
        "ann_ret":         ann_ret,
        "ann_vol":         ann_vol,
        "sharpe":          sharpe,
        "sortino":         sortino,
        "calmar":          calmar,
        "max_dd":          max_dd,
        "max_dd_duration": max_dd_dur,
        "win_rate_m":      win_rate_m,
        "best_month":      best_month,
        "worst_month":     worst_month,
    }


def equity_curve(ret: pd.Series, initial: float = 1.0) -> pd.Series:
    """從報酬率序列計算權益曲線。"""
    return initial * (1 + ret).cumprod()


def drawdown(ret: pd.Series) -> pd.Series:
    """計算回撤曲線。"""
    cum = (1 + ret).cumprod()
    return cum / cum.cummax() - 1


# ── 輸出路徑 ──────────────────────────────────────────────────────────────────

def out_path(subdir: str, filename: str) -> Path:
    """取得輸出檔案完整路徑（含 timestamp 目錄建立）。"""
    p = OUTPUT_ROOT / subdir / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ── 自我測試 ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("Robustness Utils — 自我測試")
    print("=" * 60)

    setup_dark_theme()

    print("\n[1/3] 測試 SR 回測...")
    ret_sr = run_sr_backtest(quiet=True)
    print(f"  SR: {len(ret_sr)} 天, 區間 {ret_sr.index[0].date()} → {ret_sr.index[-1].date()}")
    m_sr = compute_metrics(ret_sr)
    print(f"       CAGR {m_sr['ann_ret']:+.1%}  Sharpe {m_sr['sharpe']:.2f}  MaxDD {m_sr['max_dd']:.1%}")

    print("\n[2/3] 測試 SP500 回測...")
    ret_sp = run_sp500_backtest(quiet=True)
    print(f"  SP500: {len(ret_sp)} 天, 區間 {ret_sp.index[0].date()} → {ret_sp.index[-1].date()}")
    m_sp = compute_metrics(ret_sp)
    print(f"          CAGR {m_sp['ann_ret']:+.1%}  Sharpe {m_sp['sharpe']:.2f}  MaxDD {m_sp['max_dd']:.1%}")

    print("\n[3/3] 測試組合 (80% SR + 50% SP500 = 1.3x)...")
    combo = run_combo(ret_sr=ret_sr, ret_sp=ret_sp, alloc_sr=0.8, alloc_sp=0.5)
    m_c = compute_metrics(combo)
    print(f"  Combo: CAGR {m_c['ann_ret']:+.1%}  Sharpe {m_c['sharpe']:.2f}  MaxDD {m_c['max_dd']:.1%}")

    print("\nAll checks passed.")
