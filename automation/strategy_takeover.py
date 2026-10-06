"""
QuantCore — 單一標的交給策略接管（#107）

Panel 手動持倉碰撞提示（Sync Pos 預覽的「⏸ 手動持倉暫停」）按「交給策略
接管」之後的預覽與送單。此模組不碰 Tk；視窗在 takeover_window，執行緒與
Telegram 在 control_panel。

規則
────
- 目標 = Sync Pos 同一套語意，只取這一檔：調倉輸入（plan_inputs，SYNC，
  不寫 state）的 SR 今日訊號、SP500 live 權重、Skew LS 最新訊號；經
  `rebalance_plan.combine_targets` 的 per-Strategy 取整後加總（ADR-0008）。
- 差額 = 目標 − IB 現有股數（int，同 Rebalance Plan）；目標 0 = 賣出現有股數。
- 還有未結案 Panel 手動單、或 IB 上這一檔還有任何掛單 → 不能接管。
- 確認時在同一條連線重算：現有股數、手動股數、目標或暫停原因變了 → 舊預覽
  失效（TakeoverStale 帶回新預覽），不能直接沿用。
- 只送這一檔的 LMT（Sync Pos 同價規則、outsideRth），clientId ROLE_TAKEOVER
  — 排程調倉的「全部取消掛單」看不到它。刻意不經策略的暫停閘門
  （manual_attribution.gate_orders）：被暫停的正是這一檔，且使用者剛確認過。
- 送出前記接管紀錄；成交且 IB 股數＝接管後預計股數才轉移歸屬、解除暫停
  （manual_attribution）。拒單／取消 → 刪紀錄、維持暫停；逾時 → 紀錄留著，
  之後任何一次觀察對上就完成。
- 不改訊號、Allocation、state；不觸發整個 Account 的 Sync Pos。
- IB 連線與本機紀錄沿用 Sync Pos 的 `SyncRecords`，由呼叫端（新 Panel 的送單目標）
  明確給；沒給就報錯，沒有「預設實盤」（#183）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import logging
import math
import re
import uuid
from typing import Callable, Optional

from account_ctx import AccountCtx
from broker import IB_DONE_STATUSES
import manual_attribution
import plan_inputs
import rebalance_plan as plan
from sync_position import SyncRecords, require_records

log = logging.getLogger(__name__)

_EPS = 1e-6


class TakeoverRefused(ValueError):
    """這次不能接管（原因給使用者看）；什麼都沒送、歸屬與暫停不變。"""


class TakeoverStale(TakeoverRefused):
    """預覽後資料變了；fresh = 最新預覽，要使用者重新確認。"""

    def __init__(self, fresh: "TakeoverPreview"):
        super().__init__("持倉、策略目標或暫停狀態在預覽後變了，請確認新的預覽")
        self.fresh = fresh


def _fmt(qty: float) -> str:
    return format(qty, ".6f").rstrip("0").rstrip(".")


@dataclass(frozen=True)
class TakeoverPreview:
    account_name: str
    symbol: str
    current: float                      # IB 現有股數
    manual: float                       # 手動紀錄股數
    target: int                         # 策略整數目標（per-Strategy 取整加總）
    parts: tuple[tuple[str, int], ...]  # 各策略的整數目標
    pause_reason: str
    price: float = field(compare=False)         # 參考價（估算金額用）
    nav: float = field(compare=False)
    signal_dates: str = field(compare=False)

    @property
    def delta(self) -> int:
        return self.target - int(self.current)

    @property
    def action(self) -> str:
        return "BUY" if self.delta > 0 else ("SELL" if self.delta < 0 else "")

    @property
    def quantity(self) -> int:
        return abs(self.delta)

    @property
    def expected_after(self) -> float:
        return self.current + self.delta

    def lines(self) -> list[str]:
        """預覽內容（純文字，一行一項）。"""
        parts = "、".join(f"{s} {n}" for s, n in self.parts) or "各策略皆為 0"
        out = [
            f"Account：{self.account_name}",
            f"標的：{self.symbol}",
            f"暫停原因：{self.pause_reason}",
            f"手動紀錄股數：{_fmt(self.manual)}",
            f"IB 現有股數：{_fmt(self.current)}",
            f"策略整數目標：{self.target} 股（{parts}）",
            f"訊號日期：{self.signal_dates}",
        ]
        if self.delta == 0:
            out.append("差額：0 股 — 不需下單，確認後直接把這檔交給策略")
        elif self.target == 0:
            out.append(f"差額：{self.delta:+d} 股 — 策略目標為 0，"
                       f"將賣出現有 {self.quantity} 股")
        else:
            verb = "買進" if self.action == "BUY" else "賣出"
            out.append(f"差額：{self.delta:+d} 股 — {verb} {self.quantity} 股"
                       f"（約 ${self.quantity * self.price:,.0f}）")
        if abs(self.expected_after - self.target) > _EPS:
            out.append(f"接管後 IB 預計 {_fmt(self.expected_after)} 股"
                       "（碎股部分無法以整股單調整）")
        out.append("只送這一檔；成交並與 IB 持倉對上後才解除暫停。")
        return out


@dataclass
class TakeoverResult:
    status: str          # COMPLETED / PENDING / FAILED / DRY_RUN
    message: str
    preview: TakeoverPreview
    order_status: str = ""


def _open_orders(conn, ctx: AccountCtx, symbol: str) -> list:
    """這個 Account 在 IB 上這一檔的所有掛單（任何 clientId、含 TWS）。"""
    from symbol_contract import contract_symbol
    out = []
    for t in conn.open_orders(all_clients=True):
        account = str(getattr(t.order, "account", "") or ctx.account)
        status = str(getattr(getattr(t, "orderStatus", None), "status", "") or "")
        if (account == ctx.account and status not in IB_DONE_STATUSES
                and contract_symbol(t.contract) == symbol):
            out.append(t)
    return out


def _compute(conn, ctx: AccountCtx, symbol: str, log_fn: Callable[[str], None],
             records: SyncRecords) -> tuple[TakeoverPreview, dict[str, float], set[str]]:
    """同一條連線上的快照 → 預覽（或 TakeoverRefused）。回傳 (預覽, 持倉, Universe)。

    調倉輸入（plan_inputs，SYNC：與 Sync Pos 同一套最新訊號）；接管不得改 state，
    所以一律不寫。拒絕條件在手動持倉歸屬之後、SP500 訊號之前檢查。"""

    def vet(inputs: plan_inputs.PlanInputs) -> None:
        attribution = inputs.attribution
        if symbol in attribution.completed:
            raise TakeoverRefused(f"{symbol} 先前送出的接管單已成交並對上，接管已完成")
        if symbol not in attribution.paused:
            raise TakeoverRefused(f"{symbol} 目前沒有被手動持倉暫停，不需要接管")
        unresolved = [f for f in manual_attribution.read_manual_fills(
                          ctx.name, records.manual_orders_store)
                      if f.symbol == symbol and not f.resolved]
        if unresolved:
            raise TakeoverRefused(f"{symbol} 還有 {len(unresolved)} 筆未結案的 Panel "
                                  "手動單；結案後再接管")
        open_orders = _open_orders(conn, ctx, symbol)
        if open_orders:
            desc = "、".join(f"{t.order.action} {_fmt(float(t.order.totalQuantity))}"
                            for t in open_orders)
            raise TakeoverRefused(f"{symbol} 在 IB 還有 {len(open_orders)} 筆掛單（{desc}）；"
                                  "成交或取消後再接管")

    inputs = plan_inputs.start(ctx, plan_inputs.SYNC, persist_state=False,
                               narrator=_TakeoverSteps(log_fn))
    inputs.read(conn,
                ledger_path=records.attribution_ledger,
                manual_orders_path=records.manual_orders_store,
                vet=vet, price_symbols=[symbol])
    nav = inputs.nav
    positions = inputs.positions
    attribution = inputs.attribution

    price = inputs.prices.get(symbol, 0)
    if not (price and math.isfinite(price) and price > 0):
        raise TakeoverRefused(f"沒有 {symbol} 的有效報價，無法計算策略目標")
    prices = {symbol: price}
    only = (lambda w: {symbol: w[symbol]} if symbol in w else {})
    # [[Combine]]：per-Strategy 取整後加總（ADR-0008），只取這一檔
    combined = plan.combine_targets(nav, only(inputs.sr_wgt), only(inputs.sp500_wgt),
                                    prices, ctx=ctx, skewls_wgt=only(inputs.skewls_wgt))
    target = int(combined.get(symbol, {}).get("shares", 0))
    # 各 Strategy 拆解 = Combine 加總前的整數（SR → SP500 → SkewLS 順序）
    parts = list(combined.get(symbol, {}).get("strategy_shares", {}).items())

    preview = TakeoverPreview(
        account_name=ctx.name, symbol=symbol,
        current=float(positions.get(symbol, 0.0)),
        manual=float(attribution.manual.get(symbol, 0.0)),
        target=target, parts=tuple(parts),
        pause_reason=attribution.paused[symbol],
        price=float(price), nav=float(nav),
        signal_dates=(f"SR {inputs.sr_date}｜SP500 {inputs.sp500_date}"
                      + (f"｜SkewLS {inputs.skewls_date}" if ctx.alloc_skewls > 0 else "")),
    )
    return preview, positions, inputs.universe


class _TakeoverSteps(plan_inputs.Narrator):
    def __init__(self, say: Callable[[str], None]):
        self._say = say

    def sr(self) -> None:
        self._say("  Computing strategy targets...")


def _logger(log_fn: Optional[Callable[[str], None]]) -> Callable[[str], None]:
    def _log(msg: str) -> None:
        log.info(msg)
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass
    return _log


def build_preview(ctx: AccountCtx, symbol: str,
                  log_fn: Optional[Callable[[str], None]] = None, *,
                  records: SyncRecords) -> TakeoverPreview:
    """連 IB 取最新快照，算這一檔的接管預覽。不送單、不寫 state。"""
    records = require_records(records)
    _log = _logger(log_fn)
    _log(f"  Connecting to IB ({ctx.name})...")
    conn = records.open(ctx)
    try:
        return _compute(conn, ctx, symbol, _log, records)[0]
    finally:
        conn.close()


def _order_ref(account_name: str) -> str:
    return (manual_attribution.TAKEOVER_REF_PREFIX
            + re.sub(r"[^A-Z0-9]+", "", account_name.upper()) + ":" + uuid.uuid4().hex)


def submit(preview: TakeoverPreview, ctx: AccountCtx,
           log_fn: Optional[Callable[[str], None]] = None, *,
           records: SyncRecords) -> TakeoverResult:
    """使用者確認後：同一條連線重算、核對，只送這一檔差額，等成交與對帳。"""
    records = require_records(records)
    _log = _logger(log_fn)
    if ctx.name != preview.account_name:
        raise TakeoverRefused("預覽與目前選的 Account 不同，請重新預覽")
    exec_ctx = replace(ctx, order_type="LMT", outside_rth=True)
    conn = records.open(exec_ctx)
    try:
        fresh, _positions, universe = _compute(conn, exec_ctx, preview.symbol, _log, records)
        if fresh != preview:
            raise TakeoverStale(fresh)
        sym = fresh.symbol
        if exec_ctx.dry_run:
            _log(f"  [DRY RUN] 接管 {sym}：{fresh.action or '不需下單'} {fresh.quantity}")
            return TakeoverResult("DRY_RUN", "[DRY RUN] 沒有送單，歸屬與暫停不變", fresh)

        order_ref = _order_ref(ctx.name)
        manual_attribution.start_takeover(
            ctx.name, sym, action=fresh.action or "NONE", quantity=fresh.quantity,
            target=fresh.target, expected_after=fresh.expected_after,
            order_ref=order_ref, path=records.attribution_ledger)

        order_status = ""
        if fresh.quantity:
            import broker
            import rebalance_ib
            quotes = conn.quotes([sym])
            lmt = broker.limit_price(fresh.action, quotes.get(sym, {}), fresh.price)
            contract = conn.contract(sym)
            order = rebalance_ib.make_order(fresh.action, fresh.quantity, lmt, exec_ctx)
            order.orderRef = order_ref
            _log(f"  LMT {fresh.action} {fresh.quantity}sh {sym} @ ${lmt:.2f}（接管）")
            try:
                trade = conn.place(contract, order)
            except Exception as exc:
                return TakeoverResult(
                    "PENDING", f"送單結果不確定（{exc}）；接管紀錄保留，這檔維持暫停。"
                    "IB 持倉對上接管目標時會自動完成", fresh, "UNKNOWN")
            waited = 0
            while (trade.orderStatus.status not in IB_DONE_STATUSES
                   and waited < max(exec_ctx.wait_fill_sec, 1)):
                conn.wait(1)
                waited += 1
            order_status = str(trade.orderStatus.status or "")
            if order_status in {"Cancelled", "ApiCancelled", "Inactive"}:
                filled = float(trade.orderStatus.filled or 0)
                why = "IB 拒單" if order_status == "Inactive" else "委託被取消"
                detail = [str(getattr(e, "message", "") or "")
                          for e in getattr(trade, "log", ()) or ()]
                detail = [d for d in detail if d]
                if detail:
                    why += f"（{detail[-1]}）"
                if filled > 0:
                    why += f"，已成交 {_fmt(filled)} 股"
                manual_attribution.abandon_takeover(ctx.name, sym, why,
                                                    path=records.attribution_ledger)
                return TakeoverResult("FAILED", f"{why}；這檔維持暫停", fresh, order_status)

        # 對帳：IB 股數＝接管後預計股數才完成（成交後持倉回報可能慢一點）
        for attempt in range(5):
            positions = conn.positions()
            att = manual_attribution.evaluate(
                ctx.name, positions, universe, ledger_path=records.attribution_ledger,
                manual_orders_path=records.manual_orders_store)
            if sym in att.completed or sym not in att.paused:
                return TakeoverResult(
                    "COMPLETED", f"接管完成：IB {_fmt(float(positions.get(sym, 0.0)))} 股，"
                    f"{sym} 恢復策略送單", fresh, order_status)
            if order_status != "Filled":
                break
            conn.wait(1)
        filled = f"，狀態 {order_status}" if order_status else ""
        return TakeoverResult(
            "PENDING", f"委託還沒全部成交或持倉還沒對上{filled}；接管紀錄保留，"
            "這檔維持暫停。之後 IB 持倉對上接管目標時會自動完成", fresh, order_status)
    finally:
        conn.close()


def notify(ctx: AccountCtx, send: Optional[Callable[[str], bool]] = None, *,
           records: SyncRecords) -> list[manual_attribution.Notice]:
    """這個 Account 尚未送達的手動持倉通知（接管結果等）：從 records 的歸屬紀錄
    讀一次，發一則（send 預設走該帳戶 Telegram），送達才在同一份紀錄記已通知。
    回傳讀到的通知 — 不論送達與否，呼叫端都可以寫進 Output Log。"""
    path = require_records(records).attribution_ledger
    notices = manual_attribution.pending_notices(ctx.name, path=path)
    manual_attribution.deliver_notices(ctx, notices, send, path=path)
    return notices
