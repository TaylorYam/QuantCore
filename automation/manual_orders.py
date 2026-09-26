"""Safe, persistent Panel workflow for regular-hours manual limit buys.

The service is deliberately small in scope: one configured Account, a US
USD-denominated stock/ETF contract, whole shares, BUY LMT, regular session,
and DAY time-in-force. UI callers must show the returned preview and require
an explicit confirmation before calling submit().
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
import sqlite3
from pathlib import Path
import uuid
from typing import Callable, Iterable, Iterator

from account_utils import ROLE_MANUAL_ORDER


MANUAL_ORDER_REF_PREFIX = "QUANTCORE_MANUAL:"
SUPPORTED_MANUAL_ACCOUNTS = ("Taylor", "Rosa", "Yvonne")
_ACTIVE_STATUSES = (
    "SENDING", "SENT", "ACCEPTED", "PARTIAL", "CANCEL_PENDING",
    "SUBMISSION_UNKNOWN",
)
_TERMINAL_STATUSES = frozenset({"FILLED", "REJECTED", "CANCELLED", "EXPIRED"})
_STATUS_LABELS = {
    "SENDING": "送出中",
    "SENT": "已送出，等待 IB 回覆",
    "ACCEPTED": "IB 已接受",
    "PARTIAL": "部分成交",
    "FILLED": "已成交",
    "REJECTED": "遭 IB 拒單",
    "CANCEL_PENDING": "取消確認中",
    "CANCELLED": "已取消",
    "EXPIRED": "已失效",
    "SUBMISSION_UNKNOWN": "送單結果待確認",
}

# SMART-routed US listings returned by IB contract qualification. Unknown
# primary exchanges fail closed so a USD-denominated foreign listing cannot
# enter this US-equity-only workflow by accident.
_US_PRIMARY_EXCHANGES = frozenset({
    "AMEX", "ARCA", "BATS", "BYX", "CBOE", "EDGX", "IEX", "ISLAND",
    "MEMX", "NASDAQ", "NASDAQ.BX", "NASDAQ.CM", "NASDAQ.GM",
    "NASDAQ.NMS", "NASDAQOM", "NYSE", "NYSE AMERICAN", "NYSE ARCA",
    "NYSE MKT", "NYSEAMERICAN", "NYSEARCA", "NYSEMKT", "PSX",
})
_SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z])?$")


class ManualOrderError(ValueError):
    """A manual-order request failed a user-visible safety check."""


class DuplicateManualOrder(ManualOrderError):
    """An Account already has an unresolved manual order for the symbol."""


@dataclass(frozen=True)
class ManualOrderPreview:
    account_name: str
    account_id: str
    symbol: str
    quantity: int
    limit_price: float
    tif: str
    contract_id: int
    primary_exchange: str

    @property
    def confirmation_text(self) -> str:
        # Preserve the exact user-entered numeric value in the confirmation.
        price = str(self.limit_price)
        return (
            f"Account：{self.account_name}\n"
            f"標的：{self.symbol}\n"
            f"方向：買進\n"
            f"整股股數：{self.quantity:,}\n"
            + "限價：$" + price + "\n"
            + f"交易時段：一般時段\n"
            + f"效期：{self.tif}（當日有效）"
        )


def is_manual_order(trade_or_order) -> bool:
    """Whether an IB Trade or Order carries this tool's durable source tag."""
    order = getattr(trade_or_order, "order", trade_or_order)
    return str(getattr(order, "orderRef", "") or "").startswith(
        MANUAL_ORDER_REF_PREFIX
    )


def strategy_orders_to_cancel(trades: Iterable[object]) -> list[object]:
    """Keep manual-origin orders out of strategy-wide cancel sweeps."""
    return [trade for trade in trades if not is_manual_order(trade)]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_symbol(symbol: str) -> str:
    from symbol_contract import canonical_symbol

    value = canonical_symbol(str(symbol or "").strip())
    if not _SYMBOL_RE.fullmatch(value):
        raise ManualOrderError("標的代碼格式不符，請輸入美股代碼（例如 AAPL 或 BRK.B）")
    return value


