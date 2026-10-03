"""Broker 的 in-memory fake（#185）：seam 另一側的 adapter，測試共用這一份。

和 `broker.Broker`／`broker.Connection` 同一個 interface：`FakeBroker.connect(ctx, …)`
回傳 `FakeConnection`，提供權益、持倉、參考價、送單報價、掛單、建合約、送單、撤單、
等待、斷線。帳戶狀態（Gateway 管理的帳號、權益、持倉、報價、掛單）與送單行為都是
FakeBroker 上的屬性，測試直接設定；連線、送單、撤單都記下來給測試斷言。

送單行為 `behavior`（單送出時是 PreSubmitted，第一次 wait() 才變化，像真的 IB）：
  - "fill"     全部成交，持倉跟著變
  - "partial"  成交 partial_qty 股，狀態 Submitted
  - "working"  一直掛著（Submitted），沒有成交
  - "reject"   IB 拒單（Inactive），log 留下原因
  - "raise"    送單當下連線出錯（ConnectionError）

排程入口（21:00／16:00／04:40／21:31）在用到時才取 `broker.Broker`：測試用
`install(monkeypatch, fake)` 把它換成這個 fake。

ManualOrderService 用 ib_insync 本體（`Connection.ib`），它的測試把真的
`broker.Broker` 接在 test_manual_orders 的假 IB 上，不用這個 fake。
"""
from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Callable

QUOTE = {"bid": 99.9, "ask": 100.1, "mid": 100.0, "last": 100.0, "close": 100.0}
_DONE = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}


def install(monkeypatch, fake: "FakeBroker") -> "FakeBroker":
    """排程入口與 ib_fetcher.connect 用到的 broker.Broker 換成 fake。"""
    import broker
    monkeypatch.setattr(broker, "Broker", lambda *_a, **_k: fake)
    return fake


class FakeBroker:
    def __init__(self, *, managed=("DU001",), nav: float = 100_000.0,
                 positions: dict[str, float] | None = None,
                 price: float = 100.0, behavior: str = "fill", partial_qty: int = 100):
        self.managed = list(managed)
        self.nav = nav
        self.positions = dict(positions or {})
        self.avg_cost: dict[str, float] = {}          # 持倉成本；沒列的是 1.0
        self.detail: list | None = None               # 指定持倉明細（None = 由持倉產生）
        self.cushion_pct, self.excess_liq, self.maint_margin = 42.0, nav * 0.4, nav * 0.3
        self.session_trades: list = []                # 這次連線前就有的今日委託／成交
        self.session_fills: list = []
        self.connect_error: BaseException | None = None   # 連不上 Gateway
        self.fail: dict[str, BaseException] = {}      # 讀數方法名 → 拋出的例外
        self.price = price                    # 每一檔的參考價
        self.quote = dict(QUOTE)              # 每一檔的送單報價
        self.open_orders: list = []           # Gateway 上的掛單（Trade 形狀）
        self.behavior = behavior
        self.partial_qty = partial_qty
        self.on_read: Callable[[], None] | None = None   # 讀數前呼叫（測試卡住計算用）
        # 記錄
        self.connects: list[tuple[str, int, int]] = []   # (host, port, clientId)
        self.ctxs: list = []                              # 讀權益時連線的 ctx
        self.placed: list = []                            # 送出的單（NS，見 place）
        self.trades: list = []
        self.cancelled: list = []
        self.waits: list[float] = []
        self.closed = 0
        self.refused = 0

    def connect(self, ctx, *, timeout: float = 20, readonly: bool = False,
                check=None) -> "FakeConnection":
        self.connects.append((ctx.host, ctx.port, ctx.client_id))
        if self.connect_error is not None:
            raise self.connect_error
        conn = FakeConnection(self, ctx, readonly)
        if check is not None:
            try:
                check(conn.managed_accounts())
            except BaseException:
                self.refused += 1
                conn.close()
                raise
        return conn


