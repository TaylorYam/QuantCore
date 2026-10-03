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
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from account_ctx import AccountCtx
import ib_fetcher
import manual_attribution
import rebalance_plan as plan
import signals

log = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class SyncRecords:
    """Sync Pos 用的 IB 連線與本機紀錄。

    預設 = 實盤（Tk Panel）：ib_fetcher.connect、實盤的手動歸屬紀錄與手動單紀錄、
    寫回 SP500／SkewLS state。新 Panel 的 paper Sync Pos（#152）換一組：連線走
    送單閘門、歸屬與手動單紀錄用 paper 專用檔、state 只讀不寫 — paper 帳戶的
    持倉與成交不能改到實盤帳戶的紀錄。
    """
    connect: Optional[Callable[[AccountCtx], object]] = None   # None = ib_fetcher.connect
    attribution_ledger: Optional[Path] = None                  # None = 實盤紀錄檔
    manual_orders_store: Optional[Path] = None                 # None = 實盤手動單紀錄
    persist_strategy_state: bool = True

    def open(self, ctx: AccountCtx):
        return (self.connect or ib_fetcher.connect)(ctx)

    # 實盤預設不多傳參數：呼叫與改動前完全相同
    def sp500_state_options(self) -> dict:
        return {} if self.persist_strategy_state else {"persist": False}

    def gate_options(self) -> dict:
        return ({} if self.attribution_ledger is None
                else {"attribution_path": self.attribution_ledger})


LIVE_RECORDS = SyncRecords()


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
    targets: dict[str, dict]          # symbol -> {'shares', 'strategy', 'alloc_w', 'strategy_shares'}
    current_pos: dict[str, float]     # symbol -> shares (可為負)
    current_other_holdings: list[str] # 手動持倉（排除在 rebalance 外）
    # SkewLS 欄位（S19）— 沒啟用時為 {} / 'N/A'
    skewls_wgt:  dict[str, float] = field(default_factory=dict)
    skewls_date: str = "N/A"
    # 手動持倉暫停（#106）— 不在 orders 裡、預覽另列原因
    paused: dict[str, str] = field(default_factory=dict)        # 標的 → 原因
    paused_orders: list[SyncOrder] = field(default_factory=list)
    pause_notices: list[manual_attribution.Notice] = field(default_factory=list)  # 尚未通知
    records: SyncRecords = LIVE_RECORDS   # 送單沿用計算時的連線與紀錄


# ── Compute phase ─────────────────────────────────────────────────────

def compute_sync_plan(
    ctx: AccountCtx,
    log_fn: Optional[Callable[[str], None]] = None,
    *,
    records: SyncRecords = LIVE_RECORDS,
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
    records : SyncRecords
        IB 連線與本機紀錄；預設實盤

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
    ib = records.open(ctx)
    try:
        nav = ib_fetcher.get_nav(ib, ctx)
        current_pos = ib_fetcher.get_positions(ib, ctx)
        _log(f"  NAV=${nav:,.0f}  positions={len(current_pos)}")

        # 3. 分辨 SR vs SP500 vs 手動持倉（唯一實作 = rebalance_plan.classify_holdings）
        sp500_universe = signals.get_sp500_universe()
        split = plan.classify_holdings(current_pos, sp500_universe)
        current_sp500_holdings = split.sp500
        current_other_holdings = split.other

        # 3b. 手動持倉歸屬（#106）— 讀寫失敗拋例外：本帳戶這次不 Sync
        #     （在 4. 算 SP500 訊號寫 state 之前）
        attribution = manual_attribution.evaluate(
            ctx.name, current_pos, sp500_universe,
            ledger_path=records.attribution_ledger,
            manual_orders_path=records.manual_orders_store)

        # 4. SP500 目標權重
        _log("  Computing SP500 weights...")
        sp500_wgt, sp500_date = signals.get_sp500_weights(
            current_sp500_holdings, ctx.name, **records.sp500_state_options())

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
    # 手動持倉暫停（#106）的標的不列入可送的單，預覽另列原因
    orders, paused_orders = attribution.split_orders(orders,
                                                     key=lambda o: o.symbol)

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
        paused=dict(attribution.paused),
        paused_orders=paused_orders,
        pause_notices=list(attribution.new_notices),
        records=records,
    )


def notify_new_pauses(plan: SyncPlan,
                      send: Optional[Callable[[str], bool]] = None) -> bool:
    """Sync Pos 計算時新出現的手動持倉暫停 → 該 Account Telegram 一則（#106）。

    送成功才記「已通知」，同一次暫停只通知一次。send 預設走該帳戶的
    Telegram 設定；回傳是否有送出。
    """
    return manual_attribution.deliver_notices(plan.ctx, plan.pause_notices, send)


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

def read_quotes(ib, symbols: list[str]) -> dict[str, dict]:
    """本連線的 bid/ask/mid/last/close（無效值為 None），key = constituents 寫法。"""
    from symbol_contract import canonical_symbol, ib_stock
    contracts = [ib_stock(s) for s in symbols]
    ib.qualifyContracts(*contracts)
    tickers = ib.reqTickers(*contracts)
    quotes = {}
    for t in tickers:
        sym = canonical_symbol(t.contract.symbol)
        bid = t.bid if (t.bid and t.bid > 0 and np.isfinite(t.bid)) else None
        ask = t.ask if (t.ask and t.ask > 0 and np.isfinite(t.ask)) else None
        mid = t.midpoint() if (t.midpoint() and np.isfinite(t.midpoint())) else None
        last = t.last if (t.last and t.last > 0 and np.isfinite(t.last)) else None
        close = t.close if (t.close and t.close > 0 and np.isfinite(t.close)) else None
        quotes[sym] = {
            'bid': bid, 'ask': ask, 'mid': mid,
            'last': last, 'close': close,
        }
    return quotes


