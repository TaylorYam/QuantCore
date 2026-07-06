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

所有 IB 互動走 ib_fetcher；策略訊號走 signals；帳戶 config 走 AccountCtx；
分類 / Combine / 差額決策走 rebalance_plan（與 scheduled rebalance 同一顆腦）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace as _replace
from typing import Callable, Optional

import numpy as np

from account_ctx import AccountCtx
import ib_fetcher
import rebalance_plan as plan
import signals

log = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────

# 決策層的訂單 dataclass（唯一實作在 rebalance_plan）；
# SyncOrder 為舊名 re-export — panel / sync_preview_window 沿用此路徑。
SyncOrder = plan.PlannedOrder


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
    current_pos: dict[str, float]     # symbol -> shares (可為負)
    current_other_holdings: list[str] # 手動持倉（排除在 rebalance 外）
    # SkewLS 欄位（S19）— 沒啟用時為 {} / 'N/A'
    skewls_wgt:  dict[str, float] = field(default_factory=dict)
    skewls_date: str = "N/A"


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

    # 1b. SkewLS 目標權重 — Sync Pos 是「強制同步到最新 target」，不套 threshold gating
    #     使用者按 Sync 就是要把帳戶拉到 today's target，所以 ignore state file 中的
    #     last_target，直接用最新訊號。事後 submit 成功時會更新 state 給未來 threshold 用。
    skewls_wgt: dict[str, float] = {}
    skewls_date = "N/A"
    if ctx.alloc_skewls > 0:
        _log("  Computing Skew LS weights...")
        skewls_wgt, skewls_date = signals.get_skewls_weights()

    # 2. 連 IB
    _log("  Connecting to IB...")
    ib = ib_fetcher.connect(ctx)
    try:
        nav = ib_fetcher.get_nav(ib, ctx)
        current_pos = ib_fetcher.get_positions(ib, ctx)
        _log(f"  NAV=${nav:,.0f}  positions={len(current_pos)}")

        # 3. 分辨 SR vs SP500 vs 手動持倉（唯一實作 = rebalance_plan.classify_holdings）
        sp500_universe = signals.get_sp500_universe()
        split = plan.classify_holdings(current_pos, sp500_universe)
        current_sp500_holdings = split.sp500
        current_other_holdings = split.other

        # 4. SP500 目標權重
        _log("  Computing SP500 weights...")
        sp500_wgt, sp500_date = signals.get_sp500_weights(
            current_sp500_holdings, ctx.name)

        # 5. 取報價
        all_syms = list(set(
            list(sr_wgt.keys()) +
            list(sp500_wgt.keys()) +
            list(skewls_wgt.keys()) +
            list(current_pos.keys())
        ))
        _log(f"  Getting prices for {len(all_syms)} symbols...")
        prices = ib_fetcher.get_prices(ib, all_syms, ctx)

        # 6. 合併三策略目標（[[Combine]]，唯一實作 = rebalance_plan.combine_targets）
        targets = plan.combine_targets(nav, sr_wgt, sp500_wgt, prices, ctx=ctx,
                                       skewls_wgt=skewls_wgt)

    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass

    # 7. 差額 order list（唯一實作 = rebalance_plan.build_order_list；
    #    log_fn=None：預覽視窗自行顯示，不進 log）
    #    ignore_threshold=True：Sync = 全同步拉齊，門檻內差額也列單 —
    #    否則凍結遺留類的 <3% 殘餘永遠清不掉（2026-07-06 決定）；
    #    使用者在預覽視窗逐單確認後才送，換手由人把關。
    orders = plan.build_order_list(
        targets=targets,
        current_pos=current_pos,
        current_other_holdings=current_other_holdings,
        prices=prices,
        nav=nav,
        ignore_threshold=True,
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
        skewls_wgt=skewls_wgt,
        skewls_date=skewls_date,
    )


# ── Drift check（理論 vs 實際對帳，唯讀）──────────────────────────────

def compute_drift_rows(
    ctx: AccountCtx,
    log_fn: Optional[Callable[[str], None]] = None,
) -> tuple[list, float]:
    """
    連 IB 抓快照，回傳 (rebalance_plan.DeltaRow 列表, nav) — 純觀測，
    不下單、不寫 state。

    與 compute_sync_plan 的**語意差別**：sync 是「強制同步到今日訊號」；
    對帳是「與 scheduled rebalance 的 standing 理論目標比」——
      SR     = 今日訊號（跨週 stable）
      SkewLS = state last_target（threshold gate 維持中的目標；今日訊號
               尚未觸發前不算偏離）
      SP500  = hold 現倉（只在週三 rebalance 日有新目標；平日理論 = 現倉）
    因此平日的 🔴 = 真異常（凍結類），不會把「訊號還沒到執行日」誤報成偏離。
    """
    def _log(msg: str) -> None:
        log.info(msg)
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    _log("  Computing SR weights...")
    sr_wgt, _sr_date = signals.get_sr_weights()

    skewls_wgt: dict[str, float] = {}
    if ctx.alloc_skewls > 0:
        import skewls_state
        state = skewls_state.load_state(ctx.name)
        skewls_wgt = dict(state.get("last_target", {}))

    _log("  Connecting to IB...")
    ib = ib_fetcher.connect(ctx)
    try:
        nav = ib_fetcher.get_nav(ib, ctx)
        current_pos = ib_fetcher.get_positions(ib, ctx)
        _log(f"  NAV=${nav:,.0f}  positions={len(current_pos)}")

        sp500_universe = signals.get_sp500_universe()
        split = plan.classify_holdings(current_pos, sp500_universe)

        sp500_wgt = plan.sp500_placeholder_weights(split.sp500)
        all_syms = plan.plan_symbols(sr_wgt, sp500_wgt, skewls_wgt, current_pos)
        _log(f"  Getting prices for {len(all_syms)} symbols...")
        prices = ib_fetcher.get_prices(ib, all_syms, ctx)
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass

    targets = plan.combine_targets(nav, sr_wgt, {}, prices, ctx=ctx,
                                   skewls_wgt=skewls_wgt)
    targets = plan.apply_sp500_hold(targets, run_sp500=False,
                                    current_sp500_holdings=split.sp500,
                                    positions=current_pos)
    rows = plan.evaluate_deltas(targets, current_pos, split.other,
                                prices, nav)
    return rows, nav


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

        # 6. SkewLS state 更新（Sync Pos = 強制同步，所以即使非 threshold-triggered 也存）
        #    這樣下次 scheduled threshold check 才有正確 baseline。
        if plan.skewls_wgt and plan.ctx.alloc_skewls > 0:
            try:
                import skewls_state
                from datetime import date as _date
                state = skewls_state.load_state(plan.ctx.name)
                state = skewls_state.update_after_rebal(
                    state,
                    new_target=plan.skewls_wgt,
                    signal_date=plan.skewls_date,
                    rebal_date=str(_date.today()),
                )
                skewls_state.save_state(plan.ctx.name, state)
                _log(f"  SkewLS state updated → last_target_date={plan.skewls_date}")
            except Exception as e:
                log.error(f"SkewLS state save failed: {e}", exc_info=True)
                # 不 raise — state 寫入失敗不該阻擋送單流程

    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass

    return placed, fill_results
