"""Manual-order form parsing and display text, shared by the Tk and Qt Panels.

No GUI imports: the windows read their widgets into a ManualOrderForm, call
these functions, and put the returned strings on screen.  Colors come back as
theme key names ("text", "text_dim", "yellow") so each toolkit maps its own.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from manual_order_status import ACTIVE_STATUSES, TERMINAL_STATUSES, UNCONFIRMED_STATUSES

SHARES_MODE = "整股股數"
CASH_MODE = "美元金額"
PERCENT_MODE = "帳戶權益 %"
PERCENT_CHOICES = ("25", "30", "50", "75", "100")
ACTION_LABELS = {"買進": "BUY", "賣出": "SELL", "回補空單": "COVER"}
STORED_ACTION_LABELS = {
    "BUY": "買進", "SELL": "賣出", "SELL_SHORT": "放空", "COVER": "回補空單",
}
FORM_ACTION_LABELS = {**STORED_ACTION_LABELS, "SELL_SHORT": "賣出"}
SESSION_CHOICES = {
    "一般時段": "REGULAR", "盤前": "PRE_MARKET", "盤後": "AFTER_HOURS",
    "隔夜（IBKR Overnight）": "OVERNIGHT",
}
FORM_SESSION_LABELS = {code: label for label, code in SESSION_CHOICES.items()}
STORED_SESSION_LABELS = {
    "REGULAR": "一般時段 RTH / DAY",
    "PRE_MARKET": "盤前 outsideRTH / DAY",
    "AFTER_HOURS": "盤後 outsideRTH / DAY",
    "OVERNIGHT": "隔夜 OVERNIGHT",
}

# Rows whose IB state is unsettled: no cancel or modify until it resolves.
_UNSETTLED_STATUSES = UNCONFIRMED_STATUSES | {"CANCEL_PENDING"}
_OK_SUBMIT_STATUSES = frozenset({"SENT", "ACCEPTED", "PARTIAL", "FILLED"})


@dataclass(frozen=True)
class ManualOrderForm:
    """Raw field values exactly as the window shows them."""
    account: str
    symbol: str
    input_mode: str
    quantity: str
    amount: str
    percent: str
    order_type: str
    price: str
    session: str
    action: str
    allow_short: bool


@dataclass(frozen=True)
class ManualOrderRequest:
    account_name: str
    symbol: str
    quantity: int | None
    limit_price: float | None
    action: str = "BUY"
    allow_short: bool = False
    amount_usd: Decimal | None = None
    order_type: str = "LMT"
    session: str = "REGULAR"
    equity_pct: Decimal | None = None


@dataclass(frozen=True)
class Replacement:
    """What to show once IB settles the order being modified."""
    message: str
    form: ManualOrderForm | None  # None = nothing to re-enter


# ── Form → request ─────────────────────────────────────────────────────

def parse_request(form: ManualOrderForm) -> ManualOrderRequest:
    """Validate the form; ValueError carries the message shown to the trader."""
    account = form.account.strip()
    if not account:
        raise ValueError("請先選擇這筆委託要送往的 Account。")
    quantity = None
    amount = None
    percent = None
    if form.input_mode == SHARES_MODE:
        raw_quantity = form.quantity.strip()
        if not raw_quantity.isdecimal():
            raise ValueError("整股股數必須是正整數。")
        quantity = int(raw_quantity)
        if quantity <= 0:
            raise ValueError("整股股數必須大於零。")
    elif form.input_mode == CASH_MODE:
        try:
            amount = Decimal(form.amount.strip())
        except InvalidOperation as exc:
            raise ValueError("請輸入有效的美元金額。") from exc
        if not amount.is_finite() or amount <= 0:
            raise ValueError("美元金額必須大於零。")
    elif form.input_mode == PERCENT_MODE:
        from manual_orders import equity_percent

        percent = equity_percent(form.percent)
    else:
        raise ValueError("請選擇整股股數、美元金額或帳戶權益 %。")
    order_type = form.order_type
    if order_type == "LMT":
        try:
            price = float(form.price.strip())
        except ValueError as exc:
            raise ValueError("請輸入自填限價。") from exc
        if not math.isfinite(price) or price <= 0:
            raise ValueError("限價必須大於零。")
    elif order_type == "MKT":
        price = None
    else:
        raise ValueError("請選擇有效委託類型。")
    session = SESSION_CHOICES.get(form.session)
    if session is None:
        raise ValueError("請選擇交易時段。")
    if session != "REGULAR" and order_type != "LMT":
        # Never switch the trader's MKT to LMT or regular hours on their behalf.
        raise ValueError("盤前／盤後／隔夜只接受 LMT 限價單；請改選 LMT，或改用一般時段。")
    return ManualOrderRequest(
        account_name=account,
        symbol=form.symbol.strip(),
        quantity=quantity,
        limit_price=price,
        action=ACTION_LABELS[form.action],
        allow_short=bool(form.allow_short),
        amount_usd=amount,
        order_type=order_type,
        session=session,
        equity_pct=percent,
    )


def requested_quantity(request: ManualOrderRequest) -> int:
    from manual_orders import whole_share_quantity

    if request.equity_pct is not None:
        raise ValueError("修改委託時請改用整股股數或美元金額。")
    if request.order_type == "MKT":
        if request.quantity is None:
            raise ValueError("修改時請輸入明確股數。")
        return request.quantity
    return whole_share_quantity(
        shares=request.quantity, amount_usd=request.amount_usd,
        limit_price=request.limit_price,
    )


def preview_kwargs(request: ManualOrderRequest) -> dict:
    """Keyword options for ManualOrderService.preview; defaults are left out."""
    kwargs = dict(
        action=request.action, allow_short=request.allow_short,
        amount_usd=request.amount_usd,
    )
    if request.order_type == "MKT":
        kwargs["order_type"] = "MKT"
    if request.session != "REGULAR":
        kwargs["session"] = request.session
    if request.equity_pct is not None:
        kwargs["equity_pct"] = request.equity_pct
    return kwargs


# ── Symbol suggestions ─────────────────────────────────────────────────

def symbol_hint(symbols, text: str) -> tuple[str, str]:
    """(hint, color key) under the symbol field.

    symbols: None = directory still loading; False = unavailable; otherwise a
    us_symbols.SymbolDirectory.
    """
    text = text.strip()
    if symbols is None:
        return ("美股代號清單載入中…" if text else ""), "text_dim"
    if symbols is False:
        return ("無法取得美股代號清單，請自行確認代號" if text else ""), "text_dim"
    if not text:
        return "", "text_dim"
    name = symbols.name_for(text)
    if name:
        return (name if len(name) <= 60 else name[:59] + "…"), "text"
    return "清單內找不到此代號（仍可預覽，以 IB 合約檢查為準）", "yellow"


def suggestion_lines(matches, typed: str) -> list[str]:
    """Dropdown lines; empty when there is nothing worth suggesting."""
    # 只剩一個且已完全相符時不必再跳清單
    if not matches or (len(matches) == 1
                       and matches[0][0] == typed.strip().upper()):
        return []
    return [f"{symbol:<6}  {name[:60]}" for symbol, name in matches]


# ── Order status rows ──────────────────────────────────────────────────

def open_orders(rows) -> list[dict]:
    return [row for row in rows if row.get("status") not in TERMINAL_STATUSES]


def order_row_values(row: dict) -> tuple:
    """Cells for the manual order status table, in column order."""
    filled = int(row.get("filled_quantity") or 0)
    avg_price = row.get("avg_fill_price")
    fill_text = f"{filled}/{row['quantity']}"
    if avg_price:
        fill_text += " @ $" + f"{float(avg_price):.2f}"
    price_condition = (
        "MKT" if row.get("order_type") == "MKT"
        else f"LMT ${float(row['limit_price']):.2f}"
    )
    return (
        row["account_name"], row["symbol"],
        STORED_ACTION_LABELS.get(row["action"], row["action"]),
        row["quantity"], price_condition,
        STORED_SESSION_LABELS.get(row.get("session") or "REGULAR", row.get("session")),
        row.get("status_label", row["status"]), fill_text,
    )


def can_cancel(row: dict | None) -> bool:
    return row is not None and row.get("status") not in _UNSETTLED_STATUSES


def can_modify(row: dict | None) -> bool:
    return can_cancel(row) and row.get("order_type", "LMT") == "LMT"


def cancel_pending_row(row: dict) -> dict:
    return dict(
        row, status="CANCEL_PENDING", status_label="取消確認中",
        status_message="已記錄取消要求，等待 IB 確認",
    )


def replacement_waiting(row: dict) -> bool:
    # The original order may still fill; a replacement waits while it is active.
    return row.get("status") in ACTIVE_STATUSES


# ── Submit result ──────────────────────────────────────────────────────

def needs_attention(result: dict) -> bool:
    return result["status"] not in _OK_SUBMIT_STATUSES


def submit_status_text(result: dict) -> str:
    # The message carries IB's reason for rejections and forced cancels.
    return (f"{result['account_name']} {result['symbol']}："
            f"{result.get('status_message') or result.get('status_label', result['status'])}")


def submit_log_line(result: dict) -> str:
    price_condition = (
        "MKT" if result.get("order_type") == "MKT"
        else f"LMT {result['limit_price']} USD"
    )
    session = result.get("session") or "REGULAR"
    return (
        f"[>] Manual order [{result['account_name']}] "
        f"{result['action']} {result['quantity']}sh {result['symbol']} "
        f"{price_condition} {session} — {result['status']}"
        + (f" ({result.get('status_message', '')})" if needs_attention(result) else "")
    )


# ── Cancel / modify ────────────────────────────────────────────────────

def cancel_prompt(row: dict) -> str:
    return (f"向 IB 要求取消 {row['account_name']} 的 {row['symbol']} "
            f"{row['quantity']} 股委託？\n\n取消完成前仍可能成交。")


def form_matches_row(form: ManualOrderForm, row: dict) -> bool:
    """Whether the form already holds the selected order's identity fields."""
    return (form.account.strip() == row["account_name"]
            and form.symbol.strip().upper() == row["symbol"]
            and form.action == FORM_ACTION_LABELS[row["action"]]
            and bool(form.allow_short) == (row["action"] == "SELL_SHORT")
            and SESSION_CHOICES.get(form.session) == (row.get("session") or "REGULAR"))


