"""
Sector Skew LS — 回測引擎
================================================
QCORE-style overnight / intraday 執行模型：
  - 以「昨日收盤」算出的訊號當作「今日開盤」的進場 target
  - Overnight (close(t-1) → open(t))：持舊倉位 weights.shift(1)
  - Intraday   (open(t)    → close(t))：持新倉位 weights[t]

訊號的 shift(1) + rebal schedule 已在 signal.compute_long_short_weights 內處理完。
backtest 端拿到的 weights[t] 就是「today's intraday position」可直接用。

代理版淨報酬 =   long_pnl   (買 sector ETF；ON@舊 + ID@新)
              + inv_pnl    (買對應 inverse ETF；ON@舊 + ID@新)
              + xlc_pnl    (直接放空清單；ON@舊 + ID@新)
              − tc_drag    (commission，模式依 COMMISSION_MODEL)

COMMISSION_MODEL：
  "per_trade" — 用 max($min, shares × $/share) 算每筆 commission，精確反映 IB
                 對小帳戶的 min/order 衝擊。需 ACCOUNT_CAPITAL 假設一個 base capital。
  "uniform"   — 用 TRANSACTION_COST × |Δw|.sum() 算 drag（legacy / sweep 比較快）。
"""

from __future__ import annotations
import numpy as np
import pandas as pd

from . import config
from .signal import (
    compute_long_short_weights,
    to_inverse_proxy_weights,
)


def _per_trade_commission_drag(
    now_combined:  pd.DataFrame,
    prev_combined: pd.DataFrame,
    prices_combined: pd.DataFrame,
    capital: float,
    cost_per_share: float,
    min_per_order: float,
) -> tuple[pd.Series, pd.Series]:
    """
    Per-trade commission with min/order。

    Logic
    -----
    對每個 (date, prefixed_ticker)：
        turnover_$ = |Δw| × capital
        shares     = turnover_$ / price
        commission_$ = max(min_per_order, shares × cost_per_share)  if shares > 0 else 0

    Returns
    -------
    (tc_drag_fraction, turnover_total_fraction)
        tc_drag_fraction  : Series, daily TC as fraction of capital
        turnover_total    : Series, daily Σ|Δw| (for reporting)
    """
    delta_w   = (now_combined - prev_combined).abs()
    turnover_total = delta_w.sum(axis=1)

    # Align prices to delta_w (forward-fill any gap)
    prices = prices_combined.reindex(delta_w.index).ffill()

    # Dollar value traded per ticker per day
    trade_dollars = delta_w * capital
    # Shares traded per ticker per day (avoid div-by-zero with replace)
    shares = trade_dollars / prices.replace(0, np.nan)
    shares = shares.fillna(0)

    # Commission per trade: max(min, shares × per_share). 0 if no trade.
    per_trade = np.maximum(min_per_order, shares * cost_per_share)
    no_trade  = shares < 1e-9
    per_trade = per_trade.where(~no_trade, 0)

    # Daily total commission ($) → drag fraction of capital
    daily_commission = per_trade.sum(axis=1)
    tc_drag = daily_commission / capital
    return tc_drag, turnover_total