class FakeConnection:
    def __init__(self, broker: FakeBroker, ctx, readonly: bool):
        self._broker = broker
        self.ctx = ctx
        self.readonly = readonly
        self.connected = True

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    @property
    def ib(self):
        raise AttributeError("in-memory FakeBroker 沒有 ib_insync 本體")

    def _read(self, what: str = "") -> None:
        if what in self._broker.fail:
            raise self._broker.fail[what]
        if self._broker.on_read is not None:
            self._broker.on_read()

    def managed_accounts(self) -> list[str]:
        return list(self._broker.managed)

    def nav(self) -> float:
        self._read("nav")
        self._broker.ctxs.append(self.ctx)
        return float(self._broker.nav)

    def positions(self) -> dict[str, float]:
        self._read("positions")
        return dict(self._broker.positions)

    def positions_detail(self) -> list:
        b = self._broker
        if b.detail is not None:
            return list(b.detail)
        return [NS(contract=NS(symbol=s, secType="STK"), position=q,
                   avgCost=b.avg_cost.get(s, 1.0))
                for s, q in b.positions.items() if q]

    def account_info(self) -> dict:
        self._read("account_info")
        b = self._broker
        return {"nav": float(b.nav), "cushion_pct": b.cushion_pct,
                "excess_liq": b.excess_liq, "maint_margin": b.maint_margin}

    def trades(self) -> list:
        return list(self._broker.session_trades) + list(self._broker.trades)

    def fills(self) -> list:
        b = self._broker
        return list(b.session_fills) + [f for t in b.trades for f in t.fills]

    def prices(self, symbols) -> dict[str, float]:
        return {s: self._broker.price for s in symbols}

    def quotes(self, symbols) -> dict[str, dict]:
        return {s: dict(self._broker.quote) for s in symbols}

    def open_orders(self, *, all_clients: bool = False) -> list:
        return list(self._broker.open_orders)

    def cancel(self, order) -> None:
        self._broker.cancelled.append(order)

    def contract(self, symbol: str):
        from symbol_contract import ib_stock
        return ib_stock(symbol)

    def place(self, contract, order):
        b = self._broker
        if b.behavior == "raise":
            raise ConnectionError("socket closed")
        order.orderId = 500 + len(b.placed)
        b.placed.append(NS(symbol=contract.symbol, action=order.action,
                           qty=int(order.totalQuantity), order_type=order.orderType,
                           lmt=getattr(order, "lmtPrice", None),
                           ref=getattr(order, "orderRef", ""),
                           outside_rth=getattr(order, "outsideRth", False),
                           tif=getattr(order, "tif", ""),
                           account=getattr(order, "account", ""),
                           client_id=self.ctx.client_id, port=self.ctx.port))
        trade = NS(order=order, contract=contract, fills=[], log=[],
                   orderStatus=NS(status="PreSubmitted", filled=0))
        b.trades.append(trade)
        return trade

    def wait(self, seconds: float) -> None:
        b = self._broker
        b.waits.append(seconds)
        for trade in b.trades:
            if trade.orderStatus.status != "PreSubmitted":
                continue
            sym, qty = trade.contract.symbol, int(trade.order.totalQuantity)
            sign = 1 if trade.order.action == "BUY" else -1
            if b.behavior == "fill":
                self._fill(trade, qty, "Filled")
                b.positions[sym] = b.positions.get(sym, 0.0) + sign * qty
            elif b.behavior == "partial":
                self._fill(trade, b.partial_qty, "Submitted")
                b.positions[sym] = b.positions.get(sym, 0.0) + sign * b.partial_qty
            elif b.behavior == "working":
                trade.orderStatus.status = "Submitted"
            elif b.behavior == "reject":
                trade.orderStatus.status = "Inactive"
                trade.log.append(NS(message="Order rejected - reason: margin"))

    def _fill(self, trade, qty: int, status: str) -> None:
        price = getattr(trade.order, "lmtPrice", None) or self._broker.price
        if price > 1e300:                       # ib_insync 的 UNSET_DOUBLE（非限價單）
            price = self._broker.price
        trade.fills.append(NS(execution=NS(shares=qty, avgPrice=price)))
        trade.orderStatus.status, trade.orderStatus.filled = status, qty

    def close(self) -> None:
        if self.connected:
            self.connected = False
            self._broker.closed += 1