def sync_lmt_price(action: str, quote: dict, ref: float) -> float:
    """Sync Pos 的 LMT 價：BUY ask×1.003、SELL bid×0.997（缺價依序用
    mid/last/close/ref 後備）。交給策略接管（#107）用同一規則。"""
    if action == 'BUY':
        base = (quote.get('ask') or quote.get('mid') or quote.get('last')
                or quote.get('close') or ref)
        return round(base * 1.003, 2)
    base = (quote.get('bid') or quote.get('mid') or quote.get('last')
            or quote.get('close') or ref)
    return round(base * 0.997, 2)


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

    records = plan.records
    ib = records.open(exec_ctx)
    try:
        # 1. 取消既有掛單（Panel 手動單除外，#106）
        open_trades = manual_attribution.strategy_open_trades(ib.openTrades())
        if open_trades:
            _log(f"  Cancelling {len(open_trades)} existing open orders...")
            for t in open_trades:
                ib.cancelOrder(t.order)
            ib.sleep(2)

        # 2. 取得 bid/ask 報價以計算 LMT 價格
        quotes = read_quotes(ib, list({o.symbol for o in plan.orders}))

        # 3. 算 LMT 價並組 order dict（format 沿用 rebalance_ib.place_orders 吃的）
        ib_orders = []
        for o in plan.orders:
            # o.price = 預覽時的估價（報價都缺時的後備）
            lmt = sync_lmt_price(o.action, quotes.get(o.symbol, {}), o.price)
            ib_orders.append({
                'symbol':    o.symbol,
                'action':    o.action,
                'qty':       o.qty,
                'lmt_price': lmt,
                'strategy':  o.strategy,
            })
            _log(f"  LMT {o.action} {o.qty}sh {o.symbol} @ ${lmt:.2f}")

        # 4. 送單（透過 rebalance_ib.place_orders）
        placed, trades = reb.place_orders(ib, ib_orders, ctx=exec_ctx,
                                          **records.gate_options())

        # 5. 等成交回報
        fill_results = []
        if trades:
            fill_results = reb.check_fills(trades)

        # 6. SkewLS state 更新（Sync Pos = 強制同步，所以即使非 threshold-triggered 也存）
        #    這樣下次 scheduled threshold check 才有正確 baseline。
        if plan.skewls_wgt and plan.ctx.alloc_skewls > 0 and records.persist_strategy_state:
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


# ── 送出後：交易紀錄與 Telegram（Tk Panel 與新 Panel 正式模式共用，#154）──

def log_sync_trades(plan: SyncPlan, placed: list[dict], fill_results: list[dict]) -> None:
    """trade_log.xlsx：每筆送出的單一列（自 control_panel._sync_execute 抽出，逐字相同）。"""
    from excel_logger import append_trade_log

    ctx = plan.ctx
    for o in placed:
        strat = o.get('strategy', 'Sync')
        append_trade_log([o], fill_results,
                         f"SR:{plan.sr_date} SP500:{plan.sp500_date}",
                         'LMT', ctx.leverage, ctx.dry_run,
                         strategy=strat, account_name=ctx.name)


def sync_result_text(plan: SyncPlan, placed: list[dict], fill_results: list[dict], *,
                     today=None) -> str:
    """送出結果的 Telegram 內容（HTML）；沒有成交回報時列委託。"""
    from datetime import date as _date

    lines = [
        f"<b>[{plan.ctx.name}] Sync Position — LMT</b>",
        f"Date: {today or _date.today()}",
        f"NAV: <b>${plan.nav:,.2f}</b>",
        f"SR: {plan.sr_date}  |  SP500: {plan.sp500_date}",
    ]
    if not fill_results:
        lines.append(f"\n<b>委託 ({len(placed)} 筆):</b>")
        for o in placed:
            action_tag = "🔴 SELL" if o['action'] == 'SELL' else "🟢 BUY "
            lines.append(f"  {action_tag} {o['qty']}sh {o['symbol']}  [{o.get('strategy', '')}]")
    else:
        lines.append(f"\n<b>成交結果 ({len(fill_results)} 筆):</b>")
        for f in fill_results:
            icon = "✅" if f['status'] == 'Filled' else "⚠️"
            action_tag = "🔴 SELL" if f['action'] == 'SELL' else "🟢 BUY "
            val = f['filled'] * f['avg_price']
            px_str = f"@ ${f['avg_price']:.2f}  ~${val:,.0f}" if f['filled'] > 0 else "未成交"
            lines.append(f"  {icon} {action_tag} {f['filled']}/{f['qty']}sh {f['symbol']}  "
                         f"{px_str}  [{f['status']}]")
    return "\n".join(lines)


def send_sync_result(plan: SyncPlan, placed: list[dict], fill_results: list[dict]) -> None:
    """送出結果發到該帳戶的 Telegram。"""
    from notify import tg_send
    tg_send(sync_result_text(plan, placed, fill_results),
            token=plan.ctx.tg_token, chat_id=plan.ctx.tg_chat_id)
