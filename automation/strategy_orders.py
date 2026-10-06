"""策略單送出（#205）：把策略單送進 IB 並彙整成交的唯一實作。

21:00 調倉、16:00 日度風控（熊市 dip-buy）、新 Panel 的 Sync Pos 都呼叫 `send`；
交給策略接管只借 `make_order`（接管自己逐秒等那一張單、對帳流程不同）。這裡不是
排程入口：沒有 `__main__`、載入時沒有副作用。

`send` 的固定順序：
  1. 手動持倉暫停閘門（#106）：暫停中的標的不送；讀不到暫停紀錄拋例外、一張都不送
  2. 非 dry run 且有單：撤掉該帳戶的策略舊單（Panel 手動單、接管單除外），等 2 秒
  3. LMT：向連線要送單報價，用 Broker 的統一限價規則；報價全缺時參考價依序用訂單
     自帶的估價 → 連線的參考價。MOO／MKT 不帶限價，lmt_price 記四捨五入到分的估價
  4. 限價算不出來（≤ 0）的標的不送，記 warning
  5. 先賣後買，每張隔 0.2 秒；dry run 只記 log
  6. 有送單才等 `ctx.wait_fill_sec` 秒，再彙整成交

各 caller 自己的進度文字（Step 編號、每張單的限價說明）由 `Narrator` 說；撤單與
跳過的文字是這裡的，同時交給 `log_fn`（Sync Pos 寫到 Panel 的 Output Log）。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from ib_insync import LimitOrder, MarketOrder, Order

import broker
import manual_attribution
from symbol_contract import canonical_symbol

log = logging.getLogger(__name__)

_CANCEL_WAIT_SEC = 2
_ORDER_GAP_SEC = 0.2


class Narrator:
    """caller 自己的進度文字；預設什麼都不說。依呼叫順序：

    placing（撤完舊單、要開始送）→ priced（LMT，每張算好限價的單；src 是 caller
    傳進來的那一筆）→ checking_fills（有真的送出單，彙整成交前）
    """

    def placing(self, count: int) -> None: ...
    def priced(self, src, lmt: float) -> None: ...
    def checking_fills(self) -> None: ...


@dataclass
class SendResult:
    placed: list[dict] = field(default_factory=list)    # symbol/action/qty/lmt_price/strategy(/order_id)
    fills: list[dict] = field(default_factory=list)     # fill_summary 的格式
    skipped: list[dict] = field(default_factory=list)   # 限價算不出來而沒送的：symbol/action/qty/reason
    cancelled: int = 0                                  # 撤掉的策略舊單數


def make_order(action: str, qty: int, lmt_price: float, ctx) -> Order:
    """依 ctx.order_type 建 IB 單：MOO = 開盤市價（OPG）、LMT、MKT（盤外時 GTC）。"""
    if ctx.order_type == 'MOO':
        o = Order()
        o.action        = action
        o.totalQuantity = qty
        o.orderType     = 'MKT'
        o.tif           = 'OPG'
        o.outsideRth    = False
    elif ctx.order_type == 'LMT':
        o = LimitOrder(action, qty, lmt_price)
        o.outsideRth = ctx.outside_rth
    else:  # MKT
        o = MarketOrder(action, qty)
        o.outsideRth = ctx.outside_rth
        if ctx.outside_rth:
            o.tif = 'GTC'
    # FA 帳戶必須指定下單帳戶，否則 Error 436
    if ctx.account:
        o.account = ctx.account
    return o


def fill_summary(trades: Iterable) -> list[dict]:
    """每張單的成交：股數合計、均價 = Σ(每筆成交價 × 股數) ÷ Σ股數。

    IB 每筆成交回報的 avgPrice 是「到這筆為止的累計均價」，不能再加權（#205）。
    """
    results = []
    for trade in trades:
        sym     = canonical_symbol(trade.contract.symbol)
        action  = trade.order.action
        qty     = int(trade.order.totalQuantity)
        filled  = sum(f.execution.shares for f in trade.fills)
        avg_px  = (
            sum(f.execution.shares * f.execution.price for f in trade.fills) / filled
            if filled > 0 else 0.0
        )
        status  = trade.orderStatus.status
        log.info(f"  Fill: {action} {sym}  {filled}/{qty}sh @ ${avg_px:.2f}  [{status}]")
        results.append({
            'symbol':    sym,
            'action':    action,
            'qty':       qty,
            'filled':    filled,
            'avg_price': avg_px,
            'status':    status,
        })
    return results


def send(conn, orders: Iterable, ctx, *, attribution_path=None,
         narrator: Narrator | None = None,
         log_fn: Callable[[str], None] | None = None) -> SendResult:
    """把策略單送進 IB 並回報成交（順序見 module 說明）。

    conn = broker.Connection；orders = order dict（symbol／action／qty／strategy，可選
    price 估價）或 rebalance_plan.PlannedOrder；ctx 決定 order_type、outside_rth、
    dry_run、wait_fill_sec。attribution_path = 暫停紀錄檔，None = 實盤預設（paper 的
    Sync Pos 用自己的檔，#152）。
    """
    narrator = narrator or Narrator()

    def say(msg: str, warn: bool = False) -> None:
        (log.warning if warn else log.info)(msg)
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    # 1. 暫停閘門
    pending, _held = manual_attribution.gate_orders(
        ctx.name, [(src, _fields(src)) for src in orders],
        key=lambda pair: pair[1]['symbol'], path=attribution_path)
    result = SendResult()
    if not pending:
        return result

    # 2. 撤掉策略舊單
    if not ctx.dry_run:
        open_trades = manual_attribution.strategy_open_trades(conn.open_orders())
        if open_trades:
            say(f"  Cancelling {len(open_trades)} existing open orders...")
            for t in open_trades:
                conn.cancel(t.order)
            conn.wait(_CANCEL_WAIT_SEC)
            result.cancelled = len(open_trades)

    narrator.placing(len(pending))

    # 3–4. 限價
    ready = []
    for src, o, lmt in _priced(conn, pending, ctx):
        if ctx.order_type == 'LMT' and lmt <= 0:
            say(f"    {o['symbol']}: no price, skipping", warn=True)
            result.skipped.append({'symbol': o['symbol'], 'action': o['action'],
                                   'qty': o['qty'], 'reason': 'no price'})
            continue
        if ctx.order_type == 'LMT':
            narrator.priced(src, lmt)
        ready.append({'symbol': o['symbol'], 'action': o['action'], 'qty': o['qty'],
                      'lmt_price': lmt, 'strategy': o['strategy']})

    # 5. 先賣後買
    trades = []
    sells = [o for o in ready if o['action'] == 'SELL']
    buys  = [o for o in ready if o['action'] == 'BUY']
    for o in sells + buys:
        sym = o['symbol']
        qty = int(abs(o['qty']))
        if qty == 0:
            continue
        contract = conn.contract(sym)          # dry run 也建合約：代號有問題當場報錯
        order = make_order(o['action'], qty, o['lmt_price'], ctx)
        if ctx.dry_run:
            log.info(f"  [DRY RUN] {ctx.order_type} {o['action']:4s} {qty:5d} {sym}"
                     f"  ({o['strategy']})")
        else:
            trade = conn.place(contract, order)
            trades.append(trade)
            # IB 的 orderId 讓 SQLite 交易紀錄寫入冪等（Excel 不寫這欄）
            o['order_id'] = str(trade.order.orderId)
            log.info(f"  {ctx.order_type} {o['action']:4s} {qty:5d} {sym}"
                     f"  orderId={trade.order.orderId}  ({o['strategy']})")
        result.placed.append(o)
        time.sleep(_ORDER_GAP_SEC)

    # 6. 等成交、彙整
    if not ctx.dry_run and result.placed:
        log.info(f"Waiting {ctx.wait_fill_sec}s for fills...")
        conn.wait(ctx.wait_fill_sec)
    if trades:
        narrator.checking_fills()
        result.fills = fill_summary(trades)
    return result


def _fields(src) -> dict:
    """order dict 或 PlannedOrder → 送單需要的欄位（price = 估價，可能沒有）。"""
    if isinstance(src, dict):
        return {'symbol': src['symbol'], 'action': src['action'], 'qty': src['qty'],
                'strategy': src.get('strategy', ''), 'price': src.get('price')}
    return {'symbol': src.symbol, 'action': src.action, 'qty': src.qty,
            'strategy': src.strategy, 'price': src.price}


def _priced(conn, pending: list, ctx) -> list[tuple]:
    """→ [(src, fields, 限價或預估價)]，順序同 pending。"""
    if ctx.order_type != 'LMT':
        return [(src, o, round(o['price'] or 0, 2)) for src, o in pending]
    quotes = conn.quotes(sorted({o['symbol'] for _, o in pending}))

    def limit(o, ref):
        return broker.limit_price(o['action'], quotes.get(o['symbol'], {}), ref)

    missing = [o['symbol'] for _, o in pending if not limit(o, o['price'])]
    refs = conn.prices(missing) if missing else {}
    return [(src, o, limit(o, o['price'] or refs.get(o['symbol'])))
            for src, o in pending]
