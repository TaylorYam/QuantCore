"""Safe, persistent Panel workflow for manual orders.

The service is deliberately small in scope: one configured Account, a US
USD-denominated stock/ETF contract, whole shares, DAY time-in-force, and
either the regular session (LMT or MKT) or an explicitly chosen pre-market /
after-hours session (LMT only). UI callers must show the returned preview and
require an explicit confirmation before calling submit().
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
import logging
import math
import re
import sqlite3
from pathlib import Path
import threading
import uuid
from typing import Callable, Iterable, Iterator
from zoneinfo import ZoneInfo

from account_utils import ROLE_MANUAL_ORDER
from market_calendar import NYSE_EARLY_CLOSES, official_sessions


MANUAL_ORDER_REF_PREFIX = "QUANTCORE_MANUAL:"
DEFAULT_STORE_PATH = Path(__file__).parent / "manual_orders.sqlite3"
SUPPORTED_MANUAL_ACCOUNTS = ("Taylor", "Rosa", "Yvonne")
_ACTIVE_STATUSES = (
    "SENDING", "SENT", "ACCEPTED", "PARTIAL", "CANCEL_PENDING",
    "CANCEL_UNKNOWN", "IB_UNKNOWN", "SUBMISSION_UNKNOWN",
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
    "CANCEL_UNKNOWN": "取消結果待確認",
    "IB_UNKNOWN": "IB 狀態待確認",
    "CANCELLED": "已取消",
    "EXPIRED": "已失效",
    "SUBMISSION_UNKNOWN": "送單結果待確認",
}
SESSION_LABELS = {"REGULAR": "一般時段", "PRE_MARKET": "盤前", "AFTER_HOURS": "盤後"}
_EXTENDED_SESSIONS = frozenset({"PRE_MARKET", "AFTER_HOURS"})
_SESSION_WINDOWS = {
    "PRE_MARKET": "美東 04:00–09:30",
    "AFTER_HOURS": "美東 16:00–20:00，提早收盤日 13:00–17:00",
}
# Extended-hours orders are not accepted in a window's last minute, leaving
# time to confirm outsideRth and, if IB dropped it, to cancel before the
# regular session (pre-market) or the window's end (after-hours).
_EXTENDED_SUBMIT_MARGIN = timedelta(minutes=1)
# TWS warning: "Attribute 'Outside Regular Trading Hours' is ignored based on
# the order type and destination. PlaceOrder is now being processed."
_IB_OUTSIDE_RTH_IGNORED = 2109
_OUTSIDE_RTH_IGNORED_REASON = "IB 回報忽略盤前盤後設定（2109）"
_IB_ACK_TIMEOUT_SECONDS = 10.0
_IB_ACK_POLL_SECONDS = 0.5
# TWS sends 2109 only to the connection that placed the order, so that
# connection keeps listening this long after IB's first status. Only a quiet
# window marks outsideRth as confirmed; anything else is cancelled.
_OUTSIDE_RTH_CONFIRM_SECONDS = 5.0
_UNACKNOWLEDGED_REASON = (
    f"IB 未在 {_IB_ACK_TIMEOUT_SECONDS:g} 秒內回覆委託，無法確認盤前盤後設定"
)
_UNCONFIRMED_REASON = "送單連線在確認盤前盤後設定前中斷，可能漏收 IB 2109"
_IB_AWAITING_ACK = frozenset({"", "ApiPending", "PendingSubmit"})
_IB_WORKING_STATUSES = frozenset({"ApiPending", "PendingSubmit", "PreSubmitted", "Submitted"})
_IB_DONE_STATUSES = frozenset({"Filled", "Cancelled", "ApiCancelled", "Inactive"})

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
log = logging.getLogger(__name__)
_IB_EVENT_LOOP_LOCAL = threading.local()


class ManualOrderError(ValueError):
    """A manual-order request failed a user-visible safety check."""


class DuplicateManualOrder(ManualOrderError):
    """An Account already has an unresolved manual order for the symbol."""


@dataclass(frozen=True)
class BrokerQuote:
    price: float
    asof: datetime = field(compare=False)
    side: str
    market_data_type: int = 1
    source: str = "IB 即時買賣報價"


def _nyse_day_clock(now: datetime) -> tuple[time, time, time] | None:
    """(Eastern wall clock, regular close, after-hours end) on an NYSE session day.

    None for naive times, non-session days, and dates beyond the registry.
    """
    if now.tzinfo is None:
        return None
    eastern = now.astimezone(ZoneInfo("America/New_York"))
    try:
        if not official_sessions(eastern.date(), eastern.date()):
            return None
    except ValueError:
        return None
    clock = eastern.time().replace(tzinfo=None)
    if eastern.date() in NYSE_EARLY_CLOSES:
        return clock, time(13), time(17)
    return clock, time(16), time(20)


def is_regular_session(now: datetime) -> bool:
    """NYSE regular session, including official early closes; fail closed beyond registry."""
    day_clock = _nyse_day_clock(now)
    if day_clock is None:
        return False
    clock, close, _after_hours_end = day_clock
    return time(9, 30) <= clock < close


def extended_session_at(now: datetime) -> str | None:
    """PRE_MARKET or AFTER_HOURS on an NYSE session day; fail closed beyond registry."""
    day_clock = _nyse_day_clock(now)
    if day_clock is None:
        return None
    clock, close, after_hours_end = day_clock
    if time(4) <= clock < time(9, 30):
        return "PRE_MARKET"
    if close <= clock < after_hours_end:
        return "AFTER_HOURS"
    return None


_IB_HOURS_DAY_RE = re.compile(r"\d{8}")
_IB_HOURS_RANGE_RE = re.compile(r"(\d{4})-(?:(\d{8}):)?(\d{4})")


def ib_trading_hours_include(trading_hours: str, time_zone_id: str,
                             now: datetime) -> bool:
    """Whether IB ContractDetails.tradingHours covers now.

    Accepts both TWS formats ("20260914:0400-20260914:2000" and the legacy
    "20260914:0400-0930,0930-1600"). Malformed data raises ValueError.
    """
    zone = ZoneInfo(time_zone_id)
    for day_spec in str(trading_hours or "").split(";"):
        day_spec = day_spec.strip()
        if not day_spec:
            continue
        day, separator, ranges = day_spec.partition(":")
        if not separator or not _IB_HOURS_DAY_RE.fullmatch(day):
            raise ValueError(f"unrecognized IB trading hours: {day_spec}")
        if ranges == "CLOSED":
            continue
        for part in ranges.split(","):
            match = _IB_HOURS_RANGE_RE.fullmatch(part)
            if match is None:
                raise ValueError(f"unrecognized IB trading hours: {day_spec}")
            start = datetime.strptime(day + match[1], "%Y%m%d%H%M").replace(tzinfo=zone)
            end = datetime.strptime(
                (match[2] or day) + match[3], "%Y%m%d%H%M",
            ).replace(tzinfo=zone)
            if start <= now < end:
                return True
    return False


def read_ib_quote(ib, contract, side: str) -> BrokerQuote:
    """Wait for a fresh, selected-side tick on a real-time IB subscription."""
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
        raise ManualOrderError("沒有有效的 IB 即時報價；請改用明確股數")
    finally:
        ticker.updateEvent -= on_update
        ib.cancelMktData(contract)


def whole_share_quantity(*, shares: int | None = None,
                         amount_usd: Decimal | str | None = None,
                         limit_price: float | Decimal | str) -> int:
    """Resolve exactly one input mode to a positive whole-share quantity."""
    if (shares is None) == (amount_usd is None):
        raise ManualOrderError("請在整股股數與美元金額之間擇一輸入")
    try:
        price = Decimal(str(limit_price))
    except (InvalidOperation, ValueError) as exc:
        raise ManualOrderError("請輸入有效的限價") from exc
    if not price.is_finite() or price <= 0:
        raise ManualOrderError("限價必須是大於零的有效數字")
    if amount_usd is None:
        if isinstance(shares, bool) or not isinstance(shares, int) or shares <= 0:
            raise ManualOrderError("股數必須是大於零的整數")
        return shares
    try:
        amount = Decimal(str(amount_usd))
    except (InvalidOperation, ValueError) as exc:
        raise ManualOrderError("請輸入有效的美元金額") from exc
    if not amount.is_finite() or amount <= 0:
        raise ManualOrderError("美元金額必須大於零")
    amount_num, amount_den = amount.as_integer_ratio()
    price_num, price_den = price.as_integer_ratio()
    quantity = (amount_num * price_den) // (amount_den * price_num)
    if quantity < 1:
        raise ManualOrderError("美元金額依自填限價換算後不足一股")
    return quantity


@dataclass(frozen=True)
class ManualOrderPreview:
    account_name: str
    account_id: str
    symbol: str
    quantity: int
    limit_price: float | None
    tif: str
    contract_id: int
    primary_exchange: str
    action: str = "BUY"
    allow_short: bool = False
    current_position: float = 0.0
    projected_position: float = 0.0
    amount_usd: Decimal | None = None
    order_type: str = "LMT"
    quote: BrokerQuote | None = None
    conversion_price: float | None = None
    session: str = "REGULAR"

    @property
    def outside_rth(self) -> bool:
        return self.session in _EXTENDED_SESSIONS

    @property
    def intent_action(self) -> str:
        return "SELL_SHORT" if self.action == "SELL" and self.projected_position < 0 else self.action

    @property
    def broker_action(self) -> str:
        return "SELL" if self.action == "SELL" else "BUY"

    @property
    def confirmation_text(self) -> str:
        # Preserve the exact user-entered numeric value in the confirmation.
        price = str(self.limit_price)
        direction = {
            "BUY": "買進", "SELL": "賣出", "SELL_SHORT": "放空", "COVER": "回補空單",
        }[self.intent_action]
        expected = (
            f"預計空單：{abs(self.projected_position):g} 股"
            if self.projected_position < 0 else
            f"預計持倉：{self.projected_position:g} 股"
        )
        amount_details = ""
        if self.amount_usd is not None:
            estimated = Decimal(self.quantity) * Decimal(str(self.conversion_price or self.limit_price))
            amount_details = (
                f"輸入美元金額：${self.amount_usd:f}（預估交易目標）\n"
                f"預估委託金額：${estimated:f}\n"
                "美元金額只用於換算股數，非成交金額上限；實際成交金額可能超出輸入金額。\n"
            )
            if self.order_type == "MKT" and self.quote is not None:
                amount_details += (
                    f"報價來源：{self.quote.source}\n"
                    f"報價時點（本機收到，UTC）：{self.quote.asof.astimezone(timezone.utc).isoformat()}\n"
                    f"換算價：{self.quote.side.upper()} ${self.quote.price:g}\n"
                )
        quantity_label = "換算整股股數" if self.amount_usd is not None else "整股股數"
        session = SESSION_LABELS[self.session]
        if self.outside_rth:
            session += f"（{_SESSION_WINDOWS[self.session]}，允許延長時段成交）"
            carries_into = (
                "會延續至當日一般時段與盤後，並" if self.session == "PRE_MARKET" else ""
            )
            validity = (
                f"效期：{self.tif}（當日有效）\n"
                f"未成交部分{carries_into}於當日盤後結束"
                "（美東 20:00，提早收盤日 17:00）失效，不跨日"
            )
        else:
            validity = f"效期：{self.tif}（當日有效）"
        return (
            f"Account：{self.account_name}\n"
            f"標的：{self.symbol}\n"
            f"方向：{direction}\n"
            f"{quantity_label}：{self.quantity:,}\n"
            f"{amount_details}"
            f"目前持倉：{self.current_position:g} 股\n"
            f"{expected}\n"
            + ("市價 MKT：未指定成交價格；成交價與金額不保證。\n"
               if self.order_type == "MKT" else "限價：$" + price + "\n")
            + f"交易時段：{session}\n"
            + validity
        )


def is_manual_order(trade_or_order) -> bool:
    """Whether an IB Trade or Order carries this tool's durable source tag."""
    order = getattr(trade_or_order, "order", trade_or_order)
    return str(getattr(order, "orderRef", "") or "").startswith(
        MANUAL_ORDER_REF_PREFIX
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_ib_event_loop() -> None:
    """Install a reusable ib_insync loop for the current worker thread."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("Manual order IB calls must run outside an active asyncio loop")
    loop = getattr(_IB_EVENT_LOOP_LOCAL, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _IB_EVENT_LOOP_LOCAL.loop = loop
    asyncio.set_event_loop(loop)


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
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS manual_orders (
                    order_ref TEXT PRIMARY KEY,
                    account_name TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    limit_price REAL NOT NULL,
                    order_type TEXT NOT NULL DEFAULT 'LMT',
                    tif TEXT NOT NULL,
                    outside_rth INTEGER NOT NULL,
                    session TEXT NOT NULL DEFAULT 'REGULAR',
                    forced_cancel_reason TEXT,
                    outside_rth_confirmed_at TEXT,
                    contract_id INTEGER NOT NULL,
                    broker_order_id INTEGER,
                    perm_id INTEGER,
                    status TEXT NOT NULL,
                    status_message TEXT NOT NULL,
                    filled_quantity INTEGER NOT NULL DEFAULT 0,
                    avg_fill_price REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_manual_orders_account_created
                   ON manual_orders(account_name, created_at DESC)"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS manual_order_notifications (
                    order_ref TEXT PRIMARY KEY,
                    terminal_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS manual_order_alerts (
                    event_key TEXT PRIMARY KEY,
                    order_ref TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    resolved_at TEXT
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS manual_order_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )"""
            )

            migrations = {
                int(row["version"])
                for row in conn.execute(
                    "SELECT version FROM manual_order_schema_migrations"
                ).fetchall()
            }
            if 1 not in migrations:
                # Add the new unknown states to the Account/symbol lock as a
                # one-time, serialized migration. BEGIN IMMEDIATE keeps the
                # old invariant in place until the replacement is committed.
                conn.execute("DROP INDEX IF EXISTS uq_manual_active_account_symbol")
                conn.execute(
                    """CREATE UNIQUE INDEX uq_manual_active_account_symbol
                       ON manual_orders(account_name, symbol)
                       WHERE status IN (
                           'SENDING', 'SENT', 'ACCEPTED', 'PARTIAL',
                           'CANCEL_PENDING', 'CANCEL_UNKNOWN', 'IB_UNKNOWN',
                           'SUBMISSION_UNKNOWN'
                       )"""
                )
                conn.execute(
                    "INSERT INTO manual_order_schema_migrations VALUES (1, ?)",
                    (_utc_now(),),
                )
            if 2 not in migrations:
                # Earlier development builds used UNIQUE(order_ref, status),
                # which suppressed a new alert if the same unknown state
                # recurred after successful reconciliation. Rebuild without
                # that constraint so each unresolved episode can be recorded.
                conn.execute(
                    "ALTER TABLE manual_order_alerts RENAME TO manual_order_alerts_v1"
                )
                conn.execute(
                    """CREATE TABLE manual_order_alerts (
                        event_key TEXT PRIMARY KEY,
                        order_ref TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        delivered_at TEXT,
                        resolved_at TEXT
                    )"""
                )
                conn.execute(
                    """INSERT INTO manual_order_alerts
                       SELECT event_key, order_ref, status, created_at,
                              delivered_at, resolved_at
                       FROM manual_order_alerts_v1"""
                )
                conn.execute("DROP TABLE manual_order_alerts_v1")
                conn.execute(
                    "INSERT INTO manual_order_schema_migrations VALUES (2, ?)",
                    (_utc_now(),),
                )
            if 3 not in migrations:
                if "order_type" not in {
                    row["name"] for row in conn.execute("PRAGMA table_info(manual_orders)")
                }:
                    conn.execute("ALTER TABLE manual_orders ADD COLUMN order_type TEXT NOT NULL DEFAULT 'LMT'")
                conn.execute(
                    "INSERT INTO manual_order_schema_migrations VALUES (3, ?)",
                    (_utc_now(),),
                )
            if 4 not in migrations:
                # Rows written before extended-hours support were always
                # regular-session orders (outside_rth was hard-coded to 0).
                if "session" not in {
                    row["name"] for row in conn.execute("PRAGMA table_info(manual_orders)")
                }:
                    conn.execute(
                        "ALTER TABLE manual_orders ADD COLUMN session TEXT NOT NULL DEFAULT 'REGULAR'"
                    )
                conn.execute(
                    "INSERT INTO manual_order_schema_migrations VALUES (4, ?)",
                    (_utc_now(),),
                )
            if 5 not in migrations:
                if "forced_cancel_reason" not in {
                    row["name"] for row in conn.execute("PRAGMA table_info(manual_orders)")
                }:
                    conn.execute(
                        "ALTER TABLE manual_orders ADD COLUMN forced_cancel_reason TEXT"
                    )
                conn.execute(
                    "INSERT INTO manual_order_schema_migrations VALUES (5, ?)",
                    (_utc_now(),),
                )
            if 6 not in migrations:
                if "outside_rth_confirmed_at" not in {
                    row["name"] for row in conn.execute("PRAGMA table_info(manual_orders)")
                }:
                    conn.execute(
                        "ALTER TABLE manual_orders ADD COLUMN outside_rth_confirmed_at TEXT"
                    )
                conn.execute(
                    "INSERT INTO manual_order_schema_migrations VALUES (6, ?)",
                    (_utc_now(),),
                )
            conn.commit()

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
                        quantity, limit_price, order_type, tif, outside_rth,
                        session, contract_id,
                        status, status_message, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              'SENDING', '正在送往 IB', ?, ?)""",
                    (order_ref, preview.account_name, preview.account_id,
                     preview.symbol, preview.intent_action, preview.quantity,
                     # Legacy schema requires a number; order_type makes zero
                     # an internal placeholder, never a displayed MKT price.
                     preview.limit_price if preview.limit_price is not None else 0,
                     preview.order_type,
                     preview.tif, int(preview.outside_rth), preview.session,
                     preview.contract_id, now, now),
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
               avg_fill_price: float | None = None,
               forced_cancel_reason: str | None = None,
               outside_rth_confirmed_at: str | None = None) -> dict | None:
        updates = {
            "status": status,
            "status_message": status_message,
            "broker_order_id": broker_order_id,
            "perm_id": perm_id,
            "filled_quantity": filled_quantity,
            "avg_fill_price": avg_fill_price,
            "forced_cancel_reason": forced_cancel_reason,
            "outside_rth_confirmed_at": outside_rth_confirmed_at,
        }
        updates = {key: value for key, value in updates.items() if value is not None}
        if not updates:
            return self.get(order_ref)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM manual_orders WHERE order_ref=?", (order_ref,)
            ).fetchone()
            if current is None:
                conn.rollback()
                return None
            # An old open-order snapshot must not reopen a terminal order.
            if current["status"] in _TERMINAL_STATUSES and updates.get("status") in _ACTIVE_STATUSES:
                conn.rollback()
                return dict(current)
            if "filled_quantity" in updates:
                updates["filled_quantity"] = max(
                    int(current["filled_quantity"] or 0),
                    int(updates["filled_quantity"] or 0),
                )
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
            if row["status"] in _TERMINAL_STATUSES:
                conn.execute(
                    """INSERT INTO manual_order_notifications (
                           order_ref, terminal_status, created_at
                       ) VALUES (?, ?, ?)
                       ON CONFLICT(order_ref) DO UPDATE SET
                           terminal_status=excluded.terminal_status
                       WHERE manual_order_notifications.delivered_at IS NULL""",
                    (order_ref, row["status"], _utc_now()),
                )
            elif row["status"] in {
                "CANCEL_UNKNOWN", "IB_UNKNOWN", "SUBMISSION_UNKNOWN",
            }:
                conn.execute(
                    """INSERT INTO manual_order_alerts (
                           event_key, order_ref, status, created_at
                       ) SELECT ?, ?, ?, ?
                       WHERE NOT EXISTS (
                           SELECT 1 FROM manual_order_alerts
                           WHERE order_ref=? AND status=? AND resolved_at IS NULL
                       )""",
                    (f"{order_ref}:{row['status']}:{uuid.uuid4().hex}",
                     order_ref, row["status"], _utc_now(),
                     order_ref, row["status"]),
                )
            else:
                # Close this unknown-state episode even after its alert was
                # delivered, so the same status can alert if it recurs later.
                conn.execute(
                    """UPDATE manual_order_alerts SET resolved_at=?
                       WHERE order_ref=? AND resolved_at IS NULL""",
                    (_utc_now(), order_ref),
                )
            conn.commit()
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

    def list_pending_notifications(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT n.order_ref, n.terminal_status, o.*
                   FROM manual_order_notifications AS n
                   JOIN manual_orders AS o USING (order_ref)
                   WHERE n.delivered_at IS NULL
                   ORDER BY n.created_at"""
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_notification_delivered(self, order_ref: str,
                                    terminal_status: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE manual_order_notifications
                   SET delivered_at=?
                   WHERE order_ref=? AND terminal_status=? AND delivered_at IS NULL""",
                (_utc_now(), order_ref, terminal_status),
            )
        return cursor.rowcount == 1

    def list_pending_alerts(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT a.event_key, a.status AS alert_status, o.*
                   FROM manual_order_alerts AS a
                   JOIN manual_orders AS o USING (order_ref)
                   WHERE a.delivered_at IS NULL AND a.resolved_at IS NULL
                     AND a.status=o.status
                   ORDER BY a.created_at"""
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_alert_delivered(self, event_key: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE manual_order_alerts SET delivered_at=?
                   WHERE event_key=? AND delivered_at IS NULL AND resolved_at IS NULL""",
                (_utc_now(), event_key),
            )
        return cursor.rowcount == 1


def _manual_order_fill_totals(fills, *, row: dict, order, trade, ctx
                              ) -> tuple[int, float | None]:
    """Aggregate only executions that can be tied to this durable manual order."""
    stored_perm_id = int(row.get("perm_id") or 0)
    order_perm_id = int(getattr(order, "permId", 0) or 0)
    perm_id = stored_perm_id or order_perm_id
    order_id = int(row.get("broker_order_id") or
                   getattr(order, "orderId", 0) or 0)
    order_ref = str(row.get("order_ref") or "")
    contract = getattr(trade, "contract", None)
    contract_id = int(row.get("contract_id") or
                       getattr(contract, "conId", 0) or 0)

    seen: set[str] = set()
    shares_total = 0.0
    notional_total = 0.0
    for fill in fills:
        execution = getattr(fill, "execution", None)
        if execution is None:
            continue
        account = str(getattr(execution, "acctNumber", "") or "")
        if account != str(ctx.account):
            continue

        fill_contract = getattr(fill, "contract", None)
        fill_contract_id = int(getattr(fill_contract, "conId", 0) or 0)
        exec_ref = str(getattr(execution, "orderRef", "") or "")
        exec_perm_id = int(getattr(execution, "permId", 0) or 0)
        exec_order_id = int(getattr(execution, "orderId", 0) or 0)
        exec_client_id = int(getattr(execution, "clientId", 0) or 0)

        exact_ref_match = bool(order_ref and exec_ref == order_ref)
        perm_match = bool(perm_id and exec_perm_id == perm_id)
        client_order_match = bool(
            order_id and exec_order_id == order_id
            and exec_client_id == int(ctx.client_id)
            and contract_id and fill_contract_id == contract_id
        )
        if not (exact_ref_match or perm_match or client_order_match):
            continue
        if contract_id and fill_contract_id and fill_contract_id != contract_id:
            continue

        exec_id = str(getattr(execution, "execId", "") or "")
        if exec_id and exec_id in seen:
            continue
        if exec_id:
            seen.add(exec_id)
        try:
            shares = float(getattr(execution, "shares", 0) or 0)
            price = float(getattr(execution, "price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(shares) or not math.isfinite(price) or shares <= 0 or price <= 0:
            continue
        shares_total += shares
        notional_total += shares * price

    if shares_total <= 0:
        return 0, None
    filled = max(0, int(round(shares_total)))
    avg_price = notional_total / shares_total
    return filled, avg_price if math.isfinite(avg_price) and avg_price > 0 else None


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
        # ApiCancelled means the API cancellation was requested before IB
        # acknowledged the order; only Cancelled confirms the balance is gone.
        "ApiCancelled": "CANCEL_PENDING",
        "Cancelled": "CANCELLED",
        "Filled": "FILLED",
        "Inactive": "REJECTED",
        "Expired": "EXPIRED",
    }.get(raw_status)
    if filled > 0 and quantity > filled and mapped not in {
            "CANCEL_PENDING", "CANCEL_UNKNOWN", "CANCELLED", "EXPIRED", "REJECTED"}:
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


def _ib_order_status(trade) -> str:
    return str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")


def _await_ib_acknowledgement(ib, trade) -> bool:
    """Pump IB events until IB answers placeOrder; False if it never does."""
    waited = 0.0
    while waited < _IB_ACK_TIMEOUT_SECONDS:
        ib.sleep(_IB_ACK_POLL_SECONDS)
        waited += _IB_ACK_POLL_SECONDS
        if _ib_order_status(trade) not in _IB_AWAITING_ACK:
            return True
    return False


def _outside_rth_confirmation_window(ib, trade, warnings: list) -> bool:
    """Keep the placing connection listening after IB's first status.

    True only if the whole window passes without warning 2109 (or the order
    finishes first). Exceptions, such as a dropped connection, propagate so
    the caller can treat the order as unconfirmed.
    """
    waited = 0.0
    while (waited < _OUTSIDE_RTH_CONFIRM_SECONDS and not warnings
           and _ib_order_status(trade) not in _IB_DONE_STATUSES):
        ib.sleep(_IB_ACK_POLL_SECONDS)
        waited += _IB_ACK_POLL_SECONDS
    return not warnings


def _find_manual_trade(trades: Iterable[object], order_ref: str, account: str):
    """This tool's IB trade for order_ref in account, or None."""
    return next((item for item in trades
                 if is_manual_order(item)
                 and str(getattr(getattr(item, "order", None),
                                 "orderRef", "") or "") == order_ref
                 and str(getattr(getattr(item, "order", None),
                                 "account", "") or "") == account),
                None)


def _serialize_service_operation(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._operation_lock:
            return method(self, *args, **kwargs)
    return wrapped


class ManualOrderService:
    """Validate, submit, and reconcile one-account manual orders."""

    def __init__(self, *, account_names: Iterable[str] | None = None,
                 store: ManualOrderStore | None = None,
                 account_ctx_factory: Callable | None = None,
                 ib_factory: Callable | None = None,
                 universe_loader: Callable[[], set[str]] | None = None,
                 order_factory: Callable | None = None,
                 quote_reader: Callable | None = None,
                 clock: Callable[[], datetime] | None = None,
                 notifier: Callable[[str, str], bool],
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
                store_path = DEFAULT_STORE_PATH
            store = ManualOrderStore(store_path)
        self.store = store
        self._account_ctx_factory = account_ctx_factory
        self._ib_factory = ib_factory
        self._universe_loader = universe_loader or _default_universe_loader
        self._order_factory = order_factory
        self._quote_reader = quote_reader or read_ib_quote
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if not callable(notifier):
            raise TypeError("ManualOrderService requires an injected notifier")
        self._notifier = notifier
        self._operation_lock = threading.RLock()
        self._monitor: ManualOrderMonitor | None = None

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

    def _make_order(self, action: str, quantity: int, price: float | None,
                    order_type: str):
        if self._order_factory:
            return self._order_factory(action, quantity, price)
        from ib_insync import LimitOrder, MarketOrder

        if order_type == "MKT":
            return MarketOrder(action, quantity)
        assert price is not None
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
            # ib_insync's synchronous API runs coroutines on the calling
            # thread's event loop. Panel workers and the lifecycle monitor
            # are new threads on modern Python, where no loop is installed by
            # default.
            _ensure_ib_event_loop()
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
                       quantity: int | None, limit_price: float,
                       amount_usd: Decimal | str | None = None,
                       ) -> tuple[str, int, float, Decimal | None]:
        normalized_symbol = _canonical_symbol(symbol)
        try:
            price = float(limit_price)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ManualOrderError("請輸入有效的限價") from exc
        if not math.isfinite(price) or price <= 0:
            raise ManualOrderError("限價必須是大於零的有效數字")
        resolved = whole_share_quantity(
            shares=quantity, amount_usd=amount_usd, limit_price=price,
        )
        amount = Decimal(str(amount_usd)) if amount_usd is not None else None
        return normalized_symbol, resolved, price, amount

    @staticmethod
    def _check_market_request(symbol: str, quantity: int | None,
                              amount_usd: Decimal | str | None,
                              ) -> tuple[str, int | None, Decimal | None]:
        normalized_symbol = _canonical_symbol(symbol)
        if (quantity is None) == (amount_usd is None):
            raise ManualOrderError("請在整股股數與美元金額之間擇一輸入")
        if quantity is not None:
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                raise ManualOrderError("股數必須是大於零的整數")
            return normalized_symbol, quantity, None
        try:
            amount = Decimal(str(amount_usd))
        except (InvalidOperation, ValueError) as exc:
            raise ManualOrderError("請輸入有效的美元金額") from exc
        if not amount.is_finite() or amount <= 0:
            raise ManualOrderError("美元金額必須大於零")
        return normalized_symbol, None, amount

    def _require_regular_session(self) -> datetime:
        now = self._clock()
        if not is_regular_session(now):
            raise ManualOrderError("MKT 只能在 NYSE 一般交易時段送出")
        return now

    def _market_quote(self, ib, contract, action: str) -> BrokerQuote:
        now = self._require_regular_session()
        side = "ask" if action in {"BUY", "COVER"} else "bid"
        try:
            quote = self._quote_reader(ib, contract, side)
        except Exception as exc:
            raise ManualOrderError(
                "沒有有效的 IB 即時報價；請改用明確股數"
            ) from exc
        if (not isinstance(quote, BrokerQuote)
                or quote.side != side or quote.market_data_type != 1
                or not isinstance(quote.asof, datetime) or quote.asof.tzinfo is None
                or not isinstance(quote.price, (int, float))
                or not math.isfinite(quote.price) or quote.price <= 0
                or not 0 <= (self._clock() - quote.asof).total_seconds() <= 15
                or not is_regular_session(quote.asof)
                or quote.asof.astimezone(ZoneInfo("America/New_York")).date()
                   != now.astimezone(ZoneInfo("America/New_York")).date()):
            raise ManualOrderError("沒有有效的 IB 即時報價；請改用明確股數")
        return quote

    @staticmethod
    def _check_session(session: str, order_type: str) -> None:
        if session not in SESSION_LABELS:
            raise ManualOrderError("請選擇有效交易時段")
        if session in _EXTENDED_SESSIONS and order_type != "LMT":
            raise ManualOrderError(
                "盤前／盤後只接受 LMT 限價單；不會改用一般時段或市價送出"
            )

    def _require_extended_session(self, session: str) -> datetime:
        now = self._clock()
        if (extended_session_at(now) != session
                or extended_session_at(now + _EXTENDED_SUBMIT_MARGIN) != session):
            raise ManualOrderError(
                f"目前不在{SESSION_LABELS[session]}時段，或距離時段結束不到 1 分鐘"
                f"（{_SESSION_WINDOWS[session]}）；不會改用一般時段送出"
            )
        return now

    @staticmethod
    def _require_extended_contract_hours(ib, contract, now: datetime) -> None:
        """Fail closed unless IB's own contract hours cover this extended-hours moment."""
        symbol = getattr(contract, "symbol", "")
        try:
            details = list(ib.reqContractDetails(contract) or [])
            if len(details) != 1:
                raise ValueError(f"expected one contract detail, got {len(details)}")
            covered = ib_trading_hours_include(
                details[0].tradingHours, details[0].timeZoneId, now,
            )
        except Exception as exc:
            raise ManualOrderError(
                f"無法確認 IB {symbol} 合約的延長時段交易時間，已阻擋委託；"
                "不會改用一般時段送出"
            ) from exc
        if not covered:
            raise ManualOrderError(
                f"IB 合約交易時間顯示 {symbol} 目前不可交易，不支援此盤前／盤後委託；"
                "不會改用一般時段送出"
            )

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

    def _sync_trades(self, ib, ctx) -> list:
        # IB connect() synchronizes this client's live trades. Use that cache:
        # ib_insync documents reqOpenOrders() as capable of returning stale
        # orders, which could otherwise keep a completed ticker locked.
        open_trades = list(ib.openTrades() or [])
        trades = list(open_trades)
        try:
            trades.extend(ib.reqCompletedOrders(apiOnly=True) or [])
        except Exception:
            # Open orders remain queryable even on older TWS API versions.
            pass
        fills = []
        try:
            fills.extend(ib.fills() or [])
        except Exception:
            pass
        try:
            # A reconnect does not necessarily replay executions into fills().
            # Request today's execution records so completed orders whose
            # orderStatus has zeroed fill fields can still be reported.
            fills.extend(ib.reqExecutions() or [])
        except Exception:
            log.debug("Manual order execution query unavailable", exc_info=True)
        unique_fills = {}
        for fill in fills:
            execution = getattr(fill, "execution", None)
            if execution is None:
                continue
            exec_id = str(getattr(execution, "execId", "") or "")
            key = exec_id or (
                getattr(execution, "permId", 0),
                getattr(execution, "orderId", 0),
                getattr(execution, "clientId", 0),
                getattr(execution, "shares", 0),
                getattr(execution, "price", 0),
            )
            unique_fills[key] = fill
        fills = tuple(unique_fills.values())
        observed_refs = set()
        for trade in trades:
            if not is_manual_order(trade):
                continue
            order = getattr(trade, "order", None)
            if str(getattr(order, "account", "") or "") != ctx.account:
                continue
            order_ref = str(getattr(order, "orderRef", "") or "")
            current = self.store.get(order_ref)
            if not current:
                continue
            observed_refs.add(order_ref)
            status, message, filled, avg_price = _status_from_trade(trade)
            fill_records = list(fills)
            fill_records.extend(getattr(trade, "fills", ()) or ())
            execution_filled, execution_avg = _manual_order_fill_totals(
                fill_records, row=current, order=order, trade=trade, ctx=ctx,
            )
            persisted_filled = int(current.get("filled_quantity") or 0)
            if execution_filled >= max(filled, persisted_filled):
                filled = execution_filled
                if execution_avg is not None:
                    avg_price = execution_avg
            elif filled >= persisted_filled:
                # The status snapshot is at least as complete as our durable
                # record, so its average remains the best available value.
                pass
            else:
                # A reconnect may expose only a subset of today's executions;
                # keep both the monotonic quantity and its prior average.
                filled = persisted_filled
                avg_price = None
            quantity = int(current["quantity"])
            if quantity > 0 and filled >= quantity:
                status = "FILLED"
                message = _STATUS_LABELS[status]
            elif (filled > 0 and status not in {
                    "CANCEL_PENDING", "CANCEL_UNKNOWN", "CANCELLED",
                    "EXPIRED", "REJECTED",
            }):
                status = "PARTIAL"
                message = _STATUS_LABELS[status]
            if current["status"] == "CANCEL_PENDING" and status in {
                "SENT", "ACCEPTED", "PARTIAL",
            }:
                # The open-order snapshot can arrive after the cancel request
                # but before IB's terminal confirmation.
                status = "CANCEL_PENDING"
                message = current["status_message"] or _STATUS_LABELS[status]
            elif current["status"] == "CANCEL_UNKNOWN" and status in {
                "SENT", "ACCEPTED", "PARTIAL",
            }:
                # Reconciliation found the order still working after a timeout.
                status = "ACCEPTED" if status != "PARTIAL" else "PARTIAL"
            if status is None:
                current = self.store.get(order_ref)
                status = ("CANCEL_UNKNOWN" if current and current["status"] in {
                    "CANCEL_PENDING", "CANCEL_UNKNOWN",
                } else "SUBMISSION_UNKNOWN")
                raw_status = str(getattr(
                    getattr(trade, "orderStatus", None), "status", ""
                ) or "未知")
                message = f"未知 IB 狀態，保持未結案：{raw_status}"
            self.store.update(
                order_ref, status=status, status_message=message,
                broker_order_id=getattr(order, "orderId", None),
                perm_id=getattr(order, "permId", None),
                filled_quantity=filled, avg_fill_price=avg_price,
            )
        for row in self.store.list_active(ctx.name):
            if row["order_ref"] in observed_refs:
                continue
            execution_filled, execution_avg = _manual_order_fill_totals(
                fills, row=row, order=None, trade=None, ctx=ctx,
            )
            if execution_filled >= int(row["quantity"]) > 0:
                self.store.update(
                    row["order_ref"], status="FILLED",
                    status_message=_STATUS_LABELS["FILLED"],
                    filled_quantity=execution_filled,
                    avg_fill_price=execution_avg,
                )
                continue
            unknown_status = (
                "CANCEL_UNKNOWN" if row["status"] in {
                    "CANCEL_PENDING", "CANCEL_UNKNOWN",
                } else "IB_UNKNOWN"
            )
            self.store.update(
                row["order_ref"], status=unknown_status,
                status_message=(
                "IB 清單未找到此手動委託；保持未結案，請再次對帳"
                ),
                filled_quantity=execution_filled,
                avg_fill_price=(
                    execution_avg
                    if execution_filled >= int(row.get("filled_quantity") or 0)
                    else None
                ),
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

    @staticmethod
    def _position(ib, ctx, contract) -> float:
        """Read this Account's synchronized IB position for the qualified contract."""
        total = 0.0
        try:
            positions = ib.positions(ctx.account)
        except Exception as exc:
            raise ManualOrderError(f"無法取得 {ctx.name} 最新持倉，已阻擋委託：{exc}") from exc
        for position in positions:
            held = position.contract
            if (str(getattr(position, "account", "") or "") != ctx.account
                    or str(getattr(held, "secType", "") or "").upper() != "STK"
                    or str(getattr(held, "currency", "") or "").upper() != "USD"
                    or int(getattr(held, "conId", 0) or 0) != int(contract.conId)):
                continue
            quantity = float(position.position)
            if not math.isfinite(quantity):
                raise ManualOrderError("IB 持倉數量無效，已阻擋委託")
            total += quantity
        if not math.isfinite(total):
            raise ManualOrderError("IB 持倉數量無效，已阻擋委託")
        return total

    @staticmethod
    def _project_position(action: str, quantity: int, current: float,
                          allow_short: bool) -> float:
        if action == "BUY":
            if allow_short:
                raise ManualOrderError("允許放空只適用於賣出")
            return current + quantity
        if action == "SELL":
            sellable = math.floor(max(0.0, current))
            if quantity > sellable and not allow_short:
                raise ManualOrderError(
                    f"可賣持倉只有 {sellable} 股；若要形成空單，須勾選「允許放空」"
                )
            return current - quantity
        if action == "COVER":
            if allow_short:
                raise ManualOrderError("允許放空只適用於賣出")
            coverable = math.floor(max(0.0, -current))
            if quantity > coverable:
                raise ManualOrderError(
                    f"可回補空單只有 {coverable} 股；由空翻多請另下買單"
                )
            return current + quantity
        raise ManualOrderError("請選擇買進、賣出或回補空單")

    def _build_preview(self, account: str, symbol: str, quantity: int | None,
                       price: float | None, action: str, allow_short: bool,
                       ctx, ib, amount_usd: Decimal | None = None,
                       order_type: str = "LMT", session: str = "REGULAR",
                       ) -> tuple[ManualOrderPreview, object]:
        extended_now = (
            self._require_extended_session(session)
            if session in _EXTENDED_SESSIONS else None
        )
        if symbol in self._universe():
            raise ManualOrderError(
                f"{symbol} 屬於 SR、Skew LS 或 SP500 歷史策略 Universe，不能手動下單"
            )
        contract, primary_exchange = self._qualify_contract(ib, symbol)
        if extended_now is not None:
            self._require_extended_contract_hours(ib, contract, extended_now)
        quote = None
        conversion_price = None
        if order_type == "MKT":
            self._require_regular_session()
            if amount_usd is not None:
                quote = self._market_quote(ib, contract, action)
                conversion_price = quote.price
                try:
                    quantity = whole_share_quantity(
                        amount_usd=amount_usd, limit_price=quote.price,
                    )
                except ManualOrderError as exc:
                    raise ManualOrderError(
                        "美元金額依 IB 即時報價換算後不足一股；請改用明確股數"
                    ) from exc
        assert quantity is not None
        open_trades = self._sync_trades(ib, ctx)
        self._assert_no_duplicate(account, ctx, symbol, open_trades)
        current = self._position(ib, ctx, contract)
        projected = self._project_position(action, quantity, current, allow_short)
        preview = ManualOrderPreview(
            account_name=account, account_id=ctx.account, symbol=symbol,
            quantity=quantity, limit_price=price, tif="DAY",
            contract_id=int(contract.conId), primary_exchange=primary_exchange,
            action=action, allow_short=allow_short,
            current_position=current, projected_position=projected,
            amount_usd=amount_usd, order_type=order_type,
            quote=quote, conversion_price=conversion_price, session=session,
        )
        return preview, contract

    @_serialize_service_operation
    def preview(self, account_name: str, symbol: str,
                quantity: int | None, limit_price: float | None, *, action: str = "BUY",
                allow_short: bool = False,
                amount_usd: Decimal | str | None = None,
                order_type: str = "LMT",
                session: str = "REGULAR") -> ManualOrderPreview:
        account = self._account_name(account_name)
        self._check_session(session, order_type)
        if order_type == "LMT":
            symbol, quantity, price, amount = self._check_request(
                account, symbol, quantity, limit_price, amount_usd,
            )
        elif order_type == "MKT" and limit_price is None:
            symbol, quantity, amount = self._check_market_request(
                symbol, quantity, amount_usd,
            )
            price = None
        else:
            raise ManualOrderError("請選擇有效委託類型與價格")
        if action not in {"BUY", "SELL", "COVER"} or not isinstance(allow_short, bool):
            raise ManualOrderError("請選擇有效方向與放空設定")
        with self._connected(account) as (ctx, ib):
            preview, _contract = self._build_preview(
                account, symbol, quantity, price, action, allow_short, ctx, ib,
                amount, order_type, session,
            )
            return preview

    @_serialize_service_operation
    def validate_preview(self, preview: ManualOrderPreview) -> ManualOrderPreview:
        """Recheck an existing preview immediately before asking for consent."""
        fresh = self.preview(
            preview.account_name, preview.symbol,
            None if preview.amount_usd is not None else preview.quantity,
            preview.limit_price, action=preview.action,
            allow_short=preview.allow_short, amount_usd=preview.amount_usd,
            order_type=preview.order_type, session=preview.session,
        )
        if fresh != preview:
            raise ManualOrderError("Account、報價、股數、合約或持倉在預覽後變更，請重新預覽")
        return fresh

    @_serialize_service_operation
    def submit(self, preview: ManualOrderPreview) -> dict:
        account = self._account_name(preview.account_name)
        self._check_session(preview.session, preview.order_type)
        if preview.order_type == "LMT":
            symbol, quantity, price, amount = self._check_request(
                account, preview.symbol,
                None if preview.amount_usd is not None else preview.quantity,
                preview.limit_price, preview.amount_usd,
            )
        elif preview.order_type == "MKT" and preview.limit_price is None:
            symbol, quantity, amount = self._check_market_request(
                preview.symbol,
                None if preview.amount_usd is not None else preview.quantity,
                preview.amount_usd,
            )
            price = None
        else:
            raise ManualOrderError("委託預覽已變更，請重新預覽")
        if (account != preview.account_name or preview.tif != "DAY"
                or preview.account_id == ""
                or (quantity is not None and quantity != preview.quantity)
                or preview.action not in {"BUY", "SELL", "COVER"}
                or not isinstance(preview.allow_short, bool)):
            raise ManualOrderError("委託預覽已變更，請重新預覽")
        with self._connected(account) as (ctx, ib):
            fresh, contract = self._build_preview(
                account, symbol, quantity, price, preview.action,
                preview.allow_short, ctx, ib, amount, preview.order_type,
                preview.session,
            )
            if fresh != preview:
                raise ManualOrderError("Account、報價、股數、合約或持倉在預覽後變更，請重新預覽")

            if preview.order_type == "MKT":
                self._require_regular_session()
            if preview.outside_rth:
                self._require_extended_session(preview.session)
            # Resolve the event before creating the durable row, so a missing
            # IB hook cannot leave an unsent order locked as SENDING.
            error_event = ib.errorEvent if preview.outside_rth else None
            outside_rth_ignored = []

            order_ref = (
                MANUAL_ORDER_REF_PREFIX
                + re.sub(r"[^A-Z0-9]+", "", account.upper())
                + ":"
                + uuid.uuid4().hex
            )
            self.store.create(preview, order_ref)
            order = self._make_order(
                preview.broker_action, fresh.quantity, price, preview.order_type,
            )
            order.action = preview.broker_action
            order.totalQuantity = fresh.quantity
            if preview.order_type == "MKT":
                from ib_insync.order import UNSET_DOUBLE
                order.lmtPrice = UNSET_DOUBLE
            else:
                order.lmtPrice = price
            order.orderType = preview.order_type
            order.tif = "DAY"
            order.outsideRth = preview.outside_rth
            order.account = ctx.account
            order.orderRef = order_ref

            def on_ib_error(req_id, error_code, _error_string, _contract=None):
                if (error_code == _IB_OUTSIDE_RTH_IGNORED
                        and req_id == getattr(order, "orderId", None)):
                    outside_rth_ignored.append(error_code)

            if error_event is not None:
                error_event += on_ib_error
            trade = None
            acknowledged = confirmed = False
            try:
                trade = ib.placeOrder(contract, order)
                self.store.update(
                    order_ref, status="SENT", status_message=_STATUS_LABELS["SENT"],
                    broker_order_id=getattr(order, "orderId", None),
                    perm_id=getattr(order, "permId", None),
                )
                if preview.outside_rth:
                    # Listen for 2109 until IB answers, then through the
                    # confirmation window on this same connection.
                    acknowledged = _await_ib_acknowledgement(ib, trade)
                    if acknowledged and not outside_rth_ignored:
                        confirmed = _outside_rth_confirmation_window(
                            ib, trade, outside_rth_ignored,
                        )
                else:
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
            finally:
                if error_event is not None:
                    error_event -= on_ib_error
            if preview.outside_rth and trade is not None:
                # Fail closed: an extended-hours order IB may be running as a
                # regular-session order is cancelled, never left working.
                if outside_rth_ignored:
                    self._force_cancel(ib, trade, order_ref, _OUTSIDE_RTH_IGNORED_REASON)
                elif not acknowledged:
                    self._force_cancel(ib, trade, order_ref, _UNACKNOWLEDGED_REASON)
                elif not confirmed:
                    self._force_cancel(ib, trade, order_ref, _UNCONFIRMED_REASON)
                else:
                    self.store.update(order_ref, outside_rth_confirmed_at=_utc_now())
        result = self.store.get(order_ref)
        assert result is not None
        result["status_label"] = _STATUS_LABELS.get(
            result["status"], result["status"]
        )
        self._dispatch_terminal_notifications()
        return result

    def _force_cancel(self, ib, trade, order_ref: str, reason: str) -> None:
        """Cancel an extended-hours order IB may run as a regular-session order.

        The reason is persisted so reconciliation keeps re-sending the cancel
        while IB still reports the order working (see _retry_forced_cancels).
        """
        row = self.store.get(order_ref)
        if row is None or row["status"] in _TERMINAL_STATUSES:
            return
        requested = f"{reason}；已要求取消，不會改以一般時段成交"
        self.store.update(
            order_ref, status="CANCEL_PENDING", status_message=requested,
            forced_cancel_reason=reason,
        )
        try:
            ib.cancelOrder(trade.order)
            ib.sleep(1.0)
        except Exception as exc:
            self.store.update(
                order_ref, status="CANCEL_UNKNOWN",
                status_message=f"{reason}；取消結果待確認：{exc}",
            )
            return
        status, message, filled, avg_price = _status_from_trade(trade)
        if status in _TERMINAL_STATUSES:
            self.store.update(
                order_ref, status=status, status_message=f"{message}；{requested}",
                filled_quantity=filled, avg_fill_price=avg_price,
            )
        else:
            self.store.update(
                order_ref, status="CANCEL_PENDING", status_message=requested,
                filled_quantity=filled, avg_fill_price=avg_price,
            )

    def _retry_forced_cancels(self, ib, ctx, open_trades) -> None:
        """Re-send forced cancels that IB has not yet started processing.

        An extended-hours order still unconfirmed here lost its submitting
        connection (crash or disconnect) before the confirmation window
        ended; a 2109 sent to that connection can no longer be received, so
        it is force-cancelled as well.
        """
        for row in self.store.list_active(ctx.name):
            reason = row.get("forced_cancel_reason")
            if not reason and row.get("outside_rth") and not row.get("outside_rth_confirmed_at"):
                reason = _UNCONFIRMED_REASON
                self.store.update(row["order_ref"], forced_cancel_reason=reason)
            if not reason:
                continue
            trade = _find_manual_trade(open_trades, row["order_ref"], ctx.account)
            status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
            if trade is not None and status in _IB_WORKING_STATUSES:
                self._force_cancel(ib, trade, row["order_ref"], reason)

    @_serialize_service_operation
    def cancel(self, order_ref: str) -> dict:
        """Request cancellation of one persisted manual order and reconcile it.

        Request acceptance is deliberately kept separate from the terminal
        cancellation state. Until IB reports Cancelled (or another terminal
        result such as Filled), the order keeps its Account/symbol lock.
        """
        with self._operation_lock:
            row = self.store.get(str(order_ref or ""))
            if (row is None or not str(order_ref or "").startswith(
                    MANUAL_ORDER_REF_PREFIX)):
                raise ManualOrderError("找不到這筆手動委託；未送出取消要求")
            if row["status"] in _TERMINAL_STATUSES:
                return self._with_status_label(row)
            if row["status"] in {"CANCEL_PENDING", "CANCEL_UNKNOWN"}:
                return self._with_status_label(row)

            account = self._account_name(row["account_name"])
            row = self.store.update(
                order_ref, status="CANCEL_PENDING",
                status_message=_STATUS_LABELS["CANCEL_PENDING"],
            ) or row
            try:
                with self._connected(account) as (ctx, ib):
                    if row["account_id"] != ctx.account:
                        raise ManualOrderError(
                            "委託所屬 Account 與目前設定不符；已阻擋取消"
                        )
                    open_trades = self._sync_trades(ib, ctx)
                    row = self.store.get(order_ref) or row
                    if row["status"] in _TERMINAL_STATUSES:
                        result = row
                    else:
                        trade = _find_manual_trade(open_trades, order_ref, ctx.account)
                        if trade is None:
                            result = self.store.update(
                                order_ref, status="CANCEL_UNKNOWN",
                                status_message=(
                                    "IB 尚未提供可驗證的手動委託；保持未結案，請刷新確認"
                                ),
                            ) or row
                        else:
                            pending = self.store.update(
                                order_ref,
                                broker_order_id=getattr(trade.order, "orderId", None),
                                perm_id=getattr(trade.order, "permId", None),
                            ) or row
                            try:
                                ib.cancelOrder(trade.order)
                                ib.sleep(1.0)
                                self._sync_trades(ib, ctx)
                            except Exception as exc:
                                pending = self.store.update(
                                    order_ref, status="CANCEL_UNKNOWN",
                                    status_message=f"取消結果待確認：{exc}",
                                ) or pending
                            result = self.store.get(order_ref) or pending
            except Exception as exc:
                row = self.store.get(order_ref) or row
                if row["status"] not in _TERMINAL_STATUSES:
                    row = self.store.update(
                        order_ref, status="CANCEL_UNKNOWN",
                        status_message=f"取消結果待確認：{exc}",
                    ) or row
                result = row
            self._dispatch_terminal_notifications()
            return self._with_status_label(result)

    @_serialize_service_operation
    def refresh_status(self) -> dict:
        """Reconnect with each owning clientId and reconcile active orders."""
        with self._operation_lock:
            active = self.store.list_active()
            accounts = list(dict.fromkeys(row["account_name"] for row in active))
            errors = []
            for account in accounts:
                try:
                    with self._connected(account) as (ctx, ib):
                        open_trades = self._sync_trades(ib, ctx)
                        self._retry_forced_cancels(ib, ctx, open_trades)
                except Exception as exc:
                    errors.append(f"{account}：{exc}")
                    for row in self.store.list_active(account):
                        unknown_status = (
                            "CANCEL_UNKNOWN" if row["status"] in {
                                "CANCEL_PENDING", "CANCEL_UNKNOWN",
                            } else "IB_UNKNOWN"
                        )
                        self.store.update(
                            row["order_ref"], status=unknown_status,
                            status_message=f"IB 對帳失敗；保持未結案：{exc}",
                        )
            self._dispatch_terminal_notifications()
            return {"orders": self.list_orders(active_only=True), "errors": errors}

    def list_orders(self, limit: int = 100, *, active_only: bool = False) -> list[dict]:
        rows = (self.store.list_active() if active_only
                else self.store.list_orders(limit=limit))
        return [self._with_status_label(row) for row in rows]

    @staticmethod
    def _with_status_label(row: dict) -> dict:
        result = dict(row)
        result["status_label"] = _STATUS_LABELS.get(
            result["status"], result["status"]
        )
        return result

    def _notification_message(self, row: dict) -> str:
        status = _STATUS_LABELS.get(row["status"], row["status"])
        notification_ref = str(row["order_ref"]).rsplit(":", 1)[-1][:12].upper()
        filled = int(row.get("filled_quantity") or 0)
        quantity = int(row["quantity"])
        fill_detail = f"成交：{filled}/{quantity} 股"
        avg_price = row.get("avg_fill_price")
        if avg_price:
            fill_detail += f"，均價 ${float(avg_price):.2f}"
        price_condition = (
            "MKT（未指定成交價格）" if row.get("order_type") == "MKT"
            else f"LMT ${float(row['limit_price']):.2f}"
        )
        session = SESSION_LABELS[row["session"]]
        forced = row.get("forced_cancel_reason")
        return (
            f"手動委託結果｜{row['account_name']}\n"
            f"委託編號：{notification_ref}\n"
            f"{row['action']} {quantity} 股 {row['symbol']}｜"
            f"{price_condition}｜{session} DAY\n"
            f"結果：{status}\n{fill_detail}"
            + (f"\n自動取消原因：{forced}" if forced else "")
        )

    def _reconciliation_alert_message(self, row: dict) -> str:
        status = _STATUS_LABELS.get(row["alert_status"], row["alert_status"])
        notification_ref = str(row["order_ref"]).rsplit(":", 1)[-1][:12].upper()
        session = SESSION_LABELS[row["session"]]
        return (
            f"手動委託需要確認｜{row['account_name']}\n"
            f"委託編號：{notification_ref}\n"
            f"{row['action']} {row['quantity']} 股 {row['symbol']}｜{session}｜{status}\n"
            "IB 尚未確認最終狀態；系統保留同 Account／標的未結案鎖，"
            "請在 Panel 刷新委託狀態。"
        )

    def _send_terminal_notification(self, account_name: str, message: str) -> bool:
        return bool(self._notifier(account_name, message))

    def _dispatch_terminal_notifications(self) -> None:
        for row in self.store.list_pending_alerts():
            try:
                delivered = self._send_terminal_notification(
                    row["account_name"], self._reconciliation_alert_message(row)
                )
            except Exception:
                log.exception("Manual order reconciliation alert failed")
                continue
            if delivered:
                self.store.mark_alert_delivered(row["event_key"])
        for row in self.store.list_pending_notifications():
            try:
                delivered = self._send_terminal_notification(
                    row["account_name"], self._notification_message(row)
                )
            except Exception:
                log.exception("Manual order terminal notification failed")
                continue
            if delivered:
                self.store.mark_notification_delivered(
                    row["order_ref"], row["terminal_status"]
                )

    def has_active_orders(self) -> bool:
        return bool(self.store.list_active())

    def has_pending_notifications(self) -> bool:
        return bool(
            self.store.list_pending_notifications()
            or self.store.list_pending_alerts()
        )

    def start_monitor(self, interval_seconds: float = 10.0) -> "ManualOrderMonitor":
        with self._operation_lock:
            if self._monitor is None:
                self._monitor = ManualOrderMonitor(self, interval_seconds)
            self._monitor.start()
            return self._monitor

    def stop_monitor(self) -> None:
        monitor = self._monitor
        if monitor is not None:
            monitor.stop()


class ManualOrderMonitor:
    """Reconcile orders and retry durable notifications outside the UI window."""

    def __init__(self, service: ManualOrderService, interval_seconds: float = 10.0):
        self.service = service
        self.interval_seconds = max(0.1, float(interval_seconds))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "ManualOrderMonitor":
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, name="manual-order-monitor", daemon=True,
            )
            self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self.service.has_active_orders() or self.service.has_pending_notifications():
                    self.service.refresh_status()
            except Exception:
                log.exception("Manual order reconciliation failed")
            self._stop_event.wait(self.interval_seconds)

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=min(2.0, self.interval_seconds + 0.1))