def form_from_row(row: dict) -> ManualOrderForm:
    """The selected order loaded as whole-share LMT fields, ready to edit."""
    return ManualOrderForm(
        account=row["account_name"], symbol=row["symbol"],
        input_mode=SHARES_MODE, quantity=str(row["quantity"]), amount="", percent="",
        order_type="LMT", price=str(row["limit_price"]),
        session=FORM_SESSION_LABELS[row.get("session") or "REGULAR"],
        action=FORM_ACTION_LABELS[row["action"]],
        allow_short=row["action"] == "SELL_SHORT",
    )


def modify_quantity(request: ManualOrderRequest) -> int:
    if request.order_type != "LMT":
        raise ValueError("修改限價單時請選擇 LMT。")
    return requested_quantity(request)


def check_modify(row: dict, request: ManualOrderRequest, quantity: int) -> None:
    """A modification may change only price or shares, and must change one."""
    if (request.account_name != row["account_name"]
            or request.symbol != row["symbol"]
            or request.action != ("SELL" if row["action"] == "SELL_SHORT" else row["action"])
            or request.allow_short != (row["action"] == "SELL_SHORT")
            or request.session != (row.get("session") or "REGULAR")):
        raise ValueError("修改只允許變更原委託的價格或股數。")
    if (quantity == int(row["quantity"])
            and request.limit_price == float(row["limit_price"])):
        raise ValueError("請先變更價格或股數。")


