"""Broker 的 in-memory fake（#185）：seam 另一側的 adapter，測試共用這一份。

和 `broker.Broker`／`broker.Connection` 同一個 interface：`FakeBroker.connect(ctx, …)`
回傳 `FakeConnection`，提供權益、持倉、參考價、送單報價、掛單、建合約、送單、撤單、
等待、斷線，以及手動單用的合約確認與細節、已完成委託、成交明細、日線收盤、錯誤事件、
即時報價（#228）。帳戶狀態（Gateway 管理的帳號、權益、持倉、報價、掛單）與送單行為都是
FakeBroker 上的屬性，測試直接設定；連線、送單、撤單都記下來給測試斷言。

送單行為 `behavior`（單送出時是 `placed_status`，預設 PreSubmitted；第一次 wait() 才變化，
像真的 IB）：
  - "fill"     全部成交，持倉跟著變
  - "partial"  成交 partial_qty 股，狀態 Submitted
  - "working"  一直掛著（Submitted），沒有成交
  - "reject"   IB 拒單（Inactive），log 留下原因
  - "raise"    送單當下連線出錯（ConnectionError）
送出的單也放進 `open_orders`（像 IB 的掛單清單），測試自己把它移到 `completed_trades`。

手動單的情境（盤前盤後的 2109、撤單逾時、多帳戶持倉與權益）：
  - `next_status`／`next_log`：只套用在下一張送出的單
  - `place_errors`：送單後下一次 wait() 才送到這條連線的錯誤事件（IB 非同步回報）；
    `pending_errors` 是已排隊、下一次 wait() 送出的 (reqId, code, 訊息)
  - `sleep_actions`：{n: action(連線, 最後一張單)}，送單後第 n 次 wait() 執行
  - `cancel_status`：撤單後 IB 回報的狀態（None = 不變）；`cancel_error`：撤單出錯
  - `positions_by_account`／`net_liquidation`：依帳號分開的持倉與 NetLiquidation
    （None = 每個帳號都用 `positions`／`nav`）

排程入口（21:00／16:00／04:40／21:31）在用到時才取 `broker.Broker`：測試用
`install(monkeypatch, fake)` 把它換成這個 fake。
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace as NS
from typing import Callable

QUOTE = {"bid": 99.9, "ask": 100.1, "mid": 100.0, "last": 100.0, "close": 100.0}


def install(monkeypatch, fake: "FakeBroker") -> "FakeBroker":
    """排程入口與 Portfolio 快照用到的 broker.Broker 換成 fake。"""
    import broker
    monkeypatch.setattr(broker, "Broker", lambda *_a, **_k: fake)
    return fake


def install_connect(monkeypatch, connect: Callable) -> None:
    """broker.Broker().connect(ctx, …) 換成 connect(ctx, …)（測試自己決定連線結果）。"""
    import broker
    monkeypatch.setattr(broker, "Broker", lambda *_a, **_k: NS(connect=connect))


class FakeBroker:
    def __init__(self, *, managed=("DU001",), nav: float = 100_000.0,
                 positions: dict[str, float] | None = None,
                 price: float = 100.0, behavior: str = "fill", partial_qty: int = 100):
        self.managed = list(managed)
        self.nav = nav
        self.positions = dict(positions or {})
        self.avg_cost: dict[str, float] = {}          # 持倉成本；沒列的是 1.0
        self.detail: list | None = None               # 指定持倉明細（None = 由持倉產生）
        self.cushion_pct, self.excess_liq = 42.0, nav * 0.4
        self.session_trades: list = []                # 這次連線前就有的今日委託／成交
        self.session_fills: list = []
        self.connect_error: BaseException | None = None   # 連不上 Gateway
        self.fail: dict[str, BaseException] = {}      # 讀數方法名 → 拋出的例外
        self.price = price                    # 每一檔的參考價
        self.quote = dict(QUOTE)              # 每一檔的送單報價
        self.open_orders: list = []           # Gateway 上的掛單（Trade 形狀）
        self.behavior = behavior
        self.partial_qty = partial_qty
        self.placed_status = "PreSubmitted"   # 單送出當下的 IB 狀態
        self.on_read: Callable[[], None] | None = None   # 讀數前呼叫（測試卡住計算用）
        # 手動單的情境（#228，見模組說明）
        self.positions_by_account: dict[str, dict[str, float]] | None = None
        self.net_liquidation: dict[str, str] | None = None
        self.con_id = 123456                  # qualify() 補上的 conId
        self.primary_exchange = "NASDAQ"
        self.trading_hours: str | None = None     # 合約細節的 tradingHours（None = 查不到）
        self.overnight_hours: str | None = None   # 路由到 OVERNIGHT 時的 tradingHours
        self.valid_exchanges = "SMART"
        self.contract_time_zone = "US/Eastern"
        self.completed_trades: list = []      # IB 的已完成委託（可帶 completed_status）
        self.daily_bars: list = []            # [(日期, 收盤)]
        self.history_error: BaseException | None = None
        self.realtime = None                  # realtime_quote() 回的 BrokerQuote；None = 等不到
        self.next_status: str | None = None
        self.next_log: list = []
        self.place_errors: list = []
        self.pending_errors: list = []
        self.sleep_actions: dict[int, Callable] = {}
        self.sleeps_since_place = 0
        self.cancel_status: str | None = None
        self.cancel_error: str | None = None
        # 記錄
        self.connects: list[tuple[str, int, int]] = []   # (host, port, clientId)
        self.ctxs: list = []                              # 讀權益時連線的 ctx
        self.placed: list = []                            # 送出的單（NS，見 place）
        self.trades: list = []                            # 送出的單（Trade 形狀）
        self.cancelled: list = []
        self.waits: list[float] = []
        self.history_requests: list[str] = []             # 讀日線時合約的 exchange
        self.closed = 0

    @property
    def client_ids(self) -> list[int]:
        """連線用過的 clientId（依連線順序）。"""
        return [client_id for _host, _port, client_id in self.connects]

    def connect(self, ctx, *, timeout: float = 20, readonly: bool = False) -> "FakeConnection":
        self.connects.append((ctx.host, ctx.port, ctx.client_id))
        if self.connect_error is not None:
            raise self.connect_error
        return FakeConnection(self, ctx, readonly)


class _Event:
    """ib_insync 事件用到的部分：+=、-=、emit。"""

    def __init__(self):
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self

    def emit(self, *args) -> None:
        for handler in list(self.handlers):
            handler(*args)


class FakeConnection:
    def __init__(self, broker: FakeBroker, ctx, readonly: bool):
        self._broker = broker
        self.ctx = ctx
        self.readonly = readonly
        self.connected = True
        self._errors = _Event()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _read(self, what: str = "") -> None:
        if what in self._broker.fail:
            raise self._broker.fail[what]
        if self._broker.on_read is not None:
            self._broker.on_read()

    def managed_accounts(self) -> list[str]:
        return list(self._broker.managed)

    @property
    def error_event(self) -> _Event:
        return self._errors

    def equity(self) -> Decimal:
        from broker import AccountValueUnavailable

        self._read("nav")
        b = self._broker
        b.ctxs.append(self.ctx)
        if b.net_liquidation is None:
            return Decimal(str(b.nav))
        value = b.net_liquidation.get(self.ctx.account)
        if value is None or Decimal(str(value)) <= 0:
            raise AccountValueUnavailable(
                f"Cannot get NetLiquidation for account {self.ctx.account}")
        return Decimal(str(value))

    def nav(self) -> float:
        return float(self.equity())

    def cushion_pct(self) -> float:
        self._read("account_info")
        return self._broker.cushion_pct

    def excess_liq(self) -> float:
        self._read("account_info")
        return self._broker.excess_liq

    def positions(self) -> dict[str, float]:
        self._read("positions")
        return dict(self._broker.positions)

    def positions_detail(self) -> list:
        self._read("positions_detail")
        b = self._broker
        if b.detail is not None:
            return list(b.detail)
        return [NS(contract=NS(symbol=s, secType="STK"), position=q,
                   avgCost=b.avg_cost.get(s, 1.0))
                for s, q in b.positions.items() if q]

    def position_records(self) -> list:
        b = self._broker
        book = (b.positions if b.positions_by_account is None
                else b.positions_by_account[self.ctx.account])
        return [NS(account=self.ctx.account, position=quantity, contract=NS(
                    symbol=symbol, secType="STK", currency="USD", conId=b.con_id))
                for symbol, quantity in book.items()]

    def account_info(self) -> dict:
        return {"nav": self.nav(), "cushion_pct": self.cushion_pct(),
                "excess_liq": self.excess_liq()}

    def portfolio_readings(self, out: dict) -> dict:
        managed = self.managed_accounts()
        if not self.ctx.account or self.ctx.account not in managed:
            raise RuntimeError("Configured Account is not available on this Gateway")
        out.update(managed=managed, account=self.ctx.account)
        out["nav"] = self.nav()
        out["cushion"] = self.cushion_pct()
        out["excess_liq"] = self.excess_liq()
        out["positions"] = self.positions_detail()
        return out

    def trades(self) -> list:
        self._read("trades")
        return list(self._broker.session_trades) + list(self._broker.trades)

    def fills(self) -> list:
        self._read("fills")
        b = self._broker
        return list(b.session_fills) + [f for t in b.trades for f in t.fills]

    def executions(self) -> list:
        return list(self._broker.session_fills)

    def completed_orders(self) -> tuple[list, dict[str, str]]:
        trades = list(self._broker.completed_trades)
        statuses = {str(getattr(t.order, "orderRef", "") or ""):
                    str(getattr(t, "completed_status", "") or "") for t in trades}
        return trades, statuses

    def prices(self, symbols) -> dict[str, float]:
        return {s: self._broker.price for s in symbols}

    def quotes(self, symbols) -> dict[str, dict]:
        return {s: dict(self._broker.quote) for s in symbols}

    def realtime_quote(self, contract, side: str):
        from broker import NoRealtimeQuote

        if side not in {"bid", "ask"}:
            raise ValueError(side)
        if self._broker.realtime is None:
            raise NoRealtimeQuote("沒有有效的 IB 即時報價；請改用明確股數")
        return self._broker.realtime

    def open_orders(self, *, all_clients: bool = False) -> list:
        return list(self._broker.open_orders)

    def cancel(self, order) -> None:
        b = self._broker
        b.cancelled.append(order)
        if b.cancel_error:
            raise TimeoutError(b.cancel_error)
        if b.cancel_status is not None:
            for trade in b.open_orders:
                if trade.order is order:
                    trade.orderStatus.status = b.cancel_status

    def contract(self, symbol: str):
        from symbol_contract import ib_stock
        return ib_stock(symbol)

    def qualify(self, contract) -> list:
        contract.conId = self._broker.con_id
        contract.primaryExchange = self._broker.primary_exchange
        return [contract]

    def contract_details(self, contract) -> list:
        b = self._broker
        overnight = getattr(contract, "exchange", "") == "OVERNIGHT"
        hours = b.overnight_hours if overnight else b.trading_hours
        if hours is None:
            return []
        return [NS(contract=contract, tradingHours=hours,
                   liquidHours=hours if overnight else "20260914:0930-20260914:1600",
                   timeZoneId=b.contract_time_zone, validExchanges=b.valid_exchanges)]

    def daily_closes(self, contract) -> list:
        b = self._broker
        b.history_requests.append(getattr(contract, "exchange", ""))
        if b.history_error:
            raise b.history_error
        return list(b.daily_bars)

    def place(self, contract, order):
        b = self._broker
        if b.behavior == "raise":
            raise ConnectionError("socket closed")
        order.orderId = 500 + len(b.placed)
        order.permId = 9000 + order.orderId
        b.placed.append(NS(symbol=contract.symbol, action=order.action,
                           qty=int(order.totalQuantity), order_type=order.orderType,
                           lmt=getattr(order, "lmtPrice", None),
                           ref=getattr(order, "orderRef", ""),
                           outside_rth=getattr(order, "outsideRth", False),
                           tif=getattr(order, "tif", ""),
                           account=getattr(order, "account", ""),
                           client_id=self.ctx.client_id, port=self.ctx.port))
        if getattr(contract, "exchange", "") == "OVERNIGHT" and order.tif == "DAY":
            # IB records an OVERNIGHT-routed DAY order with its Overnight tif.
            order.tif = "OVERNIGHT"
        status = b.next_status or b.placed_status
        trade = NS(order=order, contract=contract, fills=[], log=list(b.next_log),
                   orderStatus=NS(status=status, filled=0, avgFillPrice=0.0))
        b.next_status, b.next_log = None, []
        # IB 的委託警告非同步送達：呼叫端在 wait() 時才收到
        b.pending_errors.extend((order.orderId, code, message)
                                for code, message in b.place_errors)
        b.place_errors = []
        b.sleeps_since_place = 0
        b.trades.append(trade)
        b.open_orders.append(trade)
        return trade

    def wait(self, seconds: float) -> None:
        b = self._broker
        b.waits.append(seconds)
        b.sleeps_since_place += 1
        pending, b.pending_errors = b.pending_errors, []
        for req_id, code, message in pending:
            self.error_event.emit(req_id, code, message, None)
        action = b.sleep_actions.get(b.sleeps_since_place)
        if action is not None and b.trades:
            action(self, b.trades[-1])
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
        # price = 這筆成交價；avgPrice = IB 的累計均價（只有一筆時兩者相同）
        trade.fills.append(NS(execution=NS(shares=qty, price=price, avgPrice=price)))
        trade.orderStatus.status, trade.orderStatus.filled = status, qty

    def close(self) -> None:
        if self.connected:
            self.connected = False
            self._broker.closed += 1
