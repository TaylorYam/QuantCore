"""
Sector Skew LS — 訊號計算
================================================
- compute_skew_score        : 滾動報酬偏態加權合成
- compute_vix_regime        : VIX z-score → (long_alloc, short_alloc) per day
- compute_long_short_weights: 橫截面排名 → top N long / bottom N short，
                              權重 ∝ |score|，乘 regime allocation
                              **內含 shift(1) + rebal schedule**：
                                weights[t] = 用 close(t-1) 算的訊號，於 open(t) 進場
- to_inverse_proxy_weights  : 把 short_weights 轉成反向 ETF 多單 + XLC 直接放空

所有公開函式都接受 *可選* 的超參數覆寫 (None → 用 config 預設)，
專供 param_sweep 使用。預設行為與 config.py 完全一致。

權重慣例：
    weights[t] = 持有 open(t) → close(t) 的「日內」倉位（=昨收訊號）
    weights.shift(1)[t] = 持有 close(t-1) → open(t) 的「隔夜」倉位（=前昨收訊號）
    long_w[t, sector]   ≥ 0
    short_w[t, sector]  ≤ 0       (代理版會被 to_inverse_proxy_weights 拆解)
    proxy_long_w[t, sector]    ≥ 0
    proxy_inv_w[t, inverse]    ≥ 0
    proxy_xlc_short_w[t, "XLC"] ≤ 0
"""

from __future__ import annotations
import numpy as np
import pandas as pd

from . import config


# ─────────────────────────────────────────────────────────────────────────
# 1) 因子分數
# ─────────────────────────────────────────────────────────────────────────
def compute_skew_score(
    sector_close: pd.DataFrame,
    skew_windows: dict[int, float] | None = None,
) -> pd.DataFrame:
    """
    每個 sector 每日的合成偏態分數。

    score = Σ_w  w · skew(daily_ret, window=w)   for (w, weight) in skew_windows

    Parameters
    ----------
    sector_close : DataFrame[date, sector] 收盤價
    skew_windows : {window_days: weight}，None → 用 config.SKEW_WINDOWS

    Returns
    -------
    score : DataFrame[date, sector]
    """
    if skew_windows is None:
        skew_windows = config.SKEW_WINDOWS
    daily_ret = sector_close.pct_change()
    score = sum(
        w * daily_ret.rolling(window).skew()
        for window, w in skew_windows.items()
    )
    return score


# ─────────────────────────────────────────────────────────────────────────
# 2) VIX Regime
# ─────────────────────────────────────────────────────────────────────────
def compute_vix_regime(
    vix_close: pd.Series,
    zscore_window: int | None = None,
    zscore_thresh: float | None = None,
    alloc_highvol: tuple[float, float] | None = None,
    alloc_lowvol:  tuple[float, float] | None = None,
) -> pd.DataFrame:
    """
    根據 VIX 滾動 Z-score 決定每日的 (long_alloc, short_alloc)。

    z = (VIX - VIX.rolling(WIN).mean()) / VIX.rolling(WIN).std()
    z >  THRESH → ALLOC_HIGHVOL (預設 50/50)
    z <= THRESH → ALLOC_LOWVOL  (預設 70/30)

    所有超參數可由呼叫者覆寫 (None → 用 config 預設)。

    Returns
    -------
    DataFrame[date, ["long_alloc", "short_alloc", "vix_zscore", "high_vol"]]
    """
    if zscore_window is None:
        zscore_window = config.VIX_ZSCORE_WINDOW
    if zscore_thresh is None:
        zscore_thresh = config.VIX_ZSCORE_THRESH
    if alloc_highvol is None:
        alloc_highvol = config.ALLOC_HIGHVOL
    if alloc_lowvol is None:
        alloc_lowvol = config.ALLOC_LOWVOL

    roll_mean = vix_close.rolling(zscore_window).mean()
    roll_std  = vix_close.rolling(zscore_window).std().replace(0, np.nan)
    zscore    = (vix_close - roll_mean) / roll_std

    high_vol = zscore > zscore_thresh

    long_alloc  = pd.Series(
        np.where(high_vol, alloc_highvol[0], alloc_lowvol[0]),
        index=vix_close.index, name="long_alloc",
    )
    short_alloc = pd.Series(
        np.where(high_vol, alloc_highvol[1], alloc_lowvol[1]),
        index=vix_close.index, name="short_alloc",
    )

    return pd.DataFrame({
        "long_alloc":  long_alloc,
        "short_alloc": short_alloc,
        "vix_zscore":  zscore,
        "high_vol":    high_vol.astype(bool),
    })


