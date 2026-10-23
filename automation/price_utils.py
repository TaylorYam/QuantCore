"""
QuantCore — 參考價的兩個基本零件

- `ticker_price`：從一個 IB ticker 取有效價格（mid／last／close／延遲）。
- `load_csv_close`：本地 CSV 最後收盤價（讀法在 `data/market_data`）。

參考價的多層 fallback（本連線即時 → 報價 feed port → 延遲報價 → 本地 CSV）在
`broker.Connection.prices`（#185）。
"""

import logging
import sys
from pathlib import Path

import numpy as np

# load_csv_close 要 import repo 根目錄的 data 套件；排程從 automation/ 啟動時
# sys.path 沒有根目錄，不能靠呼叫端剛好先 import 了誰（#217）
_ROOT_DIR = Path(__file__).resolve().parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

log = logging.getLogger(__name__)


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
    """本地 CSV 最後一筆收盤；沒有檔、檔不能用或價格無效回傳 0（呼叫端回退 IB）。

    讀法、檔名與缺檔／壞檔的 log 在 data/market_data（#229）；#217 起讀不到不再靜默。
    """
    from data import market_data

    c = market_data.last_close(symbol)
    return float(c) if c is not None and _ok(c) else 0.0
