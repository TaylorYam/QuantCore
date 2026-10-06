"""
QuantCore — Sync Position logic（S10 從 control_panel 抽出）

Panel 按「Sync Pos」時的核心計算與下單邏輯。此模組只處理資料與 IB 互動，
**不觸碰 Tk UI** — 預覽對話框、Telegram 通知等 cross-cutting concerns
保留在 Panel。

核心流程
────────
1. `compute_sync_plan(ctx)`
   → 調倉輸入（plan_inputs，SYNC：全部用最新訊號）：連 IB → NAV + 當前持倉 →
     手動持倉歸屬 → 三策略目標權重 → 報價 → 計算差額 order
   → 回傳 SyncPlan（dataclass，含預覽所需的完整 metadata）

2. Panel 顯示預覽對話框，使用者確認

3. `submit_sync_orders(plan, log_fn)`
   → 建新的 LMT+outsideRTH ctx → 連 IB → 取 bid/ask 算 LMT → 下單
   → 回傳 (placed_list, fill_results)

所有 IB 互動走 Broker 的連線（records.open → broker.Connection）；策略訊號走 signals；
帳戶 config 走 AccountCtx；
分類 / Combine / 差額決策走 rebalance_plan（與 scheduled rebalance 同一顆腦）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace as _replace
from pathlib import Path
from typing import Callable, Optional

from account_ctx import AccountCtx
import manual_attribution
import plan_inputs
import rebalance_plan as plan

log = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class SyncRecords:
    """Sync Pos 與交給策略接管用的 IB 連線與本機紀錄，一律由呼叫端明確給（#183）。

    沒有預設值：新 Panel 的送單目標（`qt_panel.order_target`）決定 paper 或實盤
    — 連線都經過送單閘門；paper 用自己的歸屬與手動單紀錄、SP500／SkewLS state
    只讀不寫；實盤用實盤的紀錄並寫回 state。connect 回傳 `broker.Connection`。
    """
    connect: Callable[[AccountCtx], object]
    attribution_ledger: Path
    manual_orders_store: Path
    persist_strategy_state: bool

    def open(self, ctx: AccountCtx):
        return self.connect(ctx)

    def gate_options(self) -> dict:
        return {"attribution_path": self.attribution_ledger}


def require_records(records: Optional[SyncRecords]) -> SyncRecords:
    """沒給連線與紀錄就報錯：不能在不知情下連實盤、繞過送單閘門（#183）。"""
    if not isinstance(records, SyncRecords):
        raise TypeError("沒有指定送單目標的連線與紀錄（SyncRecords），不送單")
    return records


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
    # 送單沿用計算時的連線與紀錄；只做預覽文字的 plan 可以沒有，但不能送出
    records: Optional[SyncRecords] = None
    # 算這份 plan 的調倉輸入（plan_inputs）；送出後由它記 SkewLS state
    inputs: Optional[plan_inputs.PlanInputs] = None


# ── Compute phase ─────────────────────────────────────────────────────

def compute_sync_plan(
    ctx: AccountCtx,
    log_fn: Optional[Callable[[str], None]] = None,
    *,
    records: SyncRecords,
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
        IB 連線與本機紀錄（必填；沒給就報錯）

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

    records = require_records(records)

    # 1. 不需 IB 的訊號：SR，以及 SkewLS — Sync Pos 是「強制同步到最新 target」，
    #    不套 threshold gating（使用者按 Sync 就是要把帳戶拉到 today's target）；
    #    事後 submit 成功時會更新 state 給未來 threshold 用。
    inputs = plan_inputs.start(ctx, plan_inputs.SYNC,
                               persist_state=records.persist_strategy_state,
                               narrator=_SyncSteps(_log))

    # 2. 連 IB → NAV + 持倉 → 手動持倉歸屬（#106，讀寫失敗拋例外：本帳戶這次
    #    不 Sync，且在算 SP500 訊號寫 state 之前）→ SP500 → 報價
    _log("  Connecting to IB...")
    conn = records.open(ctx)
    try:
        inputs.read(conn,
                    ledger_path=records.attribution_ledger,
                    manual_orders_path=records.manual_orders_store)
        nav = inputs.nav
        current_pos = inputs.positions
        current_other_holdings = inputs.split.other
        prices = inputs.prices
        attribution = inputs.attribution

        # 6. 合併三策略目標（[[Combine]]，唯一實作 = rebalance_plan.combine_targets）
        targets = plan.combine_targets(nav, inputs.sr_wgt, inputs.sp500_wgt, prices,
                                       ctx=ctx, skewls_wgt=inputs.skewls_wgt)

    finally:
        conn.close()

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
        sr_wgt=inputs.sr_wgt,
        sp500_wgt=inputs.sp500_wgt,
        sr_date=inputs.sr_date,
        sp500_date=inputs.sp500_date,
        prices=prices,
        targets=targets,
        current_pos=current_pos,
        current_other_holdings=current_other_holdings,
        skewls_wgt=inputs.skewls_wgt,
        skewls_date=inputs.skewls_date,
        paused=dict(attribution.paused),
        paused_orders=paused_orders,
        pause_notices=list(attribution.new_notices),
        records=records,
        inputs=inputs,
    )