# ─────────────────────────────────────────────────────────────────────────
# 3) Rebalance schedule (QCORE-style)
# ─────────────────────────────────────────────────────────────────────────
def _rebal_mask(
    dates: pd.DatetimeIndex,
    weekdays: int | list[int] | tuple[int, ...],
    every_n_weeks: int = 1,
) -> np.ndarray:
    """
    True on every n-th occurrence of any weekday in weekdays. (0=Mon ... 4=Fri)

    every_n_weeks:
      1 → 每週都換 (weekly)
      2 → 每兩週 (biweekly)
      4 → 約等於每月一次 (monthly-as-4-weeks)
    """
    if isinstance(weekdays, int):
        weekdays = [weekdays]
    mask = np.zeros(len(dates), dtype=bool)
    if every_n_weeks <= 0:
        every_n_weeks = 1

    for wd in weekdays:
        occurrences = [i for i, d in enumerate(dates) if d.dayofweek == wd]
        for j, idx in enumerate(occurrences):
            if j % every_n_weeks == 0:
                mask[idx] = True
    return mask


def _rebal_mask_monthly(
    dates: pd.DatetimeIndex,
    anchor: str | int,
) -> np.ndarray:
    """
    Calendar-month-aligned rebal mask。

    anchor:
      "first" → 每個月第一個交易日
      "last"  → 每個月最後一個交易日
      int N   → 每個月第一個 day_of_month >= N 的交易日；
                若該月沒有 (例如 N=31)，退化用月末
    """
    idx = pd.DatetimeIndex(dates)
    mask = np.zeros(len(idx), dtype=bool)
    grouper = pd.Series(np.arange(len(idx)), index=idx).groupby(
        [idx.year, idx.month],
    )

    if anchor == "first":
        for _, group in grouper:
            mask[int(group.iloc[0])] = True
    elif anchor == "last":
        for _, group in grouper:
            mask[int(group.iloc[-1])] = True
    elif isinstance(anchor, int):
        for _, group in grouper:
            picked = None
            for orig_i in group.values:
                if idx[orig_i].day >= anchor:
                    picked = int(orig_i)
                    break
            if picked is None:
                picked = int(group.iloc[-1])
            mask[picked] = True
    else:
        raise ValueError(f"Unknown month_anchor: {anchor!r}")

    return mask


def _apply_rebal_schedule(
    weights: pd.DataFrame,
    rebal_freq: str,
    rebal_weekdays: int | list[int] | None,
    rebal_every_n_weeks: int = 1,
    rebal_month_anchor: str | int = "first",
) -> pd.DataFrame:
    """
    Shift(1) + (optional) rebal-day mask + ffill。用於 "daily" / "weekly" / "monthly"。
    Threshold 模式因為要看 long+short 合併狀態，由 _apply_threshold_rebal 處理。

    Parameters
    ----------
    rebal_freq          : "daily" | "weekly" | "monthly"
    rebal_weekdays      : weekly 模式：int / list[int]，0=Mon ... 4=Fri
    rebal_every_n_weeks : weekly 模式：每 n 週換一次 (n=1 weekly, n=2 biweekly, n=4 ≈ monthly)
    rebal_month_anchor  : monthly 模式："first" / "last" / int (day-of-month)
    """
    w = weights.shift(1)

    if rebal_freq == "daily":
        return w.fillna(0)

    if rebal_freq == "weekly":
        if rebal_weekdays is None:
            rebal_weekdays = config.REBAL_WEEKDAY
        mask = _rebal_mask(w.index, rebal_weekdays,
                           every_n_weeks=rebal_every_n_weeks)
        w_masked = w.copy()
        w_masked.loc[~mask, :] = np.nan
        return w_masked.ffill().fillna(0)

    if rebal_freq == "monthly":
        mask = _rebal_mask_monthly(w.index, rebal_month_anchor)
        w_masked = w.copy()
        w_masked.loc[~mask, :] = np.nan
        return w_masked.ffill().fillna(0)

    raise ValueError(f"Unknown rebal_freq: {rebal_freq!r}")


