"""
QuantCore — Portfolio 快照（#208）

一個帳戶此刻的權益、cushion、持倉（含報價、未實現損益）與分組，Panel 的 Portfolio 頁
與唯讀驗收工具用。`fetch_snapshot(ctx)` 經 Broker 開一條連線讀完就斷（兩者都用
readonly=True），回傳不可變的 `PortfolioSnapshot`；讀不到的部分照實留空並寫在 `error`，不丟例外。

分組只問決策層：持倉分類（類股 ETF／SP500 Universe／其他）的唯一實作是
`rebalance_plan.classify_holdings`，21:00 調倉、Sync Pos 用的是同一個函式。ETF 組 =
類股 ETF ＋ SkewLS 反向 ETF（策略持有的 ETF，清單來自 strategy_owner），其他 = 手動。

Panel 另外填的欄位（手動持倉碰撞、Allocation）由 `qt_panel.portfolio_source` 在建立時
用 `dataclasses.replace` 帶入，不就地修改。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from account_ctx import AccountCtx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Holding:
    symbol: str
    shares: int
    avg_cost: float
    mkt_px: float
    mkt_val: float
    upnl: float
    upnl_pct: float
    name: str = ""


@dataclass(frozen=True)
class PortfolioSnapshot:
    """一個帳戶的 Portfolio 快照；available=False 時讀數不完整，原因在 error。"""
    date: str
    source: str                                   # 例如 "IB Live [Taylor]"
    available: bool = False
    nav: float = 0
    cushion: float = 0                            # %
    excess_liq: float = 0
    etf_holdings: tuple[Holding, ...] = ()
    stock_holdings: tuple[Holding, ...] = ()
    other_holdings: tuple[Holding, ...] = ()
    strategy_mktval: float = 0
    other_mktval: float = 0
    total_mktval: float = 0
    total_upnl: float = 0
    error: str = ""
    debug: dict | None = None                     # 連線診斷（帳號、port、clientId…）
    # Panel 建立時帶入（qt_panel.portfolio_source）
    manual_symbols: dict = field(default_factory=dict)   # 碰撞的手動持倉：標的 → 原因
    manual_error: str = ""                        # 手動歸屬紀錄讀不到的原因
    strategy_alloc: tuple[float, float] | None = None    # (alloc_sr, alloc_skewls)

    @classmethod
    def unavailable(cls, error: str, source: str = "") -> "PortfolioSnapshot":
        return cls(date=str(date.today()), source=source, error=error)


def fetch_snapshot(ctx: AccountCtx, *, readonly: bool = False,
                   strategy_source=None) -> PortfolioSnapshot:
    """開一條連線讀這個帳戶的快照，讀完就斷。連不上或讀到一半失敗 → available=False，
    已讀到的權益／cushion 照樣填（例如權益讀到、持倉讀不到時權益照舊顯示）。

    strategy_source：分類持倉用的 [[Universe]] 從哪個 signals.StrategySource 取；
    None ＝ LiveSource。"""
    from broker import Broker, connected_line
    from rebalance_plan import classify_holdings
    from signals import LiveSource
    from strategy_owner import INVERSE_ETFS
    from symbol_contract import contract_symbol

    source = f"IB Live [{ctx.name}]"
    buckets: dict[str, list[Holding]] = {"etf": [], "stock": [], "other": []}
    totals = {"strategy": 0, "other": 0, "mktval": 0, "upnl": 0}
    readings: dict = {}
    error, debug, available = "", None, False
    conn = None
    try:
        conn = Broker().connect(ctx, timeout=15, readonly=readonly)
        log.info(connected_line(ctx))
        conn.portfolio_readings(readings)
        positions = readings["positions"]

        if positions:
            held = {p.contract.symbol: p.position for p in positions}
            split = classify_holdings(held, (strategy_source or LiveSource()).sp500_universe() or set())
            stocks = set(split.sp500)
            # 策略持有的 ETF = 類股 ETF ＋ 不在 SP500 組的 SkewLS 反向 ETF
            etfs = set(split.sr) | {s for s in held if s in INVERSE_ETFS and s not in stocks}
            prices = conn.prices([contract_symbol(p.contract) for p in positions])
            # 0 不當作有效報價：沒有報價時用成本價
            prices = {s: px for s, px in prices.items() if px > 0}

            for p in positions:
                sym = p.contract.symbol
                shares = int(p.position)
                avg_cost = float(p.avgCost)
                px = prices.get(sym, avg_cost)
                mkt_val = shares * px
                upnl = (px - avg_cost) * shares
                holding = Holding(
                    symbol=sym, shares=shares, avg_cost=avg_cost, mkt_px=px,
                    mkt_val=mkt_val, upnl=upnl,
                    upnl_pct=(px / avg_cost - 1) * 100 if avg_cost > 0 else 0,
                    name=getattr(p.contract, "longName", "") or "")
                if sym in etfs:
                    buckets["etf"].append(holding)
                    totals["strategy"] += mkt_val
                elif sym in stocks:
                    buckets["stock"].append(holding)
                    totals["strategy"] += mkt_val
                else:                                # split.other：策略外 = 手動
                    buckets["other"].append(holding)
                    totals["other"] += mkt_val
                totals["mktval"] += mkt_val
                totals["upnl"] += upnl

        debug = {
            "managed_accounts": list(readings["managed"]),
            "used_account": readings["account"],
            "n_positions_stk": len(positions),
            "port": ctx.port,
            "client_id": ctx.client_id,
        }
        available = True
    except Exception as e:
        error = str(e)
    finally:
        if conn is not None:
            conn.close()

    return PortfolioSnapshot(
        date=str(date.today()), source=source, available=available,
        nav=readings.get("nav", 0), cushion=readings.get("cushion", 0),
        excess_liq=readings.get("excess_liq", 0),
        etf_holdings=tuple(buckets["etf"]), stock_holdings=tuple(buckets["stock"]),
        other_holdings=tuple(buckets["other"]),
        strategy_mktval=totals["strategy"], other_mktval=totals["other"],
        total_mktval=totals["mktval"], total_upnl=totals["upnl"],
        error=error, debug=debug)
