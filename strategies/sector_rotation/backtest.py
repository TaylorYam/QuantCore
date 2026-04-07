"""
Sector Rotation MOM — 回測引擎
Overnight / intraday 執行模型：
  - 以昨日收盤訊號 → 今日開盤成交（overnight gap 算入持倉）
  - 當日開盤 → 收盤的 intraday 部分按新權重計算
"""

import numpy as np
import pandas as pd
from . import config
from .signal import compute_mom_weights


def run_backtest(
    weights:   pd.DataFrame,
    etf_close: pd.DataFrame,
    etf_open:  pd.DataFrame,
    tc:        float | None = None,
) -> pd.Series:
    """
    根據預計算的 weights 執行回測，回傳每日淨報酬率序列。

    Parameters
    ----------
    weights   : compute_mom_weights() 的輸出
    etf_close : 收盤價 DataFrame
    etf_open  : 開盤價 DataFrame
    tc        : 單向交易成本（預設 config.TRANSACTION_COST）

    Returns
    -------
    net_ret : Series，index = 交易日
    """
    if tc is None:
        tc = config.TRANSACTION_COST

    # overnight return：昨收 → 今開（持舊倉位）
    overnight = etf_open.div(etf_close.shift(1)) - 1
    # intraday return：今開 → 今收（持新倉位）
    intraday  = etf_close.div(etf_open) - 1

    w_prev    = weights.shift(1).fillna(0)
    gross_ret = (w_prev * overnight + weights * intraday).sum(axis=1)

    # 交易成本 = 單向成本 × 換手率
    turnover  = (weights - w_prev).abs().sum(axis=1)
    net_ret   = gross_ret - tc * turnover

    return net_ret.dropna()


def run_full_backtest(
    data:          dict,
    zscore_thresh: float | None = None,
    rebal_weekday: int   | None = None,
    rebal_freq:    int   | None = None,
    tc:            float | None = None,
) -> tuple[pd.Series, pd.DataFrame, pd.Series]:
    """
    一站式回測：載入資料 → 計算訊號 → 執行回測。

    Parameters
    ----------
    data : load_data() 的輸出

    Returns
    -------
    (net_ret, weights, spy_ret)
    """
    weights = compute_mom_weights(
        data["etf_close"],
        zscore_thresh=zscore_thresh,
        rebal_weekday=rebal_weekday,
        rebal_freq=rebal_freq,
    )
    net_ret = run_backtest(weights, data["etf_close"], data["etf_open"], tc=tc)
    spy_ret = data["spy_close"].pct_change().dropna()
    return net_ret, weights, spy_ret
