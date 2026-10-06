"""Table tests for the manual-order status rules (#210).

`status_from_trade` and `fill_totals` are pure: they read plain attributes off
IB-shaped objects, so these cases build them from SimpleNamespace — no FakeIB.
The end-to-end scenarios stay in test_manual_orders.
"""

from __future__ import annotations

import math
from pathlib import Path
import re
from types import SimpleNamespace as NS

import pytest

import broker
import manual_order_status as mos
import manual_orders
import strategy_takeover


def _trade(status, *, filled=0, quantity=10, avg=0.0, log=()):
    return NS(
        orderStatus=NS(status=status, filled=filled, avgFillPrice=avg),
        order=NS(totalQuantity=quantity),
        log=[NS(errorCode=code, message=message) for code, message in log],
    )


def test_every_status_is_either_active_or_terminal():
    active = set(mos.ACTIVE_STATUSES)
    terminal = set(mos.TERMINAL_STATUSES)
    assert not active & terminal
    assert active | terminal == set(mos.STATUS_LABELS)
    assert set(mos.UNCONFIRMED_STATUSES) == {
        "CANCEL_UNKNOWN", "IB_UNKNOWN", "SUBMISSION_UNKNOWN"}


# IB status × filled × completedStatus × trade log → (our status, filled)
STATUS_TABLE = [
    # IB status,        filled, completedStatus, log,                          ours
    ("ApiPending",       0, "", (),                                             "SENT"),
    ("PendingSubmit",    0, "", (),                                             "SENT"),
    ("PreSubmitted",     0, "", (),                                             "ACCEPTED"),
    ("Submitted",        0, "", (),                                             "ACCEPTED"),
    ("Submitted",        3, "", (),                                             "PARTIAL"),
    ("Submitted",       10, "", (),                                             "FILLED"),
    ("PartiallyFilled",  3, "", (),                                             "PARTIAL"),
    ("Filled",          10, "", (),                                             "FILLED"),
    ("PendingCancel",    0, "", (),                                             "CANCEL_PENDING"),
    ("PendingCancel",    3, "", (),                                             "CANCEL_PENDING"),
    # ApiCancelled: the cancel was requested before IB acknowledged the order.
    ("ApiCancelled",     0, "", (),                                             "CANCEL_PENDING"),
    ("Cancelled",        0, "", (),                                             "CANCELLED"),
    ("Cancelled",        3, "", (),                                             "CANCELLED"),
    ("Cancelled",        0, "Cancelled", (),                                    "CANCELLED"),
    # Only completedStatus tells an expiry from a trader cancel.
    ("Cancelled",        0, "Expired (5)", (),                                  "EXPIRED"),
    ("Cancelled",        3, "Expired (5)", (),                                  "EXPIRED"),
    ("Expired",          0, "", (),                                             "EXPIRED"),
    ("Inactive",         0, "", (),                                             "REJECTED"),
    # A cancel that fully filled first is a fill.
    ("Cancelled",       10, "", (),                                             "FILLED"),
    # 10329: the Gateway blocked direct routing; ib_insync reports Cancelled.
    ("Cancelled",        0, "", ((10329, "Order rejected"),),                   "REJECTED"),
    # 2109 (outsideRth ignored) is a warning: status mapping ignores it and the
    # Service cancels the order instead.
    ("Submitted",        0, "", ((mos.IB_OUTSIDE_RTH_IGNORED, "ignored"),),     "ACCEPTED"),
    ("",                 0, "", (),                                             None),
    ("SomethingNew",     0, "", (),                                             None),
]


@pytest.mark.parametrize("ib_status, filled, completed, log, expected", STATUS_TABLE)
def test_status_from_trade_maps_ib_reports(ib_status, filled, completed, log, expected):
    status, _message, got_filled, _avg = mos.status_from_trade(
        _trade(ib_status, filled=filled, log=log), completed)

    assert status == expected
    assert got_filled == filled


def test_status_message_is_the_status_label():
    status, message, _, _ = mos.status_from_trade(_trade("Submitted"))

    assert message == mos.STATUS_LABELS[status]


def test_unknown_ib_status_message_shows_the_raw_status():
    assert mos.status_from_trade(_trade("SomethingNew"))[1] == "SomethingNew"
    assert mos.status_from_trade(_trade(""))[1] == "等待 IB 狀態"


def test_direct_routing_block_tells_the_trader_which_gateway_setting():
    _, message, _, _ = mos.status_from_trade(
        _trade("Cancelled", log=[(10329, "Order rejected")]))

    assert "股票API委託單跳過重新傳遞委託單警告" in message


def test_rejection_appends_ib_last_message():
    _, message, _, _ = mos.status_from_trade(
        _trade("Inactive", log=[(201, "first"), (201, "  "), (202, "Order rejected - margin")]))

    assert message == f"{mos.STATUS_LABELS['REJECTED']}：Order rejected - margin"


