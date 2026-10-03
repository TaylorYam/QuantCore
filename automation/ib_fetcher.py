"""
QuantCore — IB Gateway data-fetching helpers（S8 統一入口）

把散落在 rebalance_ib / daily_report / moo_fill_check / control_panel / sp500_daily_check
的 IB 連線與帳戶查詢 helper 收斂到單一模組。

核心函式
────────
連線：
  connect(ctx)                     → 經 Broker 開連線並記一行 log，回傳 broker.Connection
                                     （排程入口用；讀數與送單都在 Connection 上）

低階 helper（Connection 的讀數實作，需要 ib_insync 本體）：
  get_nav(ib, ctx)                 → 取得 NAV（USD）
  get_account_info(ib, ctx)        → 取得 NAV + cushion + excess_liq + maint_margin
  get_positions(ib, ctx)           → {symbol: qty} dict
  get_positions_detail(ib, ctx)    → list[Position]
  get_prices(ib, symbols, ctx)     → {symbol: price}，多層 fallback

高階 wrapper（自己管理連線生命週期）：
  fetch_portfolio(ctx)             → 打開連線、抓 NAV+持倉+報價、分類 SR/SP500/其他，
                                     回傳完整 snapshot dict

所有函式皆為 thread-safe（依賴 immutable AccountCtx，不讀模組全域變數）。
"""

from __future__ import annotations

import logging
from datetime import date

from ib_insync import IB

from account_ctx import AccountCtx
from symbol_contract import canonicalize_position, contract_symbol

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
    _log_connected(ctx)
    return conn


def _log_connected(ctx: AccountCtx) -> None:
    log.info(f"Connected  host={ctx.host} port={ctx.port} "
             f"clientId={ctx.client_id} account={ctx.account}")


def get_nav(ib: IB, ctx: AccountCtx) -> float:
    """取得 NAV（USD）。查不到時拋 RuntimeError。"""
    for v in ib.accountValues(ctx.account):
        if v.tag == 'NetLiquidation' and v.currency == 'USD':
            return float(v.value)
    raise RuntimeError(f"Cannot get NAV for account {ctx.account}")


def get_account_info(ib: IB, ctx: AccountCtx) -> dict:
    """
    一次取得 NAV + Cushion + ExcessLiquidity + MaintMarginReq。

    Returns
    -------
    dict with keys: nav, cushion_pct, excess_liq, maint_margin
    """
    info = {'nav': 0.0, 'cushion_pct': 0.0, 'excess_liq': 0.0, 'maint_margin': 0.0}
    for v in ib.accountValues(ctx.account):
        if v.currency not in ('USD', ''):
            continue
        if v.tag == 'NetLiquidation':
            info['nav'] = float(v.value)
        elif v.tag == 'Cushion':
            info['cushion_pct'] = float(v.value) * 100
        elif v.tag == 'ExcessLiquidity':
            info['excess_liq'] = float(v.value)
        elif v.tag == 'MaintMarginReq':
            info['maint_margin'] = float(v.value)
    if info['nav'] == 0:
        raise RuntimeError(f"Cannot get NAV for account {ctx.account}")
    return info


def get_positions(ib: IB, ctx: AccountCtx) -> dict[str, float]:
    """回傳 {symbol: position_qty} — 僅股票，無數量過濾"""
    positions: dict[str, float] = {}
    for p in ib.positions(ctx.account):
        if p.contract.secType != 'STK':
            continue
        # IB reports class shares with the broker spelling (e.g. ``BRK B``).
        # Aggregate rather than overwrite in the unlikely event that both
        # spellings appear in one snapshot.
        sym = contract_symbol(p.contract)
        positions[sym] = positions.get(sym, 0.0) + p.position
    return positions


def get_positions_detail(ib: IB, ctx: AccountCtx) -> list:
    """回傳非零股票持倉的完整 Position object list"""
    return [
        canonicalize_position(p) for p in ib.positions(ctx.account)
        if p.contract.secType == 'STK' and p.position != 0
    ]


def get_prices(ib: IB, symbols: list, ctx: AccountCtx, *, readonly: bool = False) -> dict[str, float]:
    """
    取得 symbols 的報價（透過 price_utils.robust_get_prices 的多層 fallback）。
    優先順序：本連線即時 → ctx.price_feed_port 備援 → delayed → 本地 CSV。
    """
    from broker import price_feed_client_id
    from price_utils import robust_get_prices
    return robust_get_prices(
        ib, symbols,
        host=ctx.host,
        price_feed_port=ctx.price_feed_port,
        verbose_log=lambda m: log.info(m),
        feed_client_id=price_feed_client_id(ctx.client_id, readonly=readonly),
        readonly=readonly,
    )


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
            "configured_account": readings["configured"],
            "used_account": readings["account"],
            "vals_source": readings["vals_source"],
            "n_account_values": readings["n_account_values"],
            "n_positions_raw": readings["n_positions_raw"],
            "n_positions_stk": len(positions),
            "port": ctx.port,
            "client_id": ctx.client_id,
        }
        result["available"] = True
    except Exception as e:
        result["error"] = str(e)
    finally:
        # 讀到哪就填到哪（中途失敗時已讀到的權益照舊留著，同 #185 之前）
        for key in ("nav", "cushion", "excess_liq"):
            if key in readings:
                result[key] = readings[key]
        if conn is not None:
            conn.close()

    return result
