"""
SP500 Momentum — 資料工具函數
資料格式已統一為 CSV：Date(index), Open, High, Low, Close, Volume
"""

import pandas as pd
from pathlib import Path


def load_stock(path: str | Path) -> pd.DataFrame | None:
    """
    讀取股票 CSV（標準格式：Date, Open, High, Low, Close, Volume）。
    回傳 DataFrame；找不到或錯誤回傳 None。
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, index_col="Date", parse_dates=True)
        df.index = pd.to_datetime(df.index).normalize()
        df = df[~df.index.duplicated(keep="last")].sort_index()
        if df.empty or "Close" not in df.columns:
            return None
        return df
    except Exception:
        return None


def load_benchmark(path: str | Path) -> pd.DataFrame | None:
    """
    讀取基準 ETF CSV（SPY, SSO 等）。
    回傳包含 Open, Close 的 DataFrame。
    """
    return load_stock(path)