def _default_universe_loader() -> set[str]:
    """Load the historical SP500 Universe and all SR/Skew LS instruments."""
    from signals import get_sp500_universe
    from strategies.sector_skew_ls import config as skewls_config
    from strategies.sector_rotation import config as sr_config
    from symbol_contract import canonical_symbol

    historical = get_sp500_universe(historical=True)
    if not historical:
        raise ManualOrderError("歷史 SP500 Universe 為空，無法安全檢查標的資格")
    symbols = set(historical)
    symbols.update(sr_config.SECTOR_TICKERS)
    symbols.update(skewls_config.INVERSE_TICKERS)
    symbols.update(skewls_config.DIRECT_SHORT_TICKERS)
    symbols.update({"SPY", "SSO"})
    return {canonical_symbol(symbol) for symbol in symbols}


class ManualOrderStore:
    """SQLite record of manual orders, including their last observed IB state."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS manual_orders (
                    order_ref TEXT PRIMARY KEY,
                    account_name TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    limit_price REAL NOT NULL,
                    tif TEXT NOT NULL,
                    outside_rth INTEGER NOT NULL,
                    contract_id INTEGER NOT NULL,
                    broker_order_id INTEGER,
                    perm_id INTEGER,
                    status TEXT NOT NULL,
                    status_message TEXT NOT NULL,
                    filled_quantity INTEGER NOT NULL DEFAULT 0,
                    avg_fill_price REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_manual_orders_account_created
                    ON manual_orders(account_name, created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_manual_active_account_symbol
                    ON manual_orders(account_name, symbol)
                    WHERE status IN (
                        'SENDING', 'SENT', 'ACCEPTED', 'PARTIAL',
                        'CANCEL_PENDING', 'SUBMISSION_UNKNOWN'
                    );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def create(self, preview: ManualOrderPreview, order_ref: str) -> dict:
        now = _utc_now()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """INSERT INTO manual_orders (
                        order_ref, account_name, account_id, symbol, action,
                        quantity, limit_price, tif, outside_rth, contract_id,
                        status, status_message, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'BUY', ?, ?, ?, 0, ?,
                              'SENDING', '正在送往 IB', ?, ?)""",
                    (order_ref, preview.account_name, preview.account_id,
                     preview.symbol, preview.quantity, preview.limit_price,
                     preview.tif, preview.contract_id, now, now),
                )
                row = conn.execute(
                    "SELECT * FROM manual_orders WHERE order_ref=?", (order_ref,)
                ).fetchone()
                conn.commit()
        except sqlite3.IntegrityError as exc:
            raise DuplicateManualOrder(
                f"{preview.account_name} 的 {preview.symbol} 已有未結案手動單"
            ) from exc
        return dict(row)

    def update(self, order_ref: str, *, status: str | None = None,
               status_message: str | None = None,
               broker_order_id: int | None = None, perm_id: int | None = None,
               filled_quantity: int | None = None,
               avg_fill_price: float | None = None) -> dict | None:
        updates = {
            "status": status,
            "status_message": status_message,
            "broker_order_id": broker_order_id,
            "perm_id": perm_id,
            "filled_quantity": filled_quantity,
            "avg_fill_price": avg_fill_price,
        }
        updates = {key: value for key, value in updates.items() if value is not None}
        if not updates:
            return self.get(order_ref)
        with self._connect() as conn:
            current = conn.execute(
                "SELECT * FROM manual_orders WHERE order_ref=?", (order_ref,)
            ).fetchone()
            if current is None:
                return None
            # An old open-order snapshot must not reopen a terminal order.
            if current["status"] in _TERMINAL_STATUSES and updates.get("status") in _ACTIVE_STATUSES:
                return dict(current)
            assignments = [f"{name}=?" for name in updates]
            values = list(updates.values())
            assignments.append("updated_at=?")
            values.extend((_utc_now(), order_ref))
            conn.execute(
                f"UPDATE manual_orders SET {', '.join(assignments)} WHERE order_ref=?",
                values,
            )
            row = conn.execute(
                "SELECT * FROM manual_orders WHERE order_ref=?", (order_ref,)
            ).fetchone()
        return dict(row) if row else None

    def get(self, order_ref: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM manual_orders WHERE order_ref=?", (order_ref,)
            ).fetchone()
        return dict(row) if row else None

    def list_orders(self, account_name: str | None = None,
                    limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM manual_orders"
        params: list[object] = []
        if account_name:
            sql += " WHERE account_name=?"
            params.append(account_name)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_active(self, account_name: str | None = None) -> list[dict]:
        marks = ",".join("?" for _ in _ACTIVE_STATUSES)
        sql = f"SELECT * FROM manual_orders WHERE status IN ({marks})"
        params: list[object] = list(_ACTIVE_STATUSES)
        if account_name:
            sql += " AND account_name=?"
            params.append(account_name)
        sql += " ORDER BY created_at"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def _status_from_trade(trade) -> tuple[str | None, str, int, float | None]:
    order_status = getattr(trade, "orderStatus", None)
    raw_status = str(getattr(order_status, "status", "") or "")
    filled_value = getattr(order_status, "filled", 0) or 0
    try:
        filled = max(0, int(float(filled_value)))
    except (TypeError, ValueError):
        filled = 0
    try:
        avg_fill = float(getattr(order_status, "avgFillPrice", 0) or 0)
    except (TypeError, ValueError):
        avg_fill = 0.0
    avg_fill_price = avg_fill if math.isfinite(avg_fill) and avg_fill > 0 else None

    quantity = int(float(getattr(getattr(trade, "order", None), "totalQuantity", 0) or 0))
    mapped = {
        "ApiPending": "SENT",
        "PendingSubmit": "SENT",
        "PreSubmitted": "ACCEPTED",
        "Submitted": "ACCEPTED",
        "PartiallyFilled": "PARTIAL",
        "PendingCancel": "CANCEL_PENDING",
        "ApiCancelled": "CANCELLED",
        "Cancelled": "CANCELLED",
        "Filled": "FILLED",
        "Inactive": "REJECTED",
        "Expired": "EXPIRED",
    }.get(raw_status)
    if filled > 0 and quantity > filled and mapped not in {"CANCELLED", "EXPIRED", "REJECTED"}:
        mapped = "PARTIAL"
    elif quantity > 0 and filled >= quantity:
        mapped = "FILLED"
    message = _STATUS_LABELS.get(mapped, raw_status or "等待 IB 狀態")
    if mapped == "REJECTED":
        entries = getattr(trade, "log", ()) or ()
        details = [str(getattr(entry, "message", "") or "").strip() for entry in entries]
        details = [item for item in details if item]
        if details:
            message = f"{message}：{details[-1]}"
    return mapped, message, filled, avg_fill_price


class ManualOrderService:
    """Validate, submit, and reconcile one-account manual BUY LMT orders."""

    def __init__(self, *, account_names: Iterable[str] | None = None,
                 store: ManualOrderStore | None = None,
                 account_ctx_factory: Callable | None = None,
                 ib_factory: Callable | None = None,
                 universe_loader: Callable[[], set[str]] | None = None,
                 order_factory: Callable | None = None,
                 store_path: str | Path | None = None):
        if account_names is None:
            from account_utils import load_accounts

            account_names = [item["name"] for item in load_accounts()]
        configured = {str(name).casefold(): str(name) for name in account_names}
        self.account_names = tuple(
            configured[name.casefold()] for name in SUPPORTED_MANUAL_ACCOUNTS
            if name.casefold() in configured
        )
        if not self.account_names:
            raise ManualOrderError("找不到可用 Account 設定")
        if store is None:
            if store_path is None:
                store_path = Path(__file__).parent / "manual_orders.sqlite3"
            store = ManualOrderStore(store_path)
        self.store = store
        self._account_ctx_factory = account_ctx_factory
        self._ib_factory = ib_factory
        self._universe_loader = universe_loader or _default_universe_loader
        self._order_factory = order_factory

    def _account_name(self, requested: str) -> str:
        value = str(requested or "").strip()
        match = next((name for name in self.account_names
                      if name.casefold() == value.casefold()), None)
        if match is None:
            raise ManualOrderError("請為這筆委託選擇一個有效 Account")
        return match

    def _context(self, account_name: str):
        if self._account_ctx_factory:
            return self._account_ctx_factory(account_name, ROLE_MANUAL_ORDER)
        from account_ctx import AccountCtx

        return AccountCtx.from_name(account_name, ROLE_MANUAL_ORDER)

    def _ib(self):
        if self._ib_factory:
            return self._ib_factory()
        from ib_insync import IB

        return IB()

    def _make_order(self, action: str, quantity: int, price: float):
        if self._order_factory:
            return self._order_factory(action, quantity, price)
        from ib_insync import LimitOrder

        return LimitOrder(action, quantity, price)

    def _universe(self) -> set[str]:
        try:
            raw = self._universe_loader()
        except Exception as exc:
            raise ManualOrderError(f"無法載入策略歷史 Universe，已阻擋委託：{exc}") from exc
        if not raw:
            raise ManualOrderError("策略歷史 Universe 無法確認，已阻擋委託")
        from symbol_contract import canonical_symbol

        return {canonical_symbol(symbol) for symbol in raw}

    @contextmanager
    def _connected(self, account_name: str) -> Iterator[tuple[object, object]]:
        ctx = self._context(account_name)
        ib = self._ib()
        try:
            ib.connect(ctx.host, ctx.port, clientId=ctx.client_id, timeout=10)
            managed = {str(item).strip() for item in (ib.managedAccounts() or [])}
            if ctx.account not in managed:
                raise ManualOrderError(
                    f"IB 連線未確認選定 Account {account_name}（{ctx.account}），已阻擋委託"
                )
            yield ctx, ib
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

    @staticmethod
    def _check_request(account_name: str, symbol: str,
                       quantity: int, limit_price: float) -> tuple[str, int, float]:
        normalized_symbol = _canonical_symbol(symbol)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ManualOrderError("股數必須是大於零的整數")
        try:
            price = float(limit_price)
        except (TypeError, ValueError) as exc:
            raise ManualOrderError("請輸入有效的限價") from exc
        if not math.isfinite(price) or price <= 0:
            raise ManualOrderError("限價必須是大於零的有效數字")
        return normalized_symbol, quantity, price

    @staticmethod
    def _qualify_contract(ib, symbol: str):
        from symbol_contract import canonical_symbol, ib_stock

        requested = ib_stock(symbol)
        contracts = list(ib.qualifyContracts(requested) or [])
        if len(contracts) != 1:
            raise ManualOrderError(f"IB 無法唯一確認 {symbol} 的美股/ETF 合約")
        contract = contracts[0]
        resolved_symbol = canonical_symbol(
            getattr(contract, "symbol", "") or getattr(contract, "localSymbol", "")
        )
        if (resolved_symbol != symbol
                or str(getattr(contract, "secType", "") or "").upper() != "STK"
                or str(getattr(contract, "currency", "") or "").upper() != "USD"
                or int(getattr(contract, "conId", 0) or 0) <= 0):
            raise ManualOrderError(
                f"IB 確認結果不是 {symbol} 的美元美股個股或 ETF 合約"
            )
        primary = str(getattr(contract, "primaryExchange", "") or "").strip()
        normalized_exchange = primary.upper().replace("_", " ")
        if normalized_exchange not in _US_PRIMARY_EXCHANGES:
            shown = primary or "未提供"
            raise ManualOrderError(
                f"IB 確認的主要市場為 {shown}，不在美國個股/ETF 允許市場內"
            )
        return contract, primary

    def _sync_trades(self, ib, ctx, *, include_completed: bool = True) -> list:
        # IB connect() synchronizes this client's live trades. Use that cache:
        # ib_insync documents reqOpenOrders() as capable of returning stale
        # orders, which could otherwise keep a completed ticker locked.
        open_trades = list(ib.openTrades() or [])
        trades = list(open_trades)
        if include_completed:
            try:
                trades.extend(ib.reqCompletedOrders(apiOnly=True) or [])
            except Exception:
                # Open orders remain queryable even on older TWS API versions.
                pass
        for trade in trades:
            if not is_manual_order(trade):
                continue
            order = getattr(trade, "order", None)
            if str(getattr(order, "account", "") or "") != ctx.account:
                continue
            order_ref = str(getattr(order, "orderRef", "") or "")
            if not self.store.get(order_ref):
                continue
            status, message, filled, avg_price = _status_from_trade(trade)
            if status:
                self.store.update(
                    order_ref, status=status, status_message=message,
                    broker_order_id=getattr(order, "orderId", None),
                    perm_id=getattr(order, "permId", None),
                    filled_quantity=filled, avg_fill_price=avg_price,
                )
        return open_trades

    def _assert_no_duplicate(self, account_name: str, ctx, symbol: str,
                             open_trades: Iterable[object]) -> None:
        stored = self.store.list_active(account_name)
        if any(row["symbol"] == symbol for row in stored):
            raise DuplicateManualOrder(
                f"{account_name} 的 {symbol} 已有未結案手動單；請先刷新委託狀態"
            )
        from symbol_contract import contract_symbol

        for trade in open_trades:
            if not is_manual_order(trade):
                continue
            order = getattr(trade, "order", None)
            if str(getattr(order, "account", "") or "") != ctx.account:
                continue
            if contract_symbol(getattr(trade, "contract", None)) == symbol:
                raise DuplicateManualOrder(
                    f"{account_name} 的 {symbol} 已有未結案手動單"
                )

    def preview(self, account_name: str, symbol: str,
                quantity: int, limit_price: float) -> ManualOrderPreview:
        account = self._account_name(account_name)
        symbol, quantity, price = self._check_request(
            account, symbol, quantity, limit_price
        )
        if symbol in self._universe():
            raise ManualOrderError(
                f"{symbol} 屬於 SR、Skew LS 或 SP500 歷史策略 Universe，不能手動下單"
            )
        with self._connected(account) as (ctx, ib):
            contract, primary_exchange = self._qualify_contract(ib, symbol)
            open_trades = self._sync_trades(ib, ctx)
            self._assert_no_duplicate(account, ctx, symbol, open_trades)
            return ManualOrderPreview(
                account_name=account,
                account_id=ctx.account,
                symbol=symbol,
                quantity=quantity,
                limit_price=price,
                tif="DAY",
                contract_id=int(contract.conId),
                primary_exchange=primary_exchange,
            )

    def submit(self, preview: ManualOrderPreview) -> dict:
        account = self._account_name(preview.account_name)
        symbol, quantity, price = self._check_request(
            account, preview.symbol, preview.quantity, preview.limit_price
        )
        if (account != preview.account_name or preview.tif != "DAY"
                or preview.account_id == ""):
            raise ManualOrderError("委託預覽已變更，請重新預覽")
        if symbol in self._universe():
            raise ManualOrderError(
                f"{symbol} 屬於 SR、Skew LS 或 SP500 歷史策略 Universe，不能手動下單"
            )
        with self._connected(account) as (ctx, ib):
            if ctx.account != preview.account_id:
                raise ManualOrderError("Account 設定已變更，請重新預覽")
            contract, _primary_exchange = self._qualify_contract(ib, symbol)
            if int(contract.conId) != preview.contract_id:
                raise ManualOrderError("IB 合約在預覽後變更，請重新預覽")
            open_trades = self._sync_trades(ib, ctx)
            self._assert_no_duplicate(account, ctx, symbol, open_trades)

            order_ref = (
                MANUAL_ORDER_REF_PREFIX
                + re.sub(r"[^A-Z0-9]+", "", account.upper())
                + ":"
                + uuid.uuid4().hex
            )
            self.store.create(preview, order_ref)
            order = self._make_order("BUY", quantity, price)
            order.action = "BUY"
            order.totalQuantity = quantity
            order.lmtPrice = price
            order.orderType = "LMT"
            order.tif = "DAY"
            order.outsideRth = False
            order.account = ctx.account
            order.orderRef = order_ref
            try:
                trade = ib.placeOrder(contract, order)
                self.store.update(
                    order_ref, status="SENT", status_message=_STATUS_LABELS["SENT"],
                    broker_order_id=getattr(order, "orderId", None),
                    perm_id=getattr(order, "permId", None),
                )
                # Allow immediate IB status/error callbacks to reach ib_insync.
                ib.sleep(1.0)
                status, message, filled, avg_price = _status_from_trade(trade)
                if status:
                    self.store.update(
                        order_ref, status=status, status_message=message,
                        broker_order_id=getattr(order, "orderId", None),
                        perm_id=getattr(order, "permId", None),
                        filled_quantity=filled, avg_fill_price=avg_price,
                    )
            except Exception as exc:
                # A connection failure during placeOrder may happen after IB
                # received it. Keep the account/symbol locked and require a
                # status refresh; never tell the user that it is safe to retry.
                self.store.update(
                    order_ref, status="SUBMISSION_UNKNOWN",
                    status_message=f"送單結果待確認：{exc}",
                )
            result = self.store.get(order_ref)
            assert result is not None
            result["status_label"] = _STATUS_LABELS.get(
                result["status"], result["status"]
            )
            return result

    def refresh_status(self) -> dict:
        """Reconnect with each owning clientId and reconcile active orders."""
        active = self.store.list_active()
        accounts = list(dict.fromkeys(row["account_name"] for row in active))
        errors = []
        for account in accounts:
            try:
                with self._connected(account) as (ctx, ib):
                    self._sync_trades(ib, ctx)
            except Exception as exc:
                errors.append(f"{account}：{exc}")
        return {"orders": self.list_orders(), "errors": errors}

    def list_orders(self, limit: int = 100) -> list[dict]:
        rows = self.store.list_orders(limit=limit)
        for row in rows:
            row["status_label"] = _STATUS_LABELS.get(row["status"], row["status"])
        return rows


DEFAULT_STORE_PATH = Path(__file__).parent / "manual_orders.sqlite3"
