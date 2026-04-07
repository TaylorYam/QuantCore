"""
SP500 Momentum — 市場狀態判斷
根據 SPY 200MA 與 ATH 回撤判斷牛熊市。
"""

import pandas as pd
import numpy as np


class MarketRegime:
    def __init__(self, spy_df: pd.DataFrame, sso_df: pd.DataFrame):
        self.spy = spy_df.sort_index().copy()
        self.sso = sso_df.sort_index().copy()
        self._calculate_indicators()

    def _calculate_indicators(self):
        self.spy["MA200"]  = self.spy["Close"].rolling(200).mean()
        self.spy["ATH"]    = self.spy["Close"].cummax()
        self.spy["SPY_DD"] = (self.spy["Close"] - self.spy["ATH"]) / self.spy["ATH"]

    def get_state(self, date: pd.Timestamp) -> dict | None:
        """
        回傳指定日期的市場狀態。
        若日期不在索引中，取最近前一交易日。
        """
        if date not in self.spy.index:
            idx = self.spy.index.get_indexer([date], method="pad")[0]
            if idx == -1:
                return None
            row = self.spy.iloc[idx]
        else:
            row = self.spy.loc[date]

        return {
            "SPY_Close": row["Close"],
            "SPY_MA200": row["MA200"],
            "SPY_DD":    row["SPY_DD"],
            "SPY_ATH":   row["ATH"],
        }