@pytest.mark.parametrize("avg, expected", [
    (12.5, 12.5), (0, None), (-1, None), (math.nan, None), ("bad", None),
])
def test_status_from_trade_average_fill_price(avg, expected):
    assert mos.status_from_trade(_trade("Filled", filled=10, avg=avg))[3] == expected


def test_status_from_trade_tolerates_missing_fields():
    assert mos.status_from_trade(NS()) == (None, "等待 IB 狀態", 0, None)


# ── fill_totals ──────────────────────────────────────────────────────────

CTX = NS(account="U001", client_id=26)
ROW = {"order_ref": "QUANTCORE_MANUAL:abc", "perm_id": 555, "broker_order_id": 7,
       "contract_id": 1001}


def _fill(*, shares=1, price=10.0, account="U001", ref="", perm=0, order_id=0,
          client_id=0, con_id=1001, exec_id=None):
    return NS(
        execution=NS(acctNumber=account, orderRef=ref, permId=perm, orderId=order_id,
                     clientId=client_id, execId=exec_id, shares=shares, price=price),
        contract=NS(conId=con_id),
    )


def _totals(fills, row=ROW):
    return mos.fill_totals(fills, row=row, order=NS(), trade=NS(), ctx=CTX)


FILL_TABLE = [
    # (fill kwargs, counted?)
    (dict(ref="QUANTCORE_MANUAL:abc"), True),                      # exact orderRef
    (dict(perm=555), True),                                         # stored permId
    (dict(order_id=7, client_id=26), True),                        # orderId + clientId + conId
    (dict(order_id=7, client_id=99), False),                       # another connection's id 7
    (dict(order_id=7, client_id=26, con_id=2002), False),          # same id, other contract
    (dict(ref="QUANTCORE_MANUAL:abc", con_id=2002), False),        # ref matches, contract not
    (dict(ref="QUANTCORE_MANUAL:abc", account="U002"), False),     # other Account
    (dict(ref="QUANTCORE_MANUAL:other"), False),                   # other order
    (dict(ref="QUANTCORE_MANUAL:abc", shares=0), False),           # no shares
    (dict(ref="QUANTCORE_MANUAL:abc", price=math.nan), False),     # bad price
]


@pytest.mark.parametrize("fill, counted", FILL_TABLE)
def test_fill_totals_counts_only_fills_tied_to_this_order(fill, counted):
    assert _totals([_fill(**{"shares": 4, **fill})]) == (
        (4, 10.0) if counted else (0, None))


def test_fill_totals_average_is_share_weighted():
    fills = [_fill(perm=555, shares=3, price=10.0), _fill(perm=555, shares=1, price=14.0)]

    assert _totals(fills) == (4, 11.0)


def test_fill_totals_counts_a_repeated_execution_once():
    fills = [_fill(perm=555, shares=3, exec_id="e1"), _fill(perm=555, shares=3, exec_id="e1")]

    assert _totals(fills) == (3, 10.0)


def test_fill_totals_falls_back_to_the_ib_order_ids():
    order = NS(permId=555, orderId=7)
    trade = NS(contract=NS(conId=1001))
    row = {"order_ref": "QUANTCORE_MANUAL:abc"}

    assert mos.fill_totals([_fill(perm=555)], row=row, order=order, trade=trade,
                           ctx=CTX) == (1, 10.0)


def test_fill_totals_without_fills():
    assert _totals([]) == (0, None)
    assert _totals([NS(execution=None)]) == (0, None)


# ── one home per rule ────────────────────────────────────────────────────

def test_ib_done_statuses_live_in_the_broker():
    assert broker.IB_DONE_STATUSES == frozenset(
        {"Filled", "Cancelled", "ApiCancelled", "Inactive"})
    assert manual_orders.IB_DONE_STATUSES is broker.IB_DONE_STATUSES
    assert strategy_takeover.IB_DONE_STATUSES is broker.IB_DONE_STATUSES


def test_manual_orders_uses_the_public_status_rules():
    assert manual_orders.ACTIVE_STATUSES is mos.ACTIVE_STATUSES
    assert manual_orders.TERMINAL_STATUSES is mos.TERMINAL_STATUSES
    assert manual_orders.UNCONFIRMED_STATUSES is mos.UNCONFIRMED_STATUSES


def test_no_production_module_reaches_into_manual_orders_private_names():
    automation = Path(manual_orders.__file__).parent
    pattern = re.compile(r"(?:\bmo|\bmanual_orders)\._[A-Za-z]")
    offenders = [
        path.name for path in automation.rglob("*.py")
        if path.name != "manual_orders.py" and "tests" not in path.parts
        and any(pattern.search(line) for line in path.read_text(encoding="utf-8").splitlines()
                if not line.lstrip().startswith("#"))
    ]
    assert offenders == []
