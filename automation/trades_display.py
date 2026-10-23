"""Recent Trades for the Panel (#150).

The trades come from `ledger.recent_trades` (#209); this module only turns them
into screen rows. `read_account_trades` is what the Panel calls: one account's
last 7 days, labelled with that account's Allocation (the Portfolio page
source passes the pair it just read, #231). `trade_line` formats one trade
as a row. Sector and inverse ETF trades are
labelled with the same rule as the Portfolio (`strategy_owner`, #187). No GUI
imports; colors come back as theme key names ("green", "red", "yellow", "text_dim").
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import ledger
from strategy_owner import SR, SR_AND_SKEWLS, STRATEGY_ETFS, trade_owner

EMPTY_TEXT = "No recent trades (7 days)"

STOCKS = "Stocks"
# 不是類股／反向 ETF 的成交：寫入的 Strategy → 畫面標籤。SR 只交易類股 ETF，個股
# 實際寫的是 SP500、EXIT、Sync 或舊版的「QuantCore Combo／SP500 Only」，都顯示 Stocks；
# 下面這幾個 SR 的寫法沿用舊版判讀（含 SR、不含 SP500／Combo → SR）。
_SR_WRITTEN = frozenset({SR, SR_AND_SKEWLS, "QuantCore SR Only"})


@dataclass(frozen=True)
class TradeLine:
    datetime: str
    time: str
    symbol: str
    action: str
    qty: str
    price: str
    strategy: str
    status: str
    action_color: str
    status_color: str

    def cells(self) -> tuple[str, ...]:
        return (self.time, self.symbol, self.action, self.qty, self.price,
                self.strategy, self.status)

    def colors(self) -> tuple[str, ...]:
        """Theme key of each cell, same order as cells()."""
        return ("text_dim", "text", self.action_color, "text", "text", "text_dim",
                self.status_color)


def strategy_label(trade: ledger.TradeRow,
                   alloc: tuple[float, float] | None = None) -> str:
    """畫面上的 Strategy。類股／反向 ETF 依帳戶 Allocation（#187）；其他依寫入的 Strategy。

    alloc: 帳戶的 (alloc_sr, alloc_skewls)；None = 不知道，類股 ETF 算 SR。
    """
    if trade.symbol in STRATEGY_ETFS:
        return trade_owner(trade.symbol, alloc)
    return SR if trade.strategy in _SR_WRITTEN else STOCKS


def trade_line(trade: ledger.TradeRow,
               alloc: tuple[float, float] | None = None) -> TradeLine:
    dt = trade.datetime
    fill_px = trade.fill_price
    return TradeLine(
        datetime=dt,
        time=dt[-8:] if len(dt) >= 8 else dt,
        symbol=trade.symbol,
        action=trade.action,
        qty=str(trade.qty),
        price=f"${fill_px:.2f}" if fill_px > 0 else "—",
        strategy=strategy_label(trade, alloc),
        status=f'{trade.status}{" [DRY]" if trade.dry_run else ""}',
        action_color="red" if trade.action == "SELL" else "green",
        status_color="yellow" if "DRY" in trade.status else "green",
    )


def read_account_trades(account_name: str, path: Path | None = None, *,
                        alloc: tuple[float, float] | None = None) -> list[TradeLine]:
    """某帳戶最近 7 天的成交畫面列，新到舊。讀不到紀錄當作沒有成交。

    alloc：帳戶的 (alloc_sr, alloc_skewls)；None = 讀不到設定（類股 ETF 照舊算 SR）。
    """
    try:
        trades = ledger.recent_trades(account_name, path=path)
    except Exception:
        return []
    return [trade_line(t, alloc) for t in trades]
