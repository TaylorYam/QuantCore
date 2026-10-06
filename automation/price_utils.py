"""
QuantCore — 參考價的兩個基本零件

- `ticker_price`：從一個 IB ticker 取有效價格（mid／last／close／延遲）。
- `load_csv_close`：本地 CSV 最後收盤價。

參考價的多層 fallback（本連線即時 → 報價 feed port → 延遲報價 → 本地 CSV）在
`broker.Connection.prices`（#185）。
"""

import numpy as np


def _ok(x) -> bool:
    return x is not None and np.isfinite(x) and x > 0


def ticker_price(t) -> float:
    """從 ticker 逐級取得有效價格（mid／last／close／延遲），全部無效回傳 0。"""
    mid = t.midpoint()
    if _ok(mid):
        return float(mid)
    if _ok(t.last):
        return float(t.last)
    if _ok(t.close):
        return float(t.close)
    for attr in ('delayedLast', 'delayedClose', 'delayedBid', 'delayedAsk'):
        v = getattr(t, attr, None)
        if _ok(v):
            return float(v)
    return 0.0


def load_csv_close(symbol: str) -> float:
    """從本地 CSV 取最後一筆收盤價，查不到回傳 0。"""
    try:
        import pandas as pd
        from strategies.sp500_momentum import config as sp_cfg
        from strategies.sp500_momentum.utils import stock_csv_path

        # ETF filenames are already their canonical symbols.  Stock files use
        # the provider naming contract (BRK.B -> BRK-B), including the class
        # shares opened by issue #52.
        paths = (
            sp_cfg.ETF_DIR / f"{symbol}.csv",
            stock_csv_path(sp_cfg.STOCK_DIR, symbol),
        )
        for f in paths:
            if f.exists():
                df = pd.read_csv(f, index_col=0, parse_dates=True)
                c = float(df['Close'].dropna().iloc[-1])
                if _ok(c):
                    return c
    except Exception:
        pass
    return 0.0
