"""Manual-order status rules: what an IB report means for a manual order (#210).

Pure functions and constants — no IB connection, no SQLite. `manual_orders`
(the Service) applies them; `paper_verify_manual_orders` reads the same
outsideRth rules, so the paper check judges orders the way the Service does.

- Status vocabulary: every manual-order status is either active or terminal.
- `status_from_trade`: IB orderStatus (plus completedStatus and the trade log)
  → our status, user-visible message, filled shares and average fill price.
- `fill_totals`: shares and average price of only the executions that can be
  tied to this manual order (Σ price×shares ÷ Σ shares).
- outsideRth confirmation: IB warning 2109 and how long the placing
  connection keeps listening for it.

IB's own vocabulary that every order flow shares (`IB_DONE_STATUSES`) lives
in the Broker.
"""

from __future__ import annotations

import math


# Manual-order status vocabulary, defined only here (manual_orders,
# manual_order_form and manual_attribution use it).  Every status is either
# active or terminal.
# Active: the order may still fill; at most one per Account/symbol (the
# migration-1 unique index repeats this list — a test keeps them equal).
ACTIVE_STATUSES = (
    "SENDING", "SENT", "ACCEPTED", "PARTIAL", "CANCEL_PENDING",
    "CANCEL_UNKNOWN", "IB_UNKNOWN", "SUBMISSION_UNKNOWN",
)
TERMINAL_STATUSES = frozenset({"FILLED", "REJECTED", "CANCELLED", "EXPIRED"})
# Active statuses whose IB outcome is unconfirmed: each episode raises an alert.
UNCONFIRMED_STATUSES = frozenset({"CANCEL_UNKNOWN", "IB_UNKNOWN", "SUBMISSION_UNKNOWN"})
STATUS_LABELS = {
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

# IB order statuses before IB has answered placeOrder.
IB_AWAITING_ACK = frozenset({"", "ApiPending", "PendingSubmit"})
# IB order statuses of an order that can still execute.
IB_WORKING_STATUSES = frozenset({"ApiPending", "PendingSubmit", "PreSubmitted", "Submitted"})

# TWS warning: "Attribute 'Outside Regular Trading Hours' is ignored based on
# the order type and destination. PlaceOrder is now being processed."
IB_OUTSIDE_RTH_IGNORED = 2109
# TWS sends 2109 only to the connection that placed the order, so that
# connection keeps listening this long after IB's first status. Only a quiet
# window marks outsideRth as confirmed; anything else is cancelled.
OUTSIDE_RTH_CONFIRM_SECONDS = 5.0

# Paper, 2026-09-29: an OVERNIGHT-routed order is rejected (10329, then 201)
# unless the Gateway bypasses the redirect precaution for stock API orders.
_IB_DIRECT_ROUTING_BLOCKED = 10329
_DIRECT_ROUTING_BLOCKED_MESSAGE = (
    "遭 IB 拒單：Gateway 的 API 預防設定擋下直接路由；請在該 Gateway 的"
    "設定 → API → 預防，勾選「股票API委託單跳過重新傳遞委託單警告」"
)


def fill_totals(fills, *, row: dict, order, trade, ctx) -> tuple[int, float | None]:
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


def status_from_trade(trade, completed_status: str = "",
                      ) -> tuple[str | None, str, int, float | None]:
    """(status, message, filled, avg fill price) for an IB trade; status None = unknown."""
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
    if mapped == "CANCELLED" and completed_status.startswith("Expired"):
        # IB ends an unfilled DAY or Overnight order with status Cancelled;
        # only completedStatus ("Expired (5)") says it expired rather than
        # being cancelled by the trader (paper, 2026-09-28/29).
        mapped = "EXPIRED"
    if filled > 0 and quantity > filled and mapped not in {
            "CANCEL_PENDING", "CANCEL_UNKNOWN", "CANCELLED", "EXPIRED", "REJECTED"}:
        mapped = "PARTIAL"
    elif quantity > 0 and filled >= quantity:
        mapped = "FILLED"
    message = STATUS_LABELS.get(mapped, raw_status or "等待 IB 狀態")
    entries = getattr(trade, "log", ()) or ()
    if mapped == "CANCELLED" and any(
            getattr(entry, "errorCode", 0) == _IB_DIRECT_ROUTING_BLOCKED for entry in entries):
        # ib_insync marks an order IB refused as Cancelled; this refusal
        # comes from a Gateway setting the trader has to change.
        return "REJECTED", _DIRECT_ROUTING_BLOCKED_MESSAGE, filled, avg_fill_price
    if mapped == "REJECTED":
        details = [str(getattr(entry, "message", "") or "").strip() for entry in entries]
        details = [item for item in details if item]
        if details:
            message = f"{message}：{details[-1]}"
    return mapped, message, filled, avg_fill_price
