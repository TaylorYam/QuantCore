"""
QuantCore — Sync Position logic（S10 從 control_panel 抽出）

Panel 按「Sync Pos」時的核心計算與下單邏輯。此模組只處理資料與 IB 互動，
**不觸碰 Tk UI** — 預覽對話框、Telegram 通知等 cross-cutting concerns
保留在 Panel。

核心流程
────────
1. `compute_sync_plan(ctx)`
   → 連 IB → 抓 NAV + 當前持倉 → 算 SR/SP500 目標權重 → 計算差額 order
   → 回傳 SyncPlan（dataclass，含預覽所需的完整 metadata）

2. Panel 顯示預覽對話框，使用者確認

3. `submit_sync_orders(plan, log_fn)`
   → 建新的 LMT+outsideRTH ctx → 連 IB → 取 bid/ask 算 LMT → 下單
   → 回傳 (placed_list, fill_results)

所有 IB 互動走 ib_fetcher；策略訊號走 signals；帳戶 config 走 AccountCtx。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace as _replace
from typing import Callable, Optional

import numpy as np

from account_ctx import AccountCtx
import ib_fetcher
import signals

log = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────

@dataclass
class SyncOrder:
    """單一預計執行的同步委託"""
    symbol: str
    action: str          # 'BUY' | 'SELL'
    qty: int             # > 0
    price: float         # 預覽時的參考價（signal_date 前一天收盤或即時報價）
    strategy: str        # 'SR' | 'SP500' | 'SR+SP500' | 'SP500 (hold)' | etc.
    target: int          # 理論目標股數
    current: int         # 當前實際股數


@dataclass
class SyncPlan:
    """
    Sync Pos 計算結果 — 包含 Panel 預覽對話框所需的全部 metadata。
    """
    ctx: AccountCtx
    orders: list[SyncOrder]
    nav: float
    sr_wgt: dict[str, float]
    sp500_wgt: dict[str, float]
    sr_date: str
    sp500_date: str
    prices: dict[str, float]
    targets: dict[str, dict]          # symbol -> {'shares', 'strategy', 'alloc_w'}
    current_pos: dict[str, float]     # symbol -> shares
    current_other_holdings: list[str] # 手動持倉（排除在 rebalance 外）


# ── Compute phase ─────────────────────────────────────────────────────

def compute_sync_plan(
    ctx: AccountCtx,
    log_fn: Optional[Callable[[str], None]] = None,
) -> SyncPlan:
    """
    連線 IB、計算理論持倉與差額 order list，回傳完整 SyncPlan。

    Parameters
    ----------
    ctx : AccountCtx
        目標帳戶的 config 快照
    log_fn : callable(str) | None
        progress log callback（GUI 用，在呼叫 thread 執行；main-thread marshalling
        由 caller 負責）

    Returns
    -------
    SyncPlan

    Raises
    ------
    Exception on IB connection / signal computation failure
    """
    def _log(msg: str) -> None:
        log.info(msg)
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    # asyncio 需要有 event loop 才能讓 ib_insync 跑
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    # 1. SR 目標權重（不需 IB）
    _log("  Computing SR weights...")
    sr_wgt, sr_date = signals.get_sr_weights()

    # 2. 連 IB
    _log("  Connecting to IB...")
    ib = ib_fetcher.connect(ctx)
    try:
        nav = ib_fetcher.get_nav(ib, ctx)
        current_pos = ib_fetcher.get_positions(ib, ctx)
        _log(f"  NAV=${nav:,.0f}  positions={len(current_pos)}")

        # 3. 分辨 SR vs SP500 vs 手動持倉
        sp500_universe = signals.get_sp500_universe()
        sr_etf_set = ib_fetcher.SR_ETFS
        current_sp500_holdings = [s for s in current_pos
                                   if s not in sr_etf_set and s in sp500_universe]
        current_other_holdings = [s for s in current_pos
                                   if s not in sr_etf_set and s not in sp500_universe]

        # 4. SP500 目標權重
        _log("  Computing SP500 weights...")
        sp500_wgt, sp500_date = signals.get_sp500_weights(
            current_sp500_holdings, ctx.name)

        # 5. 取報價
        all_syms = list(set(
            list(sr_wgt.keys()) + list(sp500_wgt.keys()) + list(current_pos.keys())
        ))
        _log(f"  Getting prices for {len(all_syms)} symbols...")
        prices = ib_fetcher.get_prices(ib, all_syms, ctx)

        # 6. 合併目標（呼叫 rebalance_ib 的 combine_targets 共用邏輯）
        import rebalance_ib as reb
        targets = reb.combine_targets(nav, sr_wgt, sp500_wgt, prices, ctx=ctx)

    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass

    # 7. 差額 order list（套 REBALANCE_THRESHOLD 過濾）
    orders = _build_order_list(
        targets=targets,
        current_pos=current_pos,
        current_other_holdings=current_other_holdings,
        prices=prices,
        nav=nav,
    )

    return SyncPlan(
        ctx=ctx,
        orders=orders,
        nav=nav,
        sr_wgt=sr_wgt,
        sp500_wgt=sp500_wgt,
        sr_date=sr_date,
        sp500_date=sp500_date,
        prices=prices,
        targets=targets,
        current_pos=current_pos,
        current_other_holdings=current_other_holdings,
    )


def _build_order_list(
    targets: dict[str, dict],
    current_pos: dict[str, float],
    current_other_holdings: list[str],
    prices: dict[str, float],
    nav: float,
) -> list[SyncOrder]:
    """
    根據目標持倉 vs 現有持倉算出差額 order list。

    套用 REBALANCE_THRESHOLD 過濾：單一標的漂移 < 3% NAV 不動作。
    例外：完全清倉（target=0）或從無到有建倉（current=0）必執行。
    """
    from strategies.sp500_momentum import config as sp_cfg
    rebal_thresh = sp_cfg.REBALANCE_THRESHOLD  # 0.03

    all_target_syms = set(targets.keys()) | set(current_pos.keys())
    all_target_syms -= set(current_other_holdings)

    orders: list[SyncOrder] = []
    for sym in sorted(all_target_syms):
        price = prices.get(sym, 0)
        if price <= 0:
            continue
        target_shares = targets.get(sym, {}).get('shares', 0)
        curr_shares = int(current_pos.get(sym, 0))
        delta = target_shares - curr_shares
        strategy = targets.get(sym, {}).get('strategy', 'EXIT')

        # Threshold 過濾（只對「調整」不對「清倉」/「建倉」）
        if delta != 0 and target_shares > 0 and curr_shares > 0:
            drift_val = abs(delta) * price
            drift_pct = drift_val / nav if nav > 0 else 0
            if drift_pct < rebal_thresh:
                continue

        if delta != 0:
            orders.append(SyncOrder(
                symbol=sym,
                action='BUY' if delta > 0 else 'SELL',
                qty=abs(delta),
                price=price,
                strategy=strategy,
                target=target_shares,
                current=curr_shares,
            ))
    return orders


# ── Submit phase ──────────────────────────────────────────────────────

def submit_sync_orders(
    plan: SyncPlan,
    log_fn: Optional[Callable[[str], None]] = None,
) -> tuple[list[dict], list[dict]]:
    """
    送出 SyncPlan.orders 的 LMT 單。

    流程：
      1. 建新的 ctx（LMT + outsideRTH），frozen replace，不汙染原 ctx
      2. 連 IB → 取消既有掛單（避免重複）
      3. 取 bid/ask → 算 LMT 價（BUY ask×1.003 / SELL bid×0.997）
      4. 送單 → 等成交 → 回傳 (placed, fill_results)

    Parameters
    ----------
    plan : SyncPlan
        先前 compute_sync_plan 回傳的結果
    log_fn : callable(str) | None
        progress log callback

    Returns
    -------
    (placed, fill_results)
        placed : list[dict]      — rebalance_ib 格式的 order dict（已附 lmt_price）
        fill_results : list[dict] — 成交回報
    """
    def _log(msg: str) -> None:
        log.info(msg)
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    if not plan.orders:
        return [], []

    # asyncio 需要有 event loop
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    import rebalance_ib as reb
    # Frozen AccountCtx replace → LMT + outsideRTH 版本（不汙染原 ctx）
    exec_ctx = _replace(plan.ctx, order_type='LMT', outside_rth=True)

    ib = ib_fetcher.connect(exec_ctx)
    try:
        # 1. 取消既有掛單
        open_trades = ib.openTrades()
        if open_trades:
            _log(f"  Cancelling {len(open_trades)} existing open orders...")
            for t in open_trades:
                ib.cancelOrder(t.order)
            ib.sleep(2)

        # 2. 取得 bid/ask 報價以計算 LMT 價格
        from ib_insync import Stock as IBStock
        syms = list({o.symbol for o in plan.orders})
        contracts = [IBStock(s, 'SMART', 'USD') for s in syms]
        ib.qualifyContracts(*contracts)
        tickers = ib.reqTickers(*contracts)
        quotes = {}
        for t in tickers:
            sym = t.contract.symbol
            bid = t.bid if (t.bid and t.bid > 0 and np.isfinite(t.bid)) else None
            ask = t.ask if (t.ask and t.ask > 0 and np.isfinite(t.ask)) else None
            mid = t.midpoint() if (t.midpoint() and np.isfinite(t.midpoint())) else None
            last = t.last if (t.last and t.last > 0 and np.isfinite(t.last)) else None
            close = t.close if (t.close and t.close > 0 and np.isfinite(t.close)) else None
            quotes[sym] = {
                'bid': bid, 'ask': ask, 'mid': mid,
                'last': last, 'close': close,
            }

        # 3. 算 LMT 價並組 order dict（format 沿用 rebalance_ib.place_orders 吃的）
        #   BUY: ask * 1.003（或 mid/last/close/ref 做後備）
        #   SELL: bid * 0.997（同上）
        ib_orders = []
        for o in plan.orders:
            q = quotes.get(o.symbol, {})
            ref = o.price  # 預覽時的估價
            if o.action == 'BUY':
                base = q.get('ask') or q.get('mid') or q.get('last') or q.get('close') or ref
                lmt = round(base * 1.003, 2)
            else:
                base = q.get('bid') or q.get('mid') or q.get('last') or q.get('close') or ref
                lmt = round(base * 0.997, 2)
            ib_orders.append({
                'symbol':    o.symbol,
                'action':    o.action,
                'qty':       o.qty,
                'lmt_price': lmt,
                'strategy':  o.strategy,
            })
            _log(f"  LMT {o.action} {o.qty}sh {o.symbol} @ ${lmt:.2f}")

        # 4. 送單（透過 rebalance_ib.place_orders）
        placed, trades = reb.place_orders(ib, ib_orders, ctx=exec_ctx)

        # 5. 等成交回報
        fill_results = []
        if trades:
            fill_results = reb.check_fills(trades)

    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass

    return placed, fill_results
