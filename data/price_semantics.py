"""Price-field transformations shared by ETF strategy data loaders."""
from __future__ import annotations

import numpy as np
import pandas as pd


def extract_batched_history(history: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Extract a symbol from either yfinance MultiIndex column ordering."""
    if history.columns.nlevels != 2:
        return history.copy()
    outer = history.columns.get_level_values(0)
    inner = history.columns.get_level_values(1)
    if ticker in outer:
        return history[ticker].copy()
    if ticker in inner:
        return history.xs(ticker, axis=1, level=1).copy()
    raise KeyError(f"{ticker} missing from batched history")


def total_return_ohlc(frame: pd.DataFrame) -> pd.DataFrame:
    """Return dividend-adjusted OHLC while preserving other columns.

    ``Close`` and the OHLC fields are provider raw prices.  ``Adj Close`` is a
    total-return close.  Scale the day's Open/High/Low by Adj Close / Close so
    overnight and intraday returns use the same total-return price basis.
    For legacy files with partial adjusted-price coverage, retain the original
    Close/Open series for the entire file. Switching price basis mid-file would
    create a new artificial return discontinuity. A complete Adj Close column
    activates total-return semantics for the whole history.
    """
    result = frame.copy()
    if "Adj Close" not in result.columns:
        return result

    close = pd.to_numeric(result["Close"], errors="coerce")
    raw_adjusted = result["Adj Close"]
    adjusted = pd.to_numeric(raw_adjusted, errors="coerce")
    malformed = raw_adjusted.notna() & adjusted.isna()
    if malformed.any():
        dates = ", ".join(str(pd.Timestamp(d).date()) for d in result.index[malformed][:3])
        raise ValueError(f"invalid Adj Close value on {dates}")
    if adjusted.isna().any():
        return result
    invalid = (~np.isfinite(adjusted) | (adjusted <= 0) | ~np.isfinite(close)
               | (close <= 0) | close.isna())
    if invalid.any():
        dates = ", ".join(str(pd.Timestamp(d).date()) for d in result.index[invalid][:3])
        raise ValueError(f"invalid Adj Close/Close price pair on {dates}")

    factor = adjusted.div(close)
    for column in ("Open", "High", "Low"):
        if column in result:
            result[column] = pd.to_numeric(result[column], errors="coerce") * factor
    result["Close"] = adjusted
    return result