def run_backtest_proxy(
    data: dict,
    tc: float | None = None,
    *,
    skew_windows: dict[int, float] | None = None,
    n_sides: int | None = None,
    vix_zscore_window: int | None = None,
    vix_zscore_thresh: float | None = None,
    alloc_highvol: tuple[float, float] | None = None,
    alloc_lowvol:  tuple[float, float] | None = None,
    rebal_freq:    str | None = None,
    rebal_weekdays: int | list[int] | None = None,
    rebal_threshold: float | None = None,
    rebal_every_n_weeks: int = 1,
    rebal_month_anchor: str | int = "first",
    # Commission model overrides
    commission_model: str | None = None,
    account_capital: float | None = None,
    cost_per_share: float | None = None,
    min_per_order:  float | None = None,
) -> dict:
    """
    執行反向 ETF 代理版回測。

    Parameters
    ----------
    data : data_loader.load_data() 的輸出 (需含 sector_open / inv_open / spy_open)
    tc   : 單向交易成本，預設 config.TRANSACTION_COST（僅 uniform 模式生效）
    commission_model : "per_trade" | "uniform"，None → config.COMMISSION_MODEL
        per_trade — 精確按每筆部位 size 算 max($min, shares × $/share)
        uniform   — TRANSACTION_COST × |Δw|.sum()  (legacy)
    account_capital  : per-trade 模式假設 capital，None → config.ACCOUNT_CAPITAL
    cost_per_share   : 每股 commission，None → config.COST_PER_SHARE
    min_per_order    : 每筆 min commission，None → config.MIN_PER_ORDER

    rebal_freq     : "daily" | "weekly" | "monthly" | "threshold"，None → config.REBAL_FREQ
    rebal_weekdays : weekly 模式：int / list[int]，0=Mon ... 4=Fri
    rebal_threshold: threshold 模式：合併 long+short 累積換手門檻
    rebal_every_n_weeks: weekly 模式幾週一次 (1=weekly, 2=biweekly, 4≈monthly)
    rebal_month_anchor : monthly 模式錨點 ("first"|"last"|int day-of-month)

    其他 kwarg 全部 None → 用 config 預設；任一個非 None 即為 sweep 覆寫。

    Returns
    -------
    dict {
        "net_ret", "gross_ret", "turnover", "tc_drag",
        "long_w", "inv_w", "xlc_short_w",
        "regime", "score",
    }
    """
    # Defaults from config
    if rebal_freq is None:
        rebal_freq = config.REBAL_FREQ
    if rebal_threshold is None and rebal_freq == "threshold":
        rebal_threshold = getattr(config, "REBAL_THRESHOLD", 0.40)
    if commission_model is None:
        commission_model = config.COMMISSION_MODEL
    if tc is None:
        tc = config.TRANSACTION_COST
    if account_capital is None:
        account_capital = config.ACCOUNT_CAPITAL
    if cost_per_share is None:
        cost_per_share = config.COST_PER_SHARE
    if min_per_order is None:
        min_per_order = config.MIN_PER_ORDER

    sector_close = data["sector_close"]
    sector_open  = data["sector_open"]
    inv_close    = data["inv_close"]
    inv_open     = data["inv_open"]
    vix_close    = data["vix_close"]

    # ── 1. 計算多空權重 (已 shift(1) + rebal schedule 處理完) ────────
    ls = compute_long_short_weights(
        sector_close, vix_close,
        skew_windows=skew_windows,
        n_sides=n_sides,
        vix_zscore_window=vix_zscore_window,
        vix_zscore_thresh=vix_zscore_thresh,
        alloc_highvol=alloc_highvol,
        alloc_lowvol=alloc_lowvol,
        rebal_freq=rebal_freq,
        rebal_weekdays=rebal_weekdays,
        rebal_threshold=rebal_threshold,
        rebal_every_n_weeks=rebal_every_n_weeks,
        rebal_month_anchor=rebal_month_anchor,
    )
    long_w  = ls["long_w"]
    short_w = ls["short_w"]

    # ── 2. 轉成反向 ETF 代理權重 ──────────────────────────────────
    proxy = to_inverse_proxy_weights(short_w, inv_columns=inv_close.columns.tolist())
    inv_w       = proxy["inv_w"]
    xlc_short_w = proxy["xlc_short_w"]

    # ── 3. Overnight / Intraday 報酬序列 ───────────────────────────
    sec_overnight = sector_open.div(sector_close.shift(1)) - 1
    sec_intraday  = sector_close.div(sector_open) - 1
    inv_overnight = inv_open.div(inv_close.shift(1)) - 1
    inv_intraday  = inv_close.div(inv_open) - 1
    xlc_overnight = sec_overnight[config.DIRECT_SHORT_TICKERS]
    xlc_intraday  = sec_intraday[config.DIRECT_SHORT_TICKERS]

    # ── 4. 各段 P&L (ON 用舊倉，ID 用新倉) ────────────────────────
    long_w_prev    = long_w.shift(1).fillna(0)
    inv_w_prev     = inv_w.shift(1).fillna(0)
    xlc_short_prev = xlc_short_w.shift(1).fillna(0)

    long_pnl = (long_w_prev      * sec_overnight + long_w      * sec_intraday).sum(axis=1)
    inv_pnl  = (inv_w_prev       * inv_overnight + inv_w       * inv_intraday).sum(axis=1)
    xlc_pnl  = (xlc_short_prev   * xlc_overnight + xlc_short_w * xlc_intraday).sum(axis=1)

    gross_ret = long_pnl + inv_pnl + xlc_pnl

    # ── 5. 交易成本：兩種模式 ────────────────────────────────────
    now_combined = pd.concat(
        [long_w.add_prefix("L_"),
         inv_w.add_prefix("I_"),
         xlc_short_w.add_prefix("S_")],
        axis=1,
    ).fillna(0)
    prev_combined = now_combined.shift(1).fillna(0)

    if commission_model == "per_trade":
        # 把對應的 price 也組成 prefixed DataFrame
        raw_sector_close = data.get("sector_raw_close", sector_close)
        raw_inv_close = data.get("inv_raw_close", inv_close)
        prices_long   = raw_sector_close[long_w.columns].rename(
            columns=lambda c: f"L_{c}")
        prices_inv    = raw_inv_close[inv_w.columns].rename(
            columns=lambda c: f"I_{c}")
        prices_direct = raw_sector_close[xlc_short_w.columns].rename(
            columns=lambda c: f"S_{c}")
        prices_combined = pd.concat(
            [prices_long, prices_inv, prices_direct], axis=1,
        )

        tc_drag, turnover = _per_trade_commission_drag(
            now_combined, prev_combined, prices_combined,
            capital=account_capital,
            cost_per_share=cost_per_share,
            min_per_order=min_per_order,
        )
    elif commission_model == "uniform":
        turnover = (now_combined - prev_combined).abs().sum(axis=1)
        tc_drag  = tc * turnover
    else:
        raise ValueError(f"Unknown commission_model: {commission_model!r}")

    net_ret = (gross_ret - tc_drag).dropna()

    return {
        "net_ret":     net_ret,
        "gross_ret":   gross_ret.reindex(net_ret.index),
        "turnover":    turnover.reindex(net_ret.index),
        "tc_drag":     tc_drag.reindex(net_ret.index),
        "long_w":      long_w,
        "inv_w":       inv_w,
        "xlc_short_w": xlc_short_w,
        "regime":      ls["regime"],
        "score":       ls["score"],
    }
