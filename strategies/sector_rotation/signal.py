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