def _apply_threshold_rebal(
    long_w_pre:  pd.DataFrame,
    short_w_pre: pd.DataFrame,
    threshold:   float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Threshold-based 再平衡：只在 |target − held|.sum() > threshold 時換倉，
    其他日持原倉位。Threshold 量是「合併 long + short 兩段的全組合換手」。

    target = shift(1) 後的訊號 (= 昨收訊號，今日 open 進場)。

    Parameters
    ----------
    long_w_pre  : raw 訊號 (close-based, 未 shift)
    short_w_pre : raw 訊號 (close-based, 未 shift)
    threshold   : 觸發 rebal 的最小累積換手 (= |target − held|.sum())

    Returns
    -------
    (long_w, short_w) : 對齊 long_w_pre.index、已套用 threshold schedule 的權重
    """
    long_target  = long_w_pre.shift(1).fillna(0)
    short_target = short_w_pre.shift(1).fillna(0)

    # 合併 long + short 做 joint threshold 判斷
    combined_target = pd.concat(
        [long_target.add_prefix("L_"),
         short_target.add_prefix("S_")],
        axis=1,
    )
    target_arr = combined_target.values
    n_rows, n_cols = target_arr.shape

    held_arr = np.zeros_like(target_arr)
    current = np.zeros(n_cols)

    eps = 1e-12
    for i in range(n_rows):
        delta = np.abs(target_arr[i] - current).sum()
        if delta > threshold + eps:
            current = target_arr[i].copy()
        held_arr[i] = current

    held_df = pd.DataFrame(held_arr,
                            index=combined_target.index,
                            columns=combined_target.columns)
    long_cols  = [c for c in held_df.columns if c.startswith("L_")]
    short_cols = [c for c in held_df.columns if c.startswith("S_")]
    long_w  = held_df[long_cols].rename(columns=lambda c: c[2:])
    short_w = held_df[short_cols].rename(columns=lambda c: c[2:])
    return long_w, short_w


# ─────────────────────────────────────────────────────────────────────────
# 4) 多空權重
# ─────────────────────────────────────────────────────────────────────────
def compute_long_short_weights(
    sector_close: pd.DataFrame,
    vix_close:    pd.Series,
    skew_windows: dict[int, float] | None = None,
    n_sides:      int | None = None,
    vix_zscore_window: int | None = None,
    vix_zscore_thresh: float | None = None,
    alloc_highvol: tuple[float, float] | None = None,
    alloc_lowvol:  tuple[float, float] | None = None,
    rebal_freq:    str = "daily",
    rebal_weekdays: int | list[int] | None = None,
    rebal_threshold: float | None = None,
    rebal_every_n_weeks: int = 1,
    rebal_month_anchor: str | int = "first",
) -> dict:
    """
    每日的多空權重（基於 raw sector ETF，非 inverse proxy）。

    流程
    ----
    1. 算偏態分數 (compute_skew_score)
    2. 橫截面排名：rank=1 → 最低偏態 (short)，rank=N → 最高偏態 (long)
    3. top n_sides long / bottom n_sides short
    4. 同側權重 ∝ |score| / Σ|score|，再乘 regime allocation
    5. **shift(1) + rebal schedule** (見 _apply_rebal_schedule)：
       - 訊號用 close(t) 算 → 套在 open(t+1) 進場
       - rebal_freq="weekly" 時，非 rebal 日 ffill 持倉不動

    全部超參數可由呼叫者覆寫 (None → 用 config 預設)。

    Returns
    -------
    dict {
        "long_w"  : DataFrame[date, sector] 多單權重 (≥ 0)，已 shift + rebal
        "short_w" : DataFrame[date, sector] 空單權重 (≤ 0)，已 shift + rebal
        "score"   : DataFrame[date, sector] 偏態分數 (raw, 未 shift)
        "regime"  : DataFrame[date, ...] (見 compute_vix_regime, raw)
    }
    """
    if n_sides is None:
        n_sides = config.N_SIDES

    score  = compute_skew_score(sector_close, skew_windows=skew_windows)
    regime = compute_vix_regime(
        vix_close.reindex(score.index),
        zscore_window=vix_zscore_window,
        zscore_thresh=vix_zscore_thresh,
        alloc_highvol=alloc_highvol,
        alloc_lowvol=alloc_lowvol,
    )

    n_etfs = score.shape[1]
    rank   = score.rank(axis=1, ascending=True)
    # rank=1 是最低 skew → short；rank=n 是最高 skew → long
    long_mask  = rank > (n_etfs - n_sides)
    short_mask = rank <= n_sides

    long_score  = score.where(long_mask)
    short_score = score.where(short_mask)

    # 同側內部權重 ∝ |score|
    long_abs_sum  = long_score.abs().sum(axis=1).replace(0, np.nan)
    short_abs_sum = short_score.abs().sum(axis=1).replace(0, np.nan)

    long_w_raw  = long_score.abs().div(long_abs_sum,  axis=0)
    # short 權重保留負號 (∝ -|score|/Σ|score|)
    short_w_raw = -short_score.abs().div(short_abs_sum, axis=0)

    # 乘上 regime allocation
    long_w_pre  = long_w_raw.mul(regime["long_alloc"],   axis=0).fillna(0)
    short_w_pre = short_w_raw.mul(regime["short_alloc"], axis=0).fillna(0)

    # Shift(1) + rebal schedule
    if rebal_freq == "threshold":
        if rebal_threshold is None:
            raise ValueError("rebal_freq='threshold' requires rebal_threshold (float)")
        long_w, short_w = _apply_threshold_rebal(
            long_w_pre, short_w_pre, rebal_threshold,
        )
    else:
        long_w  = _apply_rebal_schedule(
            long_w_pre,  rebal_freq, rebal_weekdays,
            rebal_every_n_weeks=rebal_every_n_weeks,
            rebal_month_anchor=rebal_month_anchor,
        )
        short_w = _apply_rebal_schedule(
            short_w_pre, rebal_freq, rebal_weekdays,
            rebal_every_n_weeks=rebal_every_n_weeks,
            rebal_month_anchor=rebal_month_anchor,
        )

    return {
        "long_w":  long_w,
        "short_w": short_w,
        "score":   score,
        "regime":  regime,
    }


# ─────────────────────────────────────────────────────────────────────────
# 5) 反向 ETF 代理轉換
# ─────────────────────────────────────────────────────────────────────────
def to_inverse_proxy_weights(
    short_w: pd.DataFrame,
    inv_columns: list[str] | None = None,
) -> dict:
    """
    把 sector 的空頭權重 (short_w, ≤0) 轉換為：
      - 反向 ETF 的多單權重 (inv_w, ≥0)，配合槓桿倍數縮放
      - XLC 維持直接放空 (xlc_short_w, ≤0)

    這是每日的 *線性轉換*，會繼承 short_w 的 shift+rebal schedule。

    轉換規則
    --------
    對每個 sector s in INVERSE_ETF:
        inv_ticker, leverage = INVERSE_ETF[s]
        inv_w[inv_ticker] += |short_w[s]| / leverage

    對每個 s in DIRECT_SHORT_TICKERS (= ["XLC"]):
        xlc_short_w[s] = short_w[s]   (原樣保留負權重)

    Parameters
    ----------
    short_w     : DataFrame[date, sector] 板塊空頭權重，≤ 0
    inv_columns : (optional) 反向 ETF 欄位清單，預設 config.INVERSE_TICKERS

    Returns
    -------
    dict {
        "inv_w"        : DataFrame[date, inverse_ticker] ≥ 0
        "xlc_short_w"  : DataFrame[date, "XLC"]          ≤ 0
    }
    """
    if inv_columns is None:
        inv_columns = config.INVERSE_TICKERS

    inv_w = pd.DataFrame(0.0, index=short_w.index, columns=inv_columns)

    for sector, (inv_ticker, lev) in config.INVERSE_ETF.items():
        if sector not in short_w.columns:
            continue
        if inv_ticker not in inv_columns:
            continue
        # short_w[sector] ≤ 0 → 取絕對值 / 槓桿 → 加到對應反向 ETF
        inv_w[inv_ticker] = inv_w[inv_ticker] + short_w[sector].abs() / lev

    # 直接放空的標的 (XLC)：保留原 short_w 不變
    xlc_short_w = short_w[config.DIRECT_SHORT_TICKERS].copy().fillna(0)

    return {
        "inv_w":       inv_w,
        "xlc_short_w": xlc_short_w,
    }


# ─────────────────────────────────────────────────────────────────────────
# 6) Live target weights（給 automation/signals 用）
# ─────────────────────────────────────────────────────────────────────────
def get_latest_weights_live(
    as_of_date: "pd.Timestamp | None" = None,
    skew_windows: dict[int, float] | None = None,
    n_sides: int | None = None,
    vix_zscore_window: int | None = None,
    vix_zscore_thresh: float | None = None,
    alloc_highvol: tuple[float, float] | None = None,
    alloc_lowvol:  tuple[float, float] | None = None,
) -> tuple[dict, pd.Timestamp]:
    """
    Live 目標：用最新可用資料 (截至 as_of_date，預設今天) 算出 ticker-level
    目標權重，**已套用 Inverse Proxy + XLC Direct Short 轉換**。

    **不套 threshold gating** — 純粹回傳「策略當前希望持有的 ticker-level 部位」。
    Threshold 判斷由 automation/rebalance_ib.py 對照 state file 進行 (S17/S18)。

    與 backtest 對齊邏輯
    ------------------
    backtest 端的 `compute_long_short_weights(rebal_freq='daily')` 內含 `shift(1)`，
    `weights[T]` = 用 close(T-1) 算的訊號，於 open(T) 進場。
    live 端我們有資料到 T-1 close (yesterday)，直接取最後一筆 close 算出
    `long_w_pre[T-1]`，這就是 backtest 中 shift(1) 後會放到 weights[T] 的同一個值。

    Returns
    -------
    (combined_weights, signal_date)
        combined_weights: dict[symbol, signed_weight]
            positive = long position (sector ETF 或 inverse ETF)
            negative = direct short (XLC)
            weights 已套用 regime allocation (gross sum = long_alloc + short_alloc/lev_avg)
        signal_date: 訊號計算所用的最後一筆 close 日期
    """
    from .data_loader import load_data

    data = load_data()

    # 截斷到 as_of_date 之前 (預設今天)
    if as_of_date is not None:
        as_of = pd.Timestamp(as_of_date).normalize()
        for key in ("sector_close", "sector_open", "inv_close", "inv_open"):
            data[key] = data[key][data[key].index <= as_of]
        for key in ("spy_close", "spy_open", "vix_close"):
            data[key] = data[key][data[key].index <= as_of]

    sector_close = data["sector_close"]
    vix_close    = data["vix_close"]

    if n_sides is None:
        n_sides = config.N_SIDES

    # 1) 偏態分數 (整序列；最後一筆是訊號)
    score = compute_skew_score(sector_close, skew_windows=skew_windows)

    # 2) Regime
    regime = compute_vix_regime(
        vix_close.reindex(score.index),
        zscore_window=vix_zscore_window,
        zscore_thresh=vix_zscore_thresh,
        alloc_highvol=alloc_highvol,
        alloc_lowvol=alloc_lowvol,
    )

    # 3) 取最後一筆做訊號 (= 用昨收算今天的目標)
    signal_date = score.index[-1]
    last_score  = score.iloc[-1]
    last_regime = regime.iloc[-1]

    long_alloc  = float(last_regime["long_alloc"])
    short_alloc = float(last_regime["short_alloc"])

    # 4) Top n_sides long / bottom n_sides short
    n_etfs = len(last_score)
    rank   = last_score.rank(ascending=True)
    long_mask  = rank > (n_etfs - n_sides)
    short_mask = rank <= n_sides

    long_score  = last_score.where(long_mask)
    short_score = last_score.where(short_mask)

    long_abs_sum  = long_score.abs().sum()
    short_abs_sum = short_score.abs().sum()

    if long_abs_sum <= 0 or short_abs_sum <= 0:
        # 訊號還沒成熟 (rolling window 不足) — 回傳空 dict 給 caller 判斷
        return {}, signal_date

    long_w_raw  = long_score.abs() / long_abs_sum
    short_w_raw = -short_score.abs() / short_abs_sum

    # 5) 套 regime allocation
    long_w  = (long_w_raw  * long_alloc ).fillna(0)
    short_w = (short_w_raw * short_alloc).fillna(0)

    # 6) Inverse proxy + XLC direct short
    combined: dict[str, float] = {}

    # 6a) Long sector ETF
    for sym, w in long_w.items():
        if abs(w) > 1e-9:
            combined[sym] = float(w)

    # 6b) Short → Inverse ETF long (除了 XLC)
    for sector, (inv_ticker, lev) in config.INVERSE_ETF.items():
        sw = float(short_w.get(sector, 0.0))
        if abs(sw) < 1e-9:
            continue
        combined[inv_ticker] = combined.get(inv_ticker, 0.0) + abs(sw) / lev

    # 6c) Direct short (XLC)
    for sym in config.DIRECT_SHORT_TICKERS:
        sw = float(short_w.get(sym, 0.0))
        if abs(sw) > 1e-9:
            combined[sym] = sw  # negative

    return combined, signal_date
