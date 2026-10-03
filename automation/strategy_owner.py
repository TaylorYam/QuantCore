"""持倉歸屬到哪個 Strategy（#181）：新 Panel 的持倉表、每日快照、Sync Pos 預覽與
最近成交共用同一套規則；類股 ETF／反向 ETF 清單也只在這裡取（#187）。

清單的唯一來源是策略設定：類股 ETF = `strategies/sector_rotation/config`，
反向 ETF = `strategies/sector_skew_ls/config`。

IB 只給代號與股數，不知道是哪個 Strategy 買的，所以依代號、方向與該帳戶的
Allocation 推測：

- 反向 ETF、做空的類股 ETF → SkewLS
- 做多的類股 ETF：SR 與 SkewLS 的多方腿都會買，看 Allocation 決定
  - 只有 SkewLS 開（alloc_sr = 0）→ SkewLS
  - 兩個都開 → 同一檔可能兩邊都有，標 SR+SkewLS（不拆股數）
  - 其他（只有 SR 開、或不知道 Allocation）→ SR

個股（SP500）與手動持倉不在這裡判斷，由呼叫端處理。決策層（Rebalance Plan）
只用這裡的清單判斷「是不是策略的 ETF」，不用顯示用的 SR／SkewLS 標籤。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT_DIR = Path(__file__).resolve().parent.parent
if str(_ROOT_DIR) not in sys.path:  # strategies 套件在 repo 根目錄
    sys.path.insert(0, str(_ROOT_DIR))

from strategies.sector_rotation import config as _sr_config  # noqa: E402
from strategies.sector_skew_ls import config as _skewls_config  # noqa: E402

SECTOR_ETFS: frozenset[str] = frozenset(_sr_config.SECTOR_TICKERS)
INVERSE_ETFS: frozenset[str] = frozenset(_skewls_config.INVERSE_TICKERS)
STRATEGY_ETFS: frozenset[str] = SECTOR_ETFS | INVERSE_ETFS

SR, SKEWLS, SR_AND_SKEWLS = "SR", "SkewLS", "SR+SkewLS"


def etf_owner(symbol: str, shares: float,
              alloc: tuple[float, float] | None = None) -> str:
    """類股 ETF／反向 ETF 的歸屬；不是這兩類回傳空字串。

    alloc: 該帳戶的 (alloc_sr, alloc_skewls)；None = 不知道（舊快取等），
    做多的類股 ETF 維持算 SR。
    """
    if symbol in INVERSE_ETFS or (symbol in SECTOR_ETFS and shares < 0):
        return SKEWLS
    if symbol not in SECTOR_ETFS:
        return ""
    if alloc is None:
        return SR
    alloc_sr, alloc_skewls = alloc
    if alloc_skewls > 0 and alloc_sr > 0:
        return SR_AND_SKEWLS
    if alloc_skewls > 0:
        return SKEWLS
    return SR


def trade_owner(symbol: str, alloc: tuple[float, float] | None = None) -> str:
    """一筆成交的歸屬。成交只有買賣方向、看不出部位是多是空，類股 ETF 一律
    依 Allocation 判斷（同做多的規則）。"""
    return etf_owner(symbol, 1, alloc)


def includes(owner: str, strategy: str) -> bool:
    """歸屬是否包含某個 Strategy（SR+SkewLS 兩個都算），給篩選用。"""
    return strategy in owner.split("+")
