"""
Sector Rotation MOM — 資料載入
從 C:/QuantCore-master/data/etfs/ 讀取 CSV，不依賴 yfinance 即時下載。
"""

import pandas as pd
from data import market_data
from data.price_semantics import total_return_ohlc
from . import config


def _load_ticker(ticker: str) -> pd.DataFrame:
    """單檔原始 OHLC；讀法與缺檔政策（raise）在 data/market_data，與 SkewLS 同一段（#229）。"""
    return market_data.require_bars(config.DATA_DIR, ticker)


def load_data() -> dict:
    """
    讀取所有板塊 ETF + SPY，回傳標準資料字典。

    Returns
    -------
    {
        "etf_close"  : DataFrame[SECTOR_TICKERS],
        "etf_open"   : DataFrame[SECTOR_TICKERS],
        "spy_close"  : Series,
        "spy_open"   : Series,
    }
    """
    tickers = config.SECTOR_TICKERS + [config.BENCHMARK_TICKER]

    close_dict, open_dict = {}, {}
    for tk in tickers:
        df = total_return_ohlc(_load_ticker(tk))
        close_dict[tk] = df["Close"]
        open_dict[tk]  = df["Open"]

    # 對齊共同日期
    close = pd.DataFrame(close_dict).dropna(how="all").sort_index()
    open_ = pd.DataFrame(open_dict).reindex(close.index).ffill()

    return {
        "etf_close": close[config.SECTOR_TICKERS],
        "etf_open":  open_[config.SECTOR_TICKERS],
        "spy_close": close[config.BENCHMARK_TICKER],
        "spy_open":  open_[config.BENCHMARK_TICKER],
    }
