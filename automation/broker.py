"""Broker（券商連線層，#185）：QuantCore 跟 IB 打交道的那一層。

- **clientId**：依帳戶＋用途分配的唯一規則（`client_id`、`panel_query_client_id`、
  `price_feed_client_id`、`paper_client_id`）。號碼與 #185 之前逐一相同；手動單認成交
  靠 clientId 比對，所以不能改。不認得的帳戶名稱直接報錯（`UnknownAccount`）。
- **Broker.connect(ctx)**：依 ctx 的 host／port／client_id 開一條連線。event loop、
  timeout、唯讀、連不上時收掉 socket 都在這裡；`check` 給送單閘門用：拿到 Gateway
  管理的帳號後先檢查，沒通過就斷線並把例外往上丟，呼叫端拿不到連線。
- **Connection**：一條已連上的連線上的讀數與送單 — 權益、持倉、參考價、送單時的
  買賣報價、掛單、建合約、送單、撤單、等待、斷線。

讀數與限價各只有一份（#185 第三階段）：
- **權益**：只讀所選帳戶的 NetLiquidation（USD）；cushion、excess liquidity 同樣只讀
  所選帳戶。讀不到拋 `AccountValueUnavailable`，不回傳 0、不拿別的帳戶的數字。
- **參考價**（`Connection.prices`）：算下單股數、Portfolio 與每日報告標價用；
  本連線即時 → 報價 feed port → 延遲報價 → 本地 CSV 收盤。
- **送單時的買賣報價**（`Connection.quotes`）：bid > ask 當成沒有 bid／ask，價差過大記 log。
- **限價**（`limit_price`）：買 ask×1.003、賣 bid×0.997，缺價依序 mid → last → close →
  呼叫端給的參考價。
- **手動單用的即時報價**（`realtime_quote`）：刻意比較嚴，必須是即時、有量的新報價。

seam 兩側各一個 adapter：本模組（ib_insync，實盤與 paper）與
`tests/fake_broker.py`（in-memory fake，測試用）。

新 Panel（手動單、Sync Pos、接管、Portfolio）與 21:00 調倉、16:00 日度風控、
04:40 每日報告、21:31 成交回報都向 Broker 要連線。
Gateway 心跳（`gateway_monitor`）是長駐連線、不讀數不送單，仍自己建 ib_insync 連線。

`Connection.ib`（ib_insync 本體）只給兩個地方用：
- ManualOrderService：業務規則（盤後路由、成交對帳等）用到很多 IB 細節，#185 決定
  只換它底下的連線（權益仍向 Connection 讀）。
- `paper_verify_manual_orders`：paper 驗證要直接核對 IB 自己的委託紀錄。

不 import ib_insync（用到時才載入）：純 clientId 規則在離線工具與測試也能用。
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable

log = logging.getLogger(__name__)


# ── clientId ─────────────────────────────────────────────────────────────
# 每帳戶佔 10 個號碼段（依 accounts.json 的順序）：A 20-29、B 30-39、C 40-49 …
# 段內的位置 = 用途（role）。
_CLIENT_ID_BASE = 20
_CLIENT_ID_STEP = 10

ROLE_REBALANCE = 0
ROLE_REPORT = 1
ROLE_MOO_CHECK = 2
ROLE_PANEL = 5
ROLE_MANUAL_ORDER = 6
ROLE_MONITOR = 7
# 交給策略接管（#107）自己的連線：接管單不會被排程調倉的「全部取消掛單」取消
ROLE_TAKEOVER = 8
# 新 PySide6 Panel 並行期間的唯讀連線（#147，ADR-0012）：與 Tk Panel 同時開不互搶
ROLE_PANEL_READONLY = 9

# 新 Panel 查權益／持倉（#149）：ROLE_PANEL_READONLY 被它的 Gateway 心跳長連線佔著，
# 同一個 clientId 不能同時連兩條，所以查詢另用一段號碼（Taylor 69／Rosa 79／Yvonne 89）。
# 帳戶數 ≤ 4 時不會和上面的 BASE + idx*STEP + role 撞號。
_QT_PANEL_QUERY_CLIENT_ID_BASE = 69

# 本帳戶沒有即時報價時，另開一條連線到報價 feed port 借價：
# 唯讀連線用 1000 + 本連線的 clientId，一般連線用固定的 99。
_PRICE_FEED_OFFSET = 1000
_PRICE_FEED_CLIENT_ID = 99

# paper Gateway：新 Panel 手動單用基準號（config.json qt_panel_paper.client_id，預設 92），
# Sync Pos 基準＋1、交給策略接管基準＋2（比照實盤各自一個 role）。IB 只允許同一個
# clientId 撤自己的單，所以手動單固定用基準號。paper 驗證腳本預設 91，錯開才能同時開。
PAPER_CLIENT_ID = 92
PAPER_VERIFY_CLIENT_ID = 91
# 多帳戶唯讀驗收工具（check_portfolio_queries）：16025／16035／16045，遠離所有用途
_ACCEPTANCE_CHECK_CLIENT_ID_BASE = 16025
_PAPER_OFFSETS = {ROLE_MANUAL_ORDER: 0, ROLE_REBALANCE: 1, ROLE_TAKEOVER: 2}


class UnknownAccount(ValueError):
    """accounts.json 沒有這個帳戶名稱：不分配 clientId，免得連到別的帳戶的號碼段。"""


def _account_index(account_name: str, accounts: Iterable[dict] | None) -> int:
    if accounts is None:
        from account_utils import load_accounts
        accounts = load_accounts()
    accounts = list(accounts)
    wanted = str(account_name or "").upper()
    for i, a in enumerate(accounts):
        if a["name"].upper() == wanted:
            return i
    raise UnknownAccount(f"Account '{account_name}' not found in accounts.json. "
                         f"Available: {[a['name'] for a in accounts]}")


def client_id(account_name: str, role: int = ROLE_REBALANCE, *,
              accounts: Iterable[dict] | None = None) -> int:
    """帳戶＋用途 → clientId（A=20+role、B=30+role、C=40+role …）。"""
    return _CLIENT_ID_BASE + _account_index(account_name, accounts) * _CLIENT_ID_STEP + role


def panel_query_client_id(account_name: str, *,
                          accounts: Iterable[dict] | None = None) -> int:
    """新 Panel 並行期間查權益／持倉用的 clientId（Taylor 69／Rosa 79／Yvonne 89）。"""
    if accounts is None:
        from account_utils import load_accounts
        accounts = load_accounts()
    accounts = list(accounts)
    if _CLIENT_ID_BASE + len(accounts) * _CLIENT_ID_STEP > _QT_PANEL_QUERY_CLIENT_ID_BASE:
        raise ValueError("帳戶超過 4 個，新 Panel 查詢的 clientId 會和既有用途撞號，"
                         "請先改 _QT_PANEL_QUERY_CLIENT_ID_BASE")
    return (_QT_PANEL_QUERY_CLIENT_ID_BASE
            + _account_index(account_name, accounts) * _CLIENT_ID_STEP)


def price_feed_client_id(connection_client_id: int, *, readonly: bool) -> int:
    """向報價 feed port 借價的那條連線的 clientId。"""
    if readonly:
        return _PRICE_FEED_OFFSET + int(connection_client_id)
    return _PRICE_FEED_CLIENT_ID


def acceptance_check_client_id(account_index: int) -> int:
    """唯讀驗收工具（check_portfolio_queries）第 account_index 個帳戶的 clientId。"""
    return _ACCEPTANCE_CHECK_CLIENT_ID_BASE + int(account_index) * _CLIENT_ID_STEP


def paper_client_id(base: int, role: int) -> int:
    """paper Gateway 上各用途的 clientId（base = qt_panel_paper.client_id）。"""
    try:
        return int(base) + _PAPER_OFFSETS[role]
    except KeyError:
        raise ValueError(f"paper Gateway 沒有分配 role {role} 的 clientId") from None


# ── 帳戶數值、報價與限價的規則（#185 第三階段）──────────────────────────

class AccountValueUnavailable(RuntimeError):
    """所選帳戶的權益（或 cushion 等帳戶數值）讀不到：不回傳 0，也不拿別的帳戶的數字。"""


# NetLiquidation 只認 USD；Cushion 是比例（IB 給的幣別是空字串），ExcessLiquidity
# 沿用排程一直以來的讀法（USD 或空字串）。
_NON_NAV_CURRENCIES = ("USD", "")

# 送單報價的價差超過 mid 的 5% 記 log（沿用 16:00 dip-buy）
_WIDE_SPREAD = 0.05

# 限價：買 ask×1.003、賣 bid×0.997
_LIMIT_BUY = 1.003
_LIMIT_SELL = 0.997


def limit_price(action: str, quote: dict, ref: float | None) -> float:
    """送單限價的唯一規則（Sync Pos、交給策略接管、16:00 dip-buy、21:00 以 LMT 下單時）。

    BUY：ask×1.003；SELL：bid×0.997。沒有該邊報價時依序用 mid → last → close →
    呼叫端給的參考價 ref。全都沒有回傳 0（呼叫端不送這筆）。
    quote 是 `Connection.quotes()` 的一筆（無效值為 None，錯亂報價已去掉 bid／ask）。
    """
    side, factor = ('ask', _LIMIT_BUY) if action == 'BUY' else ('bid', _LIMIT_SELL)
    base = (quote.get(side) or quote.get('mid') or quote.get('last')
            or quote.get('close') or ref)
    if not base or base <= 0:
        return 0.0
    return round(base * factor, 2)


# ── 連線 ─────────────────────────────────────────────────────────────────

def ensure_event_loop() -> None:
    """ib_insync 的同步 API 在呼叫端 thread 的 event loop 上跑；Panel 的 worker thread
    預設沒有 loop，先裝一個。"""
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _default_ib():
    from ib_insync import IB
    return IB()


class Broker:
    """開 IB 連線的工廠。ib_factory 只給測試換掉 ib_insync.IB。"""

    def __init__(self, ib_factory: Callable[[], object] | None = None):
        self._ib_factory = ib_factory or _default_ib

    def connect(self, ctx, *, timeout: float = 20, readonly: bool = False,
                check: Callable[[list[str]], None] | None = None) -> "Connection":
        """依 ctx（host／port／client_id／account）連線。

        readonly：唯讀連線（不能送單），並指定 ctx.account。
        check：連上後拿 Gateway 管理的帳號檢查；拋例外時斷線並往上丟。
        連不上或檢查沒過都會收掉 socket，呼叫端拿不到連線。
        """
        ensure_event_loop()
        ib = self._ib_factory()
        try:
            if readonly:
                ib.RequestTimeout = 20
                ib.connect(ctx.host, ctx.port, clientId=ctx.client_id, timeout=timeout,
                           readonly=True, account=ctx.account)
            else:
                ib.connect(ctx.host, ctx.port, clientId=ctx.client_id, timeout=timeout)
            conn = Connection(ib, ctx, readonly=readonly, broker=self)
            if check is not None:
                check(conn.managed_accounts())
        except BaseException:
            ib.disconnect()
            raise
        return conn


class Connection:
    """一條已連上的 IB 連線（一個帳戶 × 一個用途）。可當 context manager，離開時斷線。"""

    def __init__(self, ib, ctx, *, readonly: bool = False, broker: "Broker | None" = None):
        self._ib = ib
        self.ctx = ctx
        self.readonly = readonly
        # 參考價向報價 feed port 借價時，用同一個 Broker 開那條連線
        self._broker = broker or Broker()

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    @property
    def ib(self):
        """ib_insync 本體 — 只給模組說明列的三個地方用。"""
        return self._ib

    def managed_accounts(self) -> list[str]:
        return [str(item).strip() for item in (self._ib.managedAccounts() or [])]

    # ── 讀數（#185 第三階段：權益只剩一種讀法）─────────────────────

    def _account_value(self, tag: str, currencies: tuple[str, ...]) -> Decimal:
        """所選帳戶（ctx.account）的一項帳戶數值；讀不到、不是有限數字就拋
        AccountValueUnavailable — 不回傳 0，也不拿別的帳戶或別的幣別的數字。"""
        account = self.ctx.account
        if account:
            for v in self._ib.accountValues(account):
                if (v.tag != tag or v.currency not in currencies
                        or getattr(v, "account", account) != account):
                    continue
                try:
                    value = Decimal(str(v.value))
                except (InvalidOperation, ValueError):
                    continue
                if value.is_finite():
                    return value
        raise AccountValueUnavailable(f"Cannot get {tag} for account {account}")

    def equity(self) -> Decimal:
        """權益：所選帳戶的 NetLiquidation（USD），保留 IB 給的位數（手動單權益% 用）。
        讀不到或 ≤ 0 拋 AccountValueUnavailable。"""
        value = self._account_value("NetLiquidation", ("USD",))
        if value <= 0:
            raise AccountValueUnavailable(
                f"Cannot get NetLiquidation for account {self.ctx.account}")
        return value

    def nav(self) -> float:
        """權益（同 equity()，float）：調倉、日度風控、Portfolio、每日報告都讀這個。"""
        return float(self.equity())

    def cushion_pct(self) -> float:
        """Margin Cushion（%）；讀不到拋 AccountValueUnavailable。"""
        return float(self._account_value("Cushion", _NON_NAV_CURRENCIES)) * 100

    def excess_liq(self) -> float:
        """Excess Liquidity（USD）；讀不到拋 AccountValueUnavailable。"""
        return float(self._account_value("ExcessLiquidity", _NON_NAV_CURRENCIES))

    def account_info(self) -> dict:
        """nav／cushion_pct／excess_liq，三項都讀得到才回傳。"""
        return {"nav": self.nav(), "cushion_pct": self.cushion_pct(),
                "excess_liq": self.excess_liq()}

    def positions(self) -> dict[str, float]:
        """{標的: 股數}，只算股票（同一標的兩種寫法時加總）。"""
        from symbol_contract import contract_symbol

        positions: dict[str, float] = {}
        for p in self._ib.positions(self.ctx.account):
            if p.contract.secType != 'STK':
                continue
            sym = contract_symbol(p.contract)
            positions[sym] = positions.get(sym, 0.0) + p.position
        return positions

    def positions_detail(self) -> list:
        """非零股票持倉的 Position（含成本），標的是 constituents 寫法。"""
        from symbol_contract import canonicalize_position

        return [canonicalize_position(p) for p in self._ib.positions(self.ctx.account)
                if p.contract.secType == 'STK' and p.position != 0]

    def portfolio_readings(self, out: dict) -> dict:
        """Portfolio 頁的讀數：帳號、權益／cushion／excess liquidity、股票持倉。

        和排程同一套讀法，只讀所選帳戶：Gateway 沒登入這個帳戶就拋例外。
        讀到一項就寫進 out 一項（中途讀不到而拋例外時，已讀到的照舊留在 out）。
        """
        managed = self.managed_accounts()
        account = self.ctx.account
        if not account or account not in managed:
            raise RuntimeError("Configured Account is not available on this Gateway")
        out.update(managed=managed, account=account)
        out["nav"] = self.nav()
        out["cushion"] = self.cushion_pct()
        out["excess_liq"] = self.excess_liq()
        out["positions"] = self.positions_detail()
        return out

    def _tickers(self, symbols: list[str]) -> list:
        from symbol_contract import ib_stock

        contracts = [ib_stock(s) for s in symbols]
        self._ib.qualifyContracts(*contracts)
        return self._ib.reqTickers(*contracts)

    def prices(self, symbols: list[str]) -> dict[str, float]:
        """參考價：算下單股數、Portfolio 與每日報告標價用。{標的: 價格}，全部後備都
        失敗的標的是 0。

        依序：本連線即時（mid／last／close／延遲）→ 報價 feed port 另開連線借價 →
        本連線延遲報價 → 本地 CSV 最後收盤。
        """
        from types import SimpleNamespace

        from price_utils import load_csv_close, ticker_price
        from symbol_contract import canonical_symbol

        if not symbols:
            return {}

        # 1. 本連線即時
        canonical_symbols = [canonical_symbol(s) for s in symbols]
        prices: dict[str, float] = {}
        need_feed = []
        for t in self._tickers(canonical_symbols):
            sym = canonical_symbol(t.contract.symbol)
            p = ticker_price(t)
            if p > 0:
                prices[sym] = p
                log.info(f"  {sym}: ${p:.2f}")
            else:
                need_feed.append(sym)
                prices[sym] = 0.0

        # 2. 報價 feed port 另開連線（只為借報價，不指定帳戶）
        ctx = self.ctx
        feed_port = ctx.price_feed_port
        if need_feed and feed_port and feed_port != self._ib.client.port:
            log.info(f"  {len(need_feed)} symbols no live price, "
                     f"trying price feed port {feed_port}...")
            feed = None
            try:
                feed = self._broker.connect(
                    SimpleNamespace(host=ctx.host, port=feed_port, account="",
                                    client_id=price_feed_client_id(
                                        ctx.client_id, readonly=self.readonly)),
                    timeout=10, readonly=self.readonly)
                recovered = []
                for t in feed._tickers(need_feed):
                    sym = canonical_symbol(t.contract.symbol)
                    p = ticker_price(t)
                    if p > 0:
                        prices[sym] = p
                        recovered.append(sym)
                        log.info(f"  {sym}: ${p:.2f} (feed)")
                need_feed = [s for s in need_feed if s not in recovered]
            except Exception as e:
                log.info(f"  price feed connect failed: {e}")
            finally:
                if feed is not None:
                    feed.close()

        # 3. 本連線延遲報價
        if need_feed:
            log.info(f"  {len(need_feed)} still missing, trying delayed on own port...")
            try:
                self._ib.reqMarketDataType(3)  # 3 = delayed
                for t in self._tickers(need_feed):
                    sym = canonical_symbol(t.contract.symbol)
                    p = ticker_price(t)
                    if p > 0:
                        prices[sym] = p
                        log.info(f"  {sym}: ${p:.2f} (delayed)")
            except Exception as e:
                log.info(f"  delayed data fetch failed: {e}")
            finally:
                try:
                    self._ib.reqMarketDataType(1)
                except Exception:
                    pass

        # 4. 本地 CSV 最後收盤
        for sym in canonical_symbols:
            if prices.get(sym, 0) <= 0:
                c = load_csv_close(sym)
                if c > 0:
                    prices[sym] = c
                    log.info(f"  {sym}: ${c:.2f} (CSV close)")
                else:
                    prices[sym] = 0.0
                    log.info(f"  {sym}: NO PRICE")
        return prices

    def quotes(self, symbols: list[str]) -> dict[str, dict]:
        """送單時的買賣報價（算限價用）：{標的: bid/ask/mid/last/close}，無效值為 None。

        bid > ask 的錯亂報價當成沒有 bid／ask（mid 也不用）；價差超過 5% 記 log。
        """
        import numpy as np

        from symbol_contract import canonical_symbol

        def valid(x):
            return x if (x and x > 0 and np.isfinite(x)) else None

        quotes = {}
        for t in self._tickers(symbols):
            sym = canonical_symbol(t.contract.symbol)
            bid, ask = valid(t.bid), valid(t.ask)
            mid = t.midpoint() if (t.midpoint() and np.isfinite(t.midpoint())) else None
            if bid is not None and ask is not None:
                if bid > ask:
                    log.warning(f"  {sym}: crossed quote bid={bid} > ask={ask}")
                    bid = ask = mid = None
                elif (ask - bid) / ((ask + bid) / 2) > _WIDE_SPREAD:
                    log.warning(f"  {sym}: wide spread bid={bid} ask={ask}")
            quotes[sym] = {
                'bid': bid, 'ask': ask, 'mid': mid,
                'last': valid(t.last), 'close': valid(t.close),
            }
        return quotes

    # ── 送單 ─────────────────────────────────────────────────────────

    def open_orders(self, *, all_clients: bool = False) -> list:
        """掛單（ib_insync Trade）。all_clients=False：這條連線（這個 clientId）看得到的；
        True：這個 Gateway 上所有 clientId（含 TWS）的。"""
        if all_clients:
            return list(self._ib.reqAllOpenOrders() or [])
        return list(self._ib.openTrades() or [])

    def trades(self) -> list:
        """這條連線這次看到的委託（ib_insync Trade，含今天已結束的）。"""
        return list(self._ib.trades())

    def fills(self) -> list:
        """這條連線這次看到的成交（ib_insync Fill）。"""
        return list(self._ib.fills())

    def cancel(self, order) -> None:
        """撤單；IB 只允許撤這個 clientId 自己的單。"""
        self._ib.cancelOrder(order)

    def contract(self, symbol: str):
        """這個標的的 IB 股票合約（已向 IB 確認）。"""
        from symbol_contract import ib_stock

        contract = ib_stock(symbol)
        self._ib.qualifyContracts(contract)
        return contract

    def place(self, contract, order):
        """送出一筆單，回傳 ib_insync Trade（狀態與成交會在等待時更新）。"""
        return self._ib.placeOrder(contract, order)

    def wait(self, seconds: float) -> None:
        """等 IB 回報（期間會處理成交、狀態更新等事件）。"""
        self._ib.sleep(seconds)

    def close(self) -> None:
        """斷線；已斷或斷線出錯都不拋。"""
        try:
            if self._ib.isConnected():
                self._ib.disconnect()
        except Exception:
            pass


# ── 手動單用的即時報價 ────────────────────────────────────────────────────
# 刻意比參考價、送單報價嚴：一定要是即時（market data type 1）、有量、這次訂閱新到的
# 報價，否則手動單拒絕以權益%／金額換算股數（規則在 ManualOrderService._market_quote）。

@dataclass(frozen=True)
class BrokerQuote:
    price: float
    asof: datetime = field(compare=False)
    side: str
    market_data_type: int = 1
    source: str = "IB 即時買賣報價"


class NoRealtimeQuote(RuntimeError):
    """等不到有效的 IB 即時報價。"""


def realtime_quote(ib, contract, side: str) -> BrokerQuote:
    """Wait for a fresh, selected-side tick on a real-time IB subscription.

    ib = ib_insync 本體（手動單用 `Connection.ib`）。
    """
    if side not in {"bid", "ask"}:
        raise ValueError(side)
    ib.reqMarketDataType(1)
    ticker = ib.reqMktData(contract, "", False, False)
    seen = None

    def on_update(updated):
        nonlocal seen
        for tick in updated.ticks:
            if tick.tickType == (1 if side == "bid" else 2):
                seen = BrokerQuote(tick.price, tick.time, side,
                                   updated.marketDataType)

    ticker.updateEvent += on_update
    try:
        for _ in range(25):
            ib.sleep(0.2)
            if seen is not None and ticker.marketDataType == 1:
                current = ticker.bid if side == "bid" else ticker.ask
                size = ticker.bidSize if side == "bid" else ticker.askSize
                if current == seen.price and math.isfinite(size) and size > 0:
                    return seen
        raise NoRealtimeQuote("沒有有效的 IB 即時報價；請改用明確股數")
    finally:
        ticker.updateEvent -= on_update
        ib.cancelMktData(contract)
