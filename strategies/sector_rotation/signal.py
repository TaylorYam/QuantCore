"""
Sector Rotation MOM — 訊號計算
compute_mom_score   : 多週期加權動能分數
compute_mom_weights : Z-score 過濾 + 週再平衡權重
"""

import numpy as np
import pandas as pd
from . import config


# ── 內部工具 ──────────────────────────────────────────────────────────────────

def _rebal_mask(dates: pd.DatetimeIndex,
                weekday: int,
                freq_weeks: int = 1) -> np.ndarray:
    """True on every freq_weeks-th occurrence of weekday."""
    mask = np.zeros(len(dates), dtype=bool)
    occurrences = [i for i, d in enumerate(dates) if d.dayofweek == weekday]
    for j, idx in enumerate(occurrences):
        if j % freq_weeks == 0:
            mask[idx] = True
    return mask


# ── 公開 API ──────────────────────────────────────────────────────────────────

def compute_mom_score(etf_close: pd.DataFrame) -> pd.DataFrame:
    """
    多週期加權動能分數。
    score = Σ weight_lb × pct_change(lb)  for lb in MOM_LOOKBACKS
    """
    rets = {lb: etf_close.pct_change(lb) for lb in config.MOM_LOOKBACKS}
    return sum(w * rets[lb] for lb, w in config.MOM_LOOKBACKS.items())


def compute_mom_weights(
    etf_close: pd.DataFrame,
    zscore_thresh: float | None = None,
    rebal_weekday: int   | None = None,
    rebal_freq:    int   | None = None,
) -> pd.DataFrame:
    """
    Long-only 動能權重。

    流程
    ----
    1. 計算多週期加權動能分數
    2. 橫截面標準化（Z-score）
    3. 保留 Z > zscore_thresh 且原始分數 > 0 的 ETF
    4. 按分數比例分配，四捨五入至 WEIGHT_STEP，總和 ≤ 100%
    5. 訊號延遲一天（shift(1)）
    6. 只在再平衡日更新，其餘日 forward-fill

    Parameters
    ----------
    zscore_thresh : float，預設 config.MOM_ZSCORE_THRESH
    rebal_weekday : int，預設 config.REBAL_WEEKDAY
    rebal_freq    : int，預設 config.REBAL_FREQ

    Returns
    -------
    weights : DataFrame，shape = etf_close.shape，每列加總 ≤ 1
    """
    if zscore_thresh is None:
        zscore_thresh = config.MOM_ZSCORE_THRESH
    if rebal_weekday is None:
        rebal_weekday = config.REBAL_WEEKDAY
    if rebal_freq is None:
        rebal_freq = config.REBAL_FREQ

    score = compute_mom_score(etf_close)

    # 橫截面 Z-score
    cs_mean = score.mean(axis=1)
    cs_std  = score.std(axis=1).replace(0, np.nan)
    zscore  = score.sub(cs_mean, axis=0).div(cs_std, axis=0)

    # 過濾：只留 Z > threshold 且分數 > 0 的 ETF
    selected = score.where(zscore > zscore_thresh).clip(lower=0)
    row_sum  = selected.sum(axis=1).replace(0, np.nan)
    weights  = selected.div(row_sum, axis=0).fillna(0)

    # 四捨五入至 WEIGHT_STEP，總和強制 ≤ 100%
    step = config.WEIGHT_STEP
    weights = (weights / step).round() * step
    for i in range(len(weights)):
        row = weights.iloc[i].copy()
        while row.sum() > 1.0 + 1e-9:
            idx_max = row[row > 0].idxmax()
            row[idx_max] = max(row[idx_max] - step, 0.0)
        weights.iloc[i] = row

    # 訊號延遲一天
    weights = weights.shift(1).fillna(0)

    # 只在再平衡日更新，其餘 forward-fill
    is_rebal = _rebal_mask(weights.index, rebal_weekday, rebal_freq)
    wt = weights.copy()
    wt[~is_rebal] = np.nan
    wt = wt.ffill().fillna(0)

    return wt


def get_latest_weights(etf_close: pd.DataFrame, **kwargs) -> tuple[pd.Series, pd.Timestamp]:
    """
    回傳最新一筆目標權重（用於實盤參考）。

    Returns
    -------
    (weights_series, last_date)
    """
    wt = compute_mom_weights(etf_close, **kwargs)
    last = wt.index[-1]
    return wt.loc[last], last