class _SyncSteps(plan_inputs.Narrator):
    """Sync Pos 的進度文字（預覽視窗與 Output Log）。"""

    def __init__(self, say: Callable[[str], None]):
        self._say = say
        self._nav = 0.0

    def sr(self) -> None:
        self._say("  Computing SR weights...")

    def skewls_latest(self) -> None:
        self._say("  Computing Skew LS weights...")

    def nav(self, nav: float) -> None:
        self._nav = nav

    def positions(self, positions: dict[str, float]) -> None:
        self._say(f"  NAV=${self._nav:,.0f}  positions={len(positions)}")

    def sp500(self, run: bool) -> None:
        self._say("  Computing SP500 weights...")

    def prices(self, symbols: list[str]) -> None:
        self._say(f"  Getting prices for {len(symbols)} symbols...")


def notify_new_pauses(plan: SyncPlan,
                      send: Optional[Callable[[str], bool]] = None) -> bool:
    """Sync Pos 計算時新出現的手動持倉暫停 → 該 Account 一則通知（#106）。

    送成功才記「已通知」，記在算這份 plan 時讀的那份歸屬紀錄，同一次暫停只通知
    一次。send 預設走該帳戶的 Telegram 設定；回傳是否有送出。
    """
    records = require_records(plan.records)
    return manual_attribution.deliver_notices(plan.ctx, plan.pause_notices, send,
                                              path=records.attribution_ledger)


# ── Submit phase ──────────────────────────────────────────────────────

def submit_sync_orders(
    plan: SyncPlan,
    log_fn: Optional[Callable[[str], None]] = None,
) -> tuple[list[dict], list[dict]]:
    """
    送出 SyncPlan.orders 的 LMT 單。

    流程：
      1. 建新的 ctx（LMT + outsideRTH），frozen replace，不汙染原 ctx
      2. 連 IB → strategy_orders.send（#205）：暫停閘門 → 撤策略舊單 → bid/ask 算
         LMT 價（BUY ask×1.003 / SELL bid×0.997）→ 送單 → 等成交
      3. SkewLS state 更新

    Parameters
    ----------
    plan : SyncPlan
        先前 compute_sync_plan 回傳的結果
    log_fn : callable(str) | None
        progress log callback

    Returns
    -------
    (placed, fill_results)
        placed : list[dict]      — strategy_orders 格式的 order dict（已附 lmt_price）
        fill_results : list[dict] — 成交回報
    """
    def _log(msg: str) -> None:
        log.info(msg)
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    records = require_records(plan.records)
    if not plan.orders:
        return [], []

    import strategy_orders
    # Frozen AccountCtx replace → LMT + outsideRTH 版本（不汙染原 ctx）
    exec_ctx = _replace(plan.ctx, order_type='LMT', outside_rth=True)

    class _Steps(strategy_orders.Narrator):
        def priced(self, o, lmt: float) -> None:
            _log(f"  LMT {o.action} {o.qty}sh {o.symbol} @ ${lmt:.2f}")

    conn = records.open(exec_ctx)
    try:
        # 送單閘門用送單目標給的歸屬紀錄（paper 用自己的檔，#152）；
        # o.price = 預覽時的估價（報價都缺時的後備）
        sent = strategy_orders.send(conn, plan.orders, exec_ctx, narrator=_Steps(),
                                    log_fn=log_fn, **records.gate_options())
        placed, fill_results = sent.placed, sent.fills

        # 6. SkewLS state 更新（Sync Pos = 強制同步，所以即使非 threshold-triggered 也存）
        #    這樣下次 scheduled threshold check 才有正確 baseline。
        #    規則與寫入的唯一實作在 plan_inputs（paper＝不寫 state 時不記）。
        if plan.inputs is not None:
            try:
                recorded = plan.inputs.record_skewls_rebalance()
                if recorded is not None:
                    _log(f"  SkewLS state updated → last_target_date={recorded}")
            except Exception as e:
                log.error(f"SkewLS state save failed: {e}", exc_info=True)
                # 不 raise — state 寫入失敗不該阻擋送單流程

    finally:
        conn.close()

    return placed, fill_results


# ── 送出後：交易紀錄與 Telegram（實盤送單目標用，#154）──

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
