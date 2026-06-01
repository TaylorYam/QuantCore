"""
QuantCore — IB Gateway data-fetching helpers（S8 統一入口）

把散落在 rebalance_ib / daily_report / moo_fill_check / control_panel / sp500_daily_check
的 IB 連線與帳戶查詢 helper 收斂到單一模組。

核心函式
────────
低階 helper（需要外部提供 IB 連線）：
  connect(ctx)                     → 建立並回傳 IB 連線
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

log = logging.getLogger(__name__)


# ── 常數（與 strategies 同步）──────────────────────────────────────────

# 11 支板塊 ETF，用於分類 IB 持倉屬於 Sector Rotation 策略
# （與 strategies/sector_rotation/config.py SECTOR_TICKERS 一致）
SR_ETFS: set[str] = {
    'XLK', 'XLF', 'XLV', 'XLE', 'XLI', 'XLY', 'XLB',
    'XLP', 'XLU', 'XLC', 'XLRE',
}

# Skew LS 反向 ETF 清單 — 與 SR_ETFS 一起歸入 ETF Holdings (Issue 2 Option 2)
# 同 strategies/sector_skew_ls/config.py INVERSE_TICKERS
# 確保 strategies 套件可 import — 即使 ib_fetcher 被獨立 import (測試環境等)
import sys as _sys
from pathlib import Path as _Path
_ROOT_DIR = _Path(__file__).parent.parent
if str(_ROOT_DIR) not in _sys.path:
    _sys.path.insert(0, str(_ROOT_DIR))
try:
    from strategies.sector_skew_ls import config as _skewls_cfg
    SKEWLS_INVERSE_TICKERS: set[str] = set(_skewls_cfg.INVERSE_TICKERS)
except Exception:
    SKEWLS_INVERSE_TICKERS = set()

# 所有「ETF Holdings」bucket 涵蓋的 ticker (= SR sector + SkewLS inverse)
ETF_TICKERS: set[str] = SR_ETFS | SKEWLS_INVERSE_TICKERS


# ── 低階：IB 連線 + 單項查詢 ────────────────────────────────────────────

def connect(ctx: AccountCtx, timeout: float = 20.0) -> IB:
    """連線指定帳戶的 IB Gateway。"""
    ib = IB()
    ib.connect(ctx.host, ctx.port, clientId=ctx.client_id, timeout=timeout)
    log.info(f"Connected  host={ctx.host} port={ctx.port} "
             f"clientId={ctx.client_id} account={ctx.account}")
    return ib


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
    return {
        p.contract.symbol: p.position
        for p in ib.positions(ctx.account)
        if p.contract.secType == 'STK'
    }


def get_positions_detail(ib: IB, ctx: AccountCtx) -> list:
    """回傳非零股票持倉的完整 Position object list"""
    return [
        p for p in ib.positions(ctx.account)
        if p.contract.secType == 'STK' and p.position != 0
    ]


def get_prices(ib: IB, symbols: list, ctx: AccountCtx) -> dict[str, float]:
    """
    取得 symbols 的報價（透過 price_utils.robust_get_prices 的多層 fallback）。
    優先順序：本連線即時 → ctx.price_feed_port 備援 → delayed → 本地 CSV。
    """
    from price_utils import robust_get_prices
    return robust_get_prices(
        ib, symbols,
        host=ctx.host,
        price_feed_port=ctx.price_feed_port,
        verbose_log=lambda m: log.info(m),
    )


# ── 高階：完整 Portfolio snapshot（自己管連線）─────────────────────────

def fetch_portfolio(ctx: AccountCtx) -> dict:
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

    ib = None
    try:
        ib = connect(ctx, timeout=15)

        # 1. 信任 Gateway 真正登入的帳號（accounts.json 的 U-號可能過期）
        managed = list(ib.managedAccounts() or [])
        configured = ctx.account
        if managed and configured and configured in managed:
            account = configured
        elif managed:
            account = managed[0]
        else:
            account = configured

        # 2. 帳戶資訊：優先用 accountSummary（snapshot API，對長壽命 GUI 穩定）
        vals_source = "accountSummary"
        vals = []
        try:
            vals = list(ib.accountSummary(account) if account else ib.accountSummary())
        except Exception:
            vals = []
        if not any(v.tag == 'NetLiquidation' for v in vals):
            try:
                vals = list(ib.accountSummary())
            except Exception:
                pass
        # 最後一層：舊款 accountValues（需要訂閱）
        if not any(v.tag == 'NetLiquidation' for v in vals):
            try:
                if account:
                    ib.reqAccountUpdates(True, account)
                ib.sleep(1.5)
            except Exception:
                pass
            vals = list(ib.accountValues(account) if account else ib.accountValues())
            if not any(v.tag == 'NetLiquidation' for v in vals):
                vals = list(ib.accountValues())
            vals_source = "accountValues(fallback)"

        # IB 有時把 NetLiquidation 放在 currency='BASE'（帳戶基礎幣別）
        def _pick(tag: str) -> float:
            # 優先抓指定帳戶 + USD
            for pref in ('USD', 'BASE', ''):
                for v in vals:
                    if v.tag != tag:
                        continue
                    if account and getattr(v, 'account', account) != account:
                        continue
                    if v.currency != pref:
                        continue
                    try:
                        return float(v.value)
                    except Exception:
                        pass
            # 放棄 account 過濾
            for pref in ('USD', 'BASE', ''):
                for v in vals:
                    if v.tag == tag and v.currency == pref:
                        try:
                            return float(v.value)
                        except Exception:
                            pass
            for v in vals:
                if v.tag == tag:
                    try:
                        return float(v.value)
                    except Exception:
                        pass
            return 0.0

        result["nav"] = _pick('NetLiquidation')
        result["cushion"] = _pick('Cushion') * 100
        result["excess_liq"] = _pick('ExcessLiquidity')

        # 3. 持倉明細
        all_pos = list(ib.positions())
        positions = [p for p in all_pos
                     if p.contract.secType == 'STK'
                     and p.position != 0
                     and (not account or p.account == account)]
        # 若嚴格 account 過濾後空，fallback 不過濾
        if not positions and all_pos:
            positions = [p for p in all_pos
                         if p.contract.secType == 'STK' and p.position != 0]

        if positions:
            sp500_universe = get_sp500_universe()
            symbols = [p.contract.symbol for p in positions]
            prices = get_prices(ib, symbols, ctx)
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
            "managed_accounts": list(managed),
            "configured_account": configured,
            "used_account": account,
            "vals_source": vals_source,
            "n_account_values": len(vals),
            "n_positions_raw": len(all_pos),
            "n_positions_stk": len(positions),
            "port": ctx.port,
            "client_id": ctx.client_id,
        }
        result["available"] = True
    except Exception as e:
        result["error"] = str(e)
    finally:
        if ib is not None:
            try:
                if ib.isConnected():
                    ib.disconnect()
            except Exception:
                pass

    return result