def modify_prompt(row: dict, request: ManualOrderRequest, quantity: int) -> str:
    cash_target = (
        f"預估交易目標 ${request.amount_usd:f} → "
        if request.amount_usd is not None else ""
    )
    return (
        f"先取消目前的 {row['quantity']} 股 {row['symbol']} 委託，"
        f"等 IB 確認後再預覽新條件：\n"
        f"{cash_target}{quantity} 股，LMT ${request.limit_price:.2f}"
        f"（{FORM_SESSION_LABELS[request.session]}）\n\n"
        "原委託取消前仍可能成交；不會自動送出新委託。"
    )


def replacement_after_cancel(row: dict, request: ManualOrderRequest) -> Replacement:
    """Next step once IB has settled the order the trader asked to modify."""
    if row.get("status") != "CANCELLED":
        return Replacement(
            f"IB 已回報原委託：{row.get('status_label', row.get('status'))}；"
            "不會送出修改後委託。", None)
    filled = int(row.get("filled_quantity") or 0)
    remaining = max(0, requested_quantity(request) - filled)
    if remaining <= 0:
        return Replacement("取消確認前的成交已達修改後股數；沒有可重下的數量。", None)
    # After any partial fill, prefill the remaining shares explicitly so a
    # cash target cannot recalculate to the original total on re-preview.
    form = ManualOrderForm(
        account=request.account_name, symbol=request.symbol,
        input_mode=SHARES_MODE, quantity=str(remaining), amount="", percent="",
        order_type=request.order_type, price=str(request.limit_price),
        session=FORM_SESSION_LABELS[request.session],
        action=FORM_ACTION_LABELS[request.action], allow_short=request.allow_short,
    )
    return Replacement(
        f"原委託已取消，期間成交 {filled} 股。"
        f"新委託數量已調整為 {remaining} 股；按「預覽委託」後再確認送出。", form)
