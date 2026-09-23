"""
Sector Skew LS — 資料載入
================================================
從 C:/QuantCore-master/data/etfs/*.csv 讀取所需的 23 檔，並對齊到共同
交易日（= 最晚發行日之後）。

公開 API：
    load_data() -> dict
"""

from __future__ import annotations
import pandas as pd

from data.price_semantics import total_return_ohlc
from . import config


def _load_one(local_name: str) -> pd.DataFrame:
    """讀取單檔 CSV，回傳 DataFrame（index=Date, columns=Open/Close/...）。"""
    path = config.DATA_DIR / f"{local_name}.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"找不到資料檔：{path}\n"
            f"請先執行 `python automation/update_data.py`"
        )
    df = pd.read_csv(path, index_col="Date", parse_dates=True)
    df.index = pd.to_datetime(df.index).normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def load_data() -> dict:
    """
    讀取所有所需的 ticker 資料並回傳對齊後的字典。

    對齊邏輯：以「所有 ticker 都有資料的第一日」為起點，
    這自動 = max(每個 ticker 的最早日期) = 最晚發行的 ETF 的上市日。

    Returns
    -------
    dict {
        "sector_close" : DataFrame[11]  - 11 SPDR 板塊 ETF 收盤
        "sector_open"  : DataFrame[11]
        "inv_close"    : DataFrame[10]  - 10 反向 ETF 收盤
        "inv_open"     : DataFrame[10]
        "spy_close"    : Series         - 基準
        "spy_open"     : Series
        "vix_close"    : Series         - 用於 regime
        "dates"        : DatetimeIndex  - 共同交易日
        "start_date"   : Timestamp      - 第一個共同交易日（= 最晚發行日）
    }
    """
    # ── 讀取所有 ticker ─────────────────────────────────────────────
    sector_dfs = {t: _load_one(t) for t in config.SECTOR_TICKERS}
    inv_dfs    = {t: _load_one(t) for t in config.INVERSE_TICKERS}
    spy_df     = _load_one(config.BENCHMARK_TICKER)
    vix_df     = _load_one(config.VIX_LOCAL_NAME)
    adjusted_sector_dfs = {t: total_return_ohlc(df) for t, df in sector_dfs.items()}
    adjusted_inv_dfs = {t: total_return_ohlc(df) for t, df in inv_dfs.items()}
    adjusted_spy_df = total_return_ohlc(spy_df)
    adjusted_vix_df = total_return_ohlc(vix_df)

    # ── 各自的最早日 ────────────────────────────────────────────────
    earliest = {}
    for t, df in sector_dfs.items():
        earliest[t] = df.index[0]
    for t, df in inv_dfs.items():
        earliest[t] = df.index[0]
    earliest[config.BENCHMARK_TICKER] = spy_df.index[0]
    earliest[config.VIX_LOCAL_NAME]   = vix_df.index[0]

    latest_inception = max(earliest.values())
    latest_inception_ticker = max(earliest, key=earliest.get)

    # ── 組成 DataFrame（以共同 dates 對齊）──────────────────────────
    sector_close = pd.DataFrame({t: df["Close"] for t, df in adjusted_sector_dfs.items()})
    sector_open  = pd.DataFrame({t: df["Open"]  for t, df in adjusted_sector_dfs.items()})
    sector_raw_close = pd.DataFrame({t: df["Close"] for t, df in sector_dfs.items()})
    inv_close    = pd.DataFrame({t: df["Close"] for t, df in adjusted_inv_dfs.items()})
    inv_open     = pd.DataFrame({t: df["Open"]  for t, df in adjusted_inv_dfs.items()})
    inv_raw_close = pd.DataFrame({t: df["Close"] for t, df in inv_dfs.items()})

    # SPY 為對齊基準（流動性最高、最完整）
    common = (sector_close.dropna(how="all")
                          .index
                          .intersection(adjusted_spy_df.index)
                          .intersection(vix_df.index)
                          .intersection(inv_close.dropna(how="all").index))
    common = common[common >= latest_inception].sort_values()

    if len(common) == 0:
        raise RuntimeError("找不到共同交易日，請檢查資料是否完整")

    sector_close = sector_close.reindex(common)
    sector_open  = sector_open.reindex(common)
    inv_close    = inv_close.reindex(common)
    inv_open     = inv_open.reindex(common)
    sector_raw_close = sector_raw_close.reindex(common)
    inv_raw_close = inv_raw_close.reindex(common)
    spy_close    = adjusted_spy_df["Close"].reindex(common)
    spy_open     = adjusted_spy_df["Open"].reindex(common)
    vix_close    = adjusted_vix_df["Close"].reindex(common)

    # 共同日期內仍有 NaN 表示該 ticker 那天無交易；前向填補（與 PDF 一致）
    sector_close = sector_close.ffill()
    sector_open  = sector_open.ffill()
    inv_close    = inv_close.ffill()
    inv_open     = inv_open.ffill()
    sector_raw_close = sector_raw_close.ffill()
    inv_raw_close = inv_raw_close.ffill()
    spy_close    = spy_close.ffill()
    spy_open     = spy_open.ffill()
    vix_close    = vix_close.ffill()

    return {
        "sector_close": sector_close[config.SECTOR_TICKERS],
        "sector_open":  sector_open[config.SECTOR_TICKERS],
        "inv_close":    inv_close[config.INVERSE_TICKERS],
        "inv_open":     inv_open[config.INVERSE_TICKERS],
        "sector_raw_close": sector_raw_close[config.SECTOR_TICKERS],
        "inv_raw_close": inv_raw_close[config.INVERSE_TICKERS],
        "spy_close":    spy_close,
        "spy_open":     spy_open,
        "vix_close":    vix_close,
        "dates":        common,
        "start_date":   common[0],
        "latest_inception_ticker": latest_inception_ticker,
    }


def summarize(data: dict) -> str:
    """回傳資料摘要字串（用於 backtest 開頭印表）。"""
    lines = [
        f"  共同交易日：{len(data['dates']):,} 個",
        f"  區間：{data['dates'][0].date()} ~ {data['dates'][-1].date()}",
        f"  最晚發行 ticker：{data['latest_inception_ticker']} "
        f"(@ {data['start_date'].date()})",
        f"  板塊 ETF ({len(config.SECTOR_TICKERS)}) : {' '.join(config.SECTOR_TICKERS)}",
        f"  反向 ETF ({len(config.INVERSE_TICKERS)}) : {' '.join(config.INVERSE_TICKERS)}",
    ]
    return "\n".join(lines)