def get_latest_weights_live(etf_close: pd.DataFrame,
                            zscore_thresh: float | None = None,
                            as_of_date: pd.Timestamp | None = None,
                            ) -> tuple[pd.Series, pd.Timestamp]:
    """
    Live 用：回傳「上一個排程調倉日當時應該使用的權重」。

    這保證：無論在哪一天執行 Sync Pos，都會得到跟上次 rebalance 排程
    完全相同的權重，所有帳戶（Rosa/Taylor/Yvonne）任何時間 sync
    都會得到一致的目標持倉，不會因為「哪天 sync」而有差異。

    邏輯
    ----
    1. 找到 `as_of_date` (預設=今天) ≤ 的最近一個排程調倉日（REBAL_WEEKDAY）
    2. 截斷 etf_close 到 `< last_rebal_day`（= 當時可用的資料）
    3. 直接用截斷後資料的最後一筆算 weight（不做 shift，
       因為截斷後的最後一筆本身就是 signal day 前一天的收盤）
    4. 等於「Rosa 在 last_rebal_day 凌晨排程執行時會算出的 weight」

    例：
      Rosa 2026-04-07 (Tue) 21:00 TW 執行 → data 到週一 → 用週一 weight
      Yvonne 2026-04-09 (Thu) sync      → 截斷到週一 → 也用週一 weight ✓

    Parameters
    ----------
    etf_close : ETF 收盤價
    zscore_thresh : Z-score 過濾門檻
    as_of_date : 以此日期作為「今天」，預設為真實今天

    Returns
    -------
    (weights_series, signal_date)
        signal_date = 用來計算訊號的實際最後一天
    """
    if zscore_thresh is None:
        zscore_thresh = config.MOM_ZSCORE_THRESH

    # ── 找上一個排程調倉日 ────────────────────────────────
    if as_of_date is None:
        as_of_date = pd.Timestamp.today().normalize()
    else:
        as_of_date = pd.Timestamp(as_of_date).normalize()

    rebal_weekday = config.REBAL_WEEKDAY  # e.g. 1 = Tuesday
    # 今天是週幾 - 調倉日是週幾（往回退幾天）
    days_back = (as_of_date.dayofweek - rebal_weekday) % 7
    last_rebal = as_of_date - pd.Timedelta(days=days_back)

    # ── 截斷資料到 < last_rebal（= 那天排程執行時可用的資料）──
    trimmed = etf_close[etf_close.index < last_rebal]
    if len(trimmed) == 0:
        # 資料不足（fallback 用全部資料）
        trimmed = etf_close

    # ── 計算 score / weight（與 compute_mom_score 一致）────
    score = compute_mom_score(trimmed)

    cs_mean = score.mean(axis=1)
    cs_std  = score.std(axis=1).replace(0, np.nan)
    zscore  = score.sub(cs_mean, axis=0).div(cs_std, axis=0)

    selected = score.where(zscore > zscore_thresh).clip(lower=0)
    row_sum  = selected.sum(axis=1).replace(0, np.nan)
    weights  = selected.div(row_sum, axis=0).fillna(0)

    step = config.WEIGHT_STEP
    weights = (weights / step).round() * step
    for i in range(len(weights)):
        row = weights.iloc[i].copy()
        while row.sum() > 1.0 + 1e-9:
            idx_max = row[row > 0].idxmax()
            row[idx_max] = max(row[idx_max] - step, 0.0)
        weights.iloc[i] = row

    # 直接取截斷後最後一筆（= last_rebal 前一個交易日的收盤訊號）
    # 不做 shift(1) —— 截斷已經達成「用前一天收盤」的效果
    last = weights.index[-1]
    return weights.loc[last], last


def get_rebal_schedule(
    dates: pd.DatetimeIndex,
    rebal_weekday: int | None = None,
    rebal_freq:    int | None = None,
) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """
    回傳 (上次再平衡日, 下次預計再平衡日)。
    """
    if rebal_weekday is None:
        rebal_weekday = config.REBAL_WEEKDAY
    if rebal_freq is None:
        rebal_freq = config.REBAL_FREQ

    mask = _rebal_mask(dates, rebal_weekday, rebal_freq)
    rebal_dates = dates[mask]
    if len(rebal_dates) == 0:
        return None, None
    last_rebal = rebal_dates[-1]
    next_rebal = last_rebal + pd.Timedelta(weeks=rebal_freq)
    return last_rebal, next_rebal
