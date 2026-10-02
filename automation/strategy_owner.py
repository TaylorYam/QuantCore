"""持倉歸屬到哪個 Strategy（#181）：新 Panel 的持倉表與每日快照共用同一套規則。

IB 只給代號與股數，不知道是哪個 Strategy 買的，所以依代號、方向與該帳戶的
Allocation 推測：

- 反向 ETF、做空的類股 ETF → SkewLS
- 做多的類股 ETF：SR 與 SkewLS 的多方腿都會買，看 Allocation 決定
  - 只有 SkewLS 開（alloc_sr = 0）→ SkewLS
  - 兩個都開 → 同一檔可能兩邊都有，標 SR+SkewLS（不拆股數）
  - 其他（只有 SR 開、或不知道 Allocation）→ SR

個股（SP500）與手動持倉不在這裡判斷，由呼叫端處理。
"""
from __future__ import annotations

from ib_fetcher import SKEWLS_INVERSE_TICKERS, SR_ETFS

SR, SKEWLS, SR_AND_SKEWLS = "SR", "SkewLS", "SR+SkewLS"


def etf_owner(symbol: str, shares: float,
              alloc: tuple[float, float] | None = None) -> str:
    """類股 ETF／反向 ETF 的歸屬；不是這兩類回傳空字串。

    alloc: 該帳戶的 (alloc_sr, alloc_skewls)；None = 不知道（舊快取等），
    做多的類股 ETF 維持算 SR。
    """
    if symbol in SKEWLS_INVERSE_TICKERS or (symbol in SR_ETFS and shares < 0):
        return SKEWLS
    if symbol not in SR_ETFS:
        return ""
    if alloc is None:
        return SR
    alloc_sr, alloc_skewls = alloc
    if alloc_skewls > 0 and alloc_sr > 0:
        return SR_AND_SKEWLS
    if alloc_skewls > 0:
        return SKEWLS
    return SR


def includes(owner: str, strategy: str) -> bool:
    """歸屬是否包含某個 Strategy（SR+SkewLS 兩個都算），給篩選用。"""
    return strategy in owner.split("+")
