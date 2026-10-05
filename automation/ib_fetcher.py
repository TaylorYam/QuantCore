"""
QuantCore — IB Gateway data-fetching helpers（S8 統一入口）

把散落在 rebalance_ib / daily_report / moo_fill_check / control_panel / sp500_daily_check
的 IB 連線與帳戶查詢 helper 收斂到單一模組。

核心函式
────────
連線：
  connect(ctx)                     → 經 Broker 開連線並記一行 log，回傳 broker.Connection
                                     （排程入口用；權益、持倉、報價、送單都在 Connection 上）

高階 wrapper（自己管理連線生命週期）：
  fetch_portfolio(ctx)             → 打開連線、抓 NAV+持倉+報價、分類 SR/SP500/其他，
                                     回傳完整 snapshot dict

所有函式皆為 thread-safe（依賴 immutable AccountCtx，不讀模組全域變數）。
"""

from __future__ import annotations

import logging
from datetime import date

from account_ctx import AccountCtx
from symbol_contract import contract_symbol

log = logging.getLogger(__name__)


# ── 常數：類股／反向 ETF 清單的唯一來源是 strategy_owner（取自策略設定，#187）──
# 舊名保留給既有呼叫端：SR_ETFS = 11 支類股 ETF、SKEWLS_INVERSE_TICKERS = 反向 ETF、
# ETF_TICKERS = 「ETF Holdings」bucket 涵蓋的全部（兩者合併）。
from strategy_owner import INVERSE_ETFS as SKEWLS_INVERSE_TICKERS  # noqa: E402
from strategy_owner import SECTOR_ETFS as SR_ETFS  # noqa: E402
from strategy_owner import STRATEGY_ETFS as ETF_TICKERS  # noqa: E402


# ── 低階：IB 連線 + 單項查詢 ────────────────────────────────────────────

def connect(ctx: AccountCtx, timeout: float = 20.0, *, readonly: bool = False):
    """經 Broker 連線指定帳戶的 IB Gateway，回傳 broker.Connection。"""
    from broker import Broker
    conn = Broker().connect(ctx, timeout=timeout, readonly=readonly)
    log.info(f"Connected  host={ctx.host} port={ctx.port} "
             f"clientId={ctx.client_id} account={ctx.account}")
    return conn


# ── 高階：完整 Portfolio snapshot（自己管連線）─────────────────────────

def fetch_portfolio(ctx: AccountCtx, *, readonly: bool = False) -> dict:
    """
    打開新連線、抓取完整 portfolio snapshot，自己 disconnect。

    回傳 dict 相容於舊 `control_panel.fetch_ib_portfolio` 的格式：
      date, nav, cushion, excess_liq,
      etf_holdings, stock_holdings, other_holdings,
      strategy_mktval, other_mktval, total_mktval, total_upnl,
      available, source, error?, debug
    """
    import numpy as np

    # SP500 universe 用於分類持倉歸屬（從 signals 單一入口取得）
    from signals import get_sp500_universe

    result = {
        "date": str(date.today()), "nav": 0, "cushion": 0, "excess_liq": 0,
        "etf_holdings": [], "stock_holdings": [], "other_holdings": [],
        "strategy_mktval": 0, "other_mktval": 0,
        "total_mktval": 0, "total_upnl": 0, "available": False,
        "source": f"IB Live [{ctx.name}]",
    }

    conn = None
    readings: dict = {}
    try:
        conn = connect(ctx, timeout=15, readonly=readonly)
        conn.portfolio_readings(readings)
        positions = readings["positions"]

        if positions:
            sp500_universe = get_sp500_universe()
            symbols = [contract_symbol(p.contract) for p in positions]
            prices = conn.prices(symbols)
            # 清理：0 不當作有效報價，讓 downstream .get(sym, avg_cost) fallback
            prices = {s: p for s, p in prices.items() if p > 0}

            for p in positions:
                sym = p.contract.symbol
                shares = int(p.position)
                avg_cost = float(p.avgCost)
                px = prices.get(sym, avg_cost)
                mkt_val = shares * px
                upnl = (px - avg_cost) * shares
                upnl_pct = (px / avg_cost - 1) * 100 if avg_cost > 0 else 0

                entry = {
                    "symbol": sym, "shares": shares,
                    "avg_cost": avg_cost, "mkt_px": px,
                    "mkt_val": mkt_val, "upnl": upnl, "upnl_pct": upnl_pct,
                    "name": getattr(p.contract, "longName", "") or "",
                }

                # ETF bucket 涵蓋 SR sector + SkewLS inverse (Issue 2 Option 2)
                if sym in ETF_TICKERS:
                    result["etf_holdings"].append(entry)
                    result["strategy_mktval"] += mkt_val
                elif sp500_universe and sym in sp500_universe:
                    result["stock_holdings"].append(entry)
                    result["strategy_mktval"] += mkt_val
                else:
                    result["other_holdings"].append(entry)
                    result["other_mktval"] += mkt_val
                result["total_mktval"] += mkt_val
                result["total_upnl"] += upnl

        # 4. 診斷資訊（協助除錯）
        result["debug"] = {
            "managed_accounts": list(readings["managed"]),
            "used_account": readings["account"],
            "n_positions_stk": len(positions),
            "port": ctx.port,
            "client_id": ctx.client_id,
        }
        result["available"] = True
    except Exception as e:
        result["error"] = str(e)
    finally:
        # 讀到哪就填到哪（例如權益讀到、cushion 讀不到時，權益照舊顯示）
        for key in ("nav", "cushion", "excess_liq"):
            if key in readings:
                result[key] = readings[key]
        if conn is not None:
            conn.close()

    return result
