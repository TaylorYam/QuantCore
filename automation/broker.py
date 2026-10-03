"""Broker（券商連線層，#185）：QuantCore 跟 IB 打交道的那一層。

- **clientId**：依帳戶＋用途分配的唯一規則（`client_id`、`panel_query_client_id`、
  `price_feed_client_id`、`paper_client_id`）。號碼與 #185 之前逐一相同；手動單認成交
  靠 clientId 比對，所以不能改。不認得的帳戶名稱直接報錯（`UnknownAccount`）。
- **Broker.connect(ctx)**：依 ctx 的 host／port／client_id 開一條連線。event loop、
  timeout、唯讀、連不上時收掉 socket 都在這裡；`check` 給送單閘門用：拿到 Gateway
  管理的帳號後先檢查，沒通過就斷線並把例外往上丟，呼叫端拿不到連線。
- **Connection**：一條已連上的連線上的讀數與送單 — 權益、持倉、參考價、送單時的
  買賣報價、掛單、建合約、送單、撤單、等待、斷線。

seam 兩側各一個 adapter：本模組（ib_insync，實盤與 paper）與
`tests/fake_broker.py`（in-memory fake，測試用）。

新 Panel（手動單、Sync Pos、接管、Portfolio）與 21:00 調倉、16:00 日度風控、
04:40 每日報告、21:31 成交回報都向 Broker 要連線（#185 第一、二階段）。讀數照搬
`ib_fetcher`，數字不變；權益、送單報價、限價統一在第三階段。
Gateway 心跳（`gateway_monitor`）是長駐連線、不讀數不送單，仍自己建 ib_insync 連線。

`Connection.ib`（ib_insync 本體）只給三個地方用：
- ManualOrderService：業務規則（盤後路由、成交對帳等）用到很多 IB 細節，#185 決定
  只換它底下的連線。
- `price_utils.robust_get_prices` 向報價 feed port 借價的那條連線：參考價的多層
  fallback 第三階段才搬進 Broker。
- `paper_verify_manual_orders`：paper 驗證要直接核對 IB 自己的委託紀錄。

不 import ib_insync（用到時才載入）：純 clientId 規則在離線工具與測試也能用。
"""
from __future__ import annotations

import asyncio
import logging
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
            conn = Connection(ib, ctx, readonly=readonly)
            if check is not None:
                check(conn.managed_accounts())
        except BaseException:
            ib.disconnect()
            raise
        return conn


class Connection:
    """一條已連上的 IB 連線（一個帳戶 × 一個用途）。可當 context manager，離開時斷線。"""

    def __init__(self, ib, ctx, *, readonly: bool = False):
        self._ib = ib
        self.ctx = ctx
        self.readonly = readonly

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

    # ── 讀數（第一階段照搬 ib_fetcher，數字不變）──────────────────────

    def nav(self) -> float:
        """權益（NetLiquidation，USD）；讀不到拋 RuntimeError。"""
        import ib_fetcher
        return ib_fetcher.get_nav(self._ib, self.ctx)

    def positions(self) -> dict[str, float]:
        """{標的: 股數}，只算股票。"""
        import ib_fetcher
        return ib_fetcher.get_positions(self._ib, self.ctx)

    def positions_detail(self) -> list:
        """非零股票持倉的 Position（含成本），標的是 constituents 寫法。"""
        import ib_fetcher
        return ib_fetcher.get_positions_detail(self._ib, self.ctx)

    def account_info(self) -> dict:
        """nav／cushion_pct／excess_liq／maint_margin；權益讀不到拋 RuntimeError。"""
        import ib_fetcher
        return ib_fetcher.get_account_info(self._ib, self.ctx)

    def prices(self, symbols: list[str]) -> dict[str, float]:
        """參考價（多層 fallback：本連線即時 → 報價 feed port → 延遲報價 → 本地 CSV）。"""
        import ib_fetcher
        # 呼叫方式和 #185 之前逐字相同：一般連線不帶 readonly（借報價的 clientId 用 99）
        if self.readonly:
            return ib_fetcher.get_prices(self._ib, symbols, self.ctx, readonly=True)
        return ib_fetcher.get_prices(self._ib, symbols, self.ctx)

    def portfolio_readings(self, out: dict) -> dict:
        """Portfolio 頁的讀數：帳號、權益／cushion／excess liquidity、股票持倉。

        沿用 #185 之前 `fetch_portfolio` 的讀法（accountSummary → accountValues 多層
        fallback；唯讀連線一定要對上設定的帳號）。第三階段改成只讀所選帳戶。
        讀到一項就寫進 out 一項（中途讀不到而拋例外時，已讀到的照舊留在 out）。
        """
        from symbol_contract import canonicalize_position

        ib, ctx, readonly = self._ib, self.ctx, self.readonly
        # 1. 信任 Gateway 真正登入的帳號（accounts.json 的 U-號可能過期）
        managed = list(ib.managedAccounts() or [])
        configured = ctx.account
        if readonly and (not configured or configured not in managed):
            raise RuntimeError("Configured Account is not available on this Gateway")
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
            if readonly:
                raise RuntimeError(f"Missing {tag} for configured Account")
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

        out.update(managed=managed, configured=configured, account=account,
                   vals_source=vals_source, n_account_values=len(vals))
        out["nav"] = _pick('NetLiquidation')
        out["cushion"] = _pick('Cushion') * 100
        out["excess_liq"] = _pick('ExcessLiquidity')

        # 3. 持倉明細
        all_pos = list(ib.positions())
        positions = [canonicalize_position(p) for p in all_pos
                     if p.contract.secType == 'STK'
                     and p.position != 0
                     and (not account or p.account == account)]
        # 若嚴格 account 過濾後空，fallback 不過濾
        if not readonly and not positions and all_pos:
            positions = [p for p in all_pos
                         if p.contract.secType == 'STK' and p.position != 0]
        out.update(positions=positions, n_positions_raw=len(all_pos))
        return out

    def quotes(self, symbols: list[str]) -> dict[str, dict]:
        """送單時的買賣報價：{標的: bid/ask/mid/last/close}，無效值為 None。"""
        import numpy as np

        from symbol_contract import canonical_symbol, ib_stock

        contracts = [ib_stock(s) for s in symbols]
        self._ib.qualifyContracts(*contracts)
        tickers = self._ib.reqTickers(*contracts)
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
