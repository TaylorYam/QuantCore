"""manual_order_form：手動單欄位解析、委託列與提示文字（Tk／Qt 共用，不依賴 GUI）。"""

import re
from decimal import Decimal

import pytest

import manual_order_form as mof
from manual_order_form import ManualOrderForm, ManualOrderRequest


def _form(**changes):
    base = dict(
        account="Rosa", symbol=" zzzz ", input_mode=mof.SHARES_MODE,
        quantity="3", amount="", percent="", order_type="LMT", price="12.5",
        session="一般時段", action="買進", allow_short=False,
    )
    return ManualOrderForm(**{**base, **changes})


def _row(**changes):
    row = {
        "order_ref": "R1", "account_name": "Rosa", "symbol": "ZZZZ",
        "action": "BUY", "quantity": 3, "limit_price": 12.5,
        "status": "ACCEPTED", "status_label": "IB accepted",
        "filled_quantity": 0, "avg_fill_price": None,
        "order_type": "LMT", "session": "REGULAR",
    }
    row.update(changes)
    return row


# ── parse_request ──────────────────────────────────────────────────────

def test_parse_whole_shares_limit_order():
    assert mof.parse_request(_form(quantity=" 7 ")) == ManualOrderRequest(
        account_name="Rosa", symbol="zzzz", quantity=7, limit_price=12.5,
        action="BUY", allow_short=False, amount_usd=None, order_type="LMT",
        session="REGULAR", equity_pct=None,
    )


def test_parse_cash_and_equity_percent_modes():
    cash = mof.parse_request(_form(input_mode=mof.CASH_MODE, amount="100.00"))
    assert (cash.quantity, cash.amount_usd) == (None, Decimal("100.00"))
    pct = mof.parse_request(_form(input_mode=mof.PERCENT_MODE, percent="40%"))
    assert (pct.quantity, pct.amount_usd, pct.equity_pct) == (None, None, Decimal("40"))


def test_parse_market_extended_session_and_direction():
    mkt = mof.parse_request(_form(order_type="MKT", price=""))
    assert (mkt.order_type, mkt.limit_price) == ("MKT", None)
    assert mof.parse_request(_form(session="盤前")).session == "PRE_MARKET"
    assert mof.parse_request(_form(session="隔夜（IBKR Overnight）")).session == "OVERNIGHT"
    short = mof.parse_request(_form(action="賣出", allow_short=True))
    assert (short.action, short.allow_short) == ("SELL", True)
    assert mof.parse_request(_form(action="回補空單")).action == "COVER"


@pytest.mark.parametrize("changes, message", [
    ({"account": "  "}, "請先選擇這筆委託要送往的 Account。"),
    ({"quantity": "0"}, "整股股數必須大於零。"),
    ({"quantity": "1.5"}, "整股股數必須是正整數。"),
    ({"quantity": ""}, "整股股數必須是正整數。"),
    ({"input_mode": mof.CASH_MODE, "amount": "abc"}, "請輸入有效的美元金額。"),
    ({"input_mode": mof.CASH_MODE, "amount": "NaN"}, "美元金額必須大於零。"),
    ({"input_mode": mof.CASH_MODE, "amount": "-5"}, "美元金額必須大於零。"),
    ({"input_mode": mof.PERCENT_MODE, "percent": "150"}, "權益百分比"),
    ({"input_mode": "bogus"}, "請選擇整股股數、美元金額或帳戶權益 %。"),
    ({"price": "abc"}, "請輸入自填限價。"),
    ({"price": "nan"}, "限價必須大於零。"),
    ({"price": "0"}, "限價必須大於零。"),
    ({"order_type": "STP"}, "請選擇有效委託類型。"),
    ({"session": "bogus"}, "請選擇交易時段。"),
    ({"session": "盤後", "order_type": "MKT", "price": ""}, "只接受 LMT"),
])
def test_parse_rejects_bad_input_with_the_trader_facing_message(changes, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        mof.parse_request(_form(**changes))


def test_requested_quantity():
    assert mof.requested_quantity(mof.parse_request(_form(quantity="4"))) == 4
    cash = mof.parse_request(_form(input_mode=mof.CASH_MODE, amount="100", price="30"))
    assert mof.requested_quantity(cash) == 3
    with pytest.raises(ValueError, match="修改委託時請改用整股股數或美元金額。"):
        mof.requested_quantity(mof.parse_request(
            _form(input_mode=mof.PERCENT_MODE, percent="30")))
    with pytest.raises(ValueError, match="修改時請輸入明確股數。"):
        mof.requested_quantity(mof.parse_request(_form(
            input_mode=mof.CASH_MODE, amount="100", order_type="MKT", price="")))


def test_preview_kwargs_only_carries_non_default_options():
    assert mof.preview_kwargs(mof.parse_request(_form())) == {
        "action": "BUY", "allow_short": False, "amount_usd": None,
    }
    request = mof.parse_request(_form(session="盤後",
                                      input_mode=mof.PERCENT_MODE, percent="30"))
    assert mof.preview_kwargs(request) == {
        "action": "BUY", "allow_short": False, "amount_usd": None,
        "session": "AFTER_HOURS", "equity_pct": Decimal("30"),
    }
    mkt = mof.parse_request(_form(order_type="MKT", price=""))
    assert mof.preview_kwargs(mkt)["order_type"] == "MKT"


# ── 代號提示 ───────────────────────────────────────────────────────────

class _Directory:
    def name_for(self, symbol):
        return {"SKHY": "SK hynix", "LONG": "x" * 80}.get(symbol.strip().upper())


def test_symbol_hint_by_directory_state():
    assert mof.symbol_hint(None, "ab") == ("美股代號清單載入中…", "text_dim")
    assert mof.symbol_hint(None, " ") == ("", "text_dim")
    assert mof.symbol_hint(False, "ab") == ("無法取得美股代號清單，請自行確認代號", "text_dim")
    assert mof.symbol_hint(_Directory(), "") == ("", "text_dim")
    assert mof.symbol_hint(_Directory(), "skhy") == ("SK hynix", "text")
    assert mof.symbol_hint(_Directory(), "long") == ("x" * 59 + "…", "text")
    assert mof.symbol_hint(_Directory(), "zzzz") == (
        "清單內找不到此代號（仍可預覽，以 IB 合約檢查為準）", "yellow")


def test_suggestions_hidden_when_only_exact_match():
    assert mof.suggestion_lines([("SKHY", "SK hynix")], " skhy ") == []
    assert mof.suggestion_lines([], "sk") == []
    assert mof.suggestion_lines([("SKHU", "U" * 70), ("SKHY", "SK hynix")], "skh") == [
        "SKHU    " + "U" * 60, "SKHY    SK hynix",
    ]


# ── 委託狀態列 ─────────────────────────────────────────────────────────

def test_order_row_values():
    assert mof.order_row_values(_row()) == (
        "Rosa", "ZZZZ", "買進", 3, "LMT $12.50", "一般時段 RTH / DAY", "IB accepted", "0/3",
    )
    partial = _row(action="SELL_SHORT", quantity=10, limit_price=184.4,
                   filled_quantity=4, avg_fill_price=184.35, session="PRE_MARKET",
                   status_label="部分成交")
    assert mof.order_row_values(partial) == (
        "Rosa", "ZZZZ", "放空", 10, "LMT $184.40", "盤前 outsideRTH / DAY", "部分成交",
        "4/10 @ $184.35",
    )
    mkt = _row(order_type="MKT", limit_price=None, session=None, action="WEIRD",
               filled_quantity=None)
    del mkt["status_label"]
    assert mof.order_row_values(mkt) == (
        "Rosa", "ZZZZ", "WEIRD", 3, "MKT", "一般時段 RTH / DAY", "ACCEPTED", "0/3",
    )
    assert mof.order_row_values(_row(session="MOON"))[5] == "MOON"


def test_open_orders_drop_terminal_history():
    rows = [_row(order_ref=s, status=s) for s in (
        "ACCEPTED", "FILLED", "REJECTED", "CANCELLED", "EXPIRED", "PARTIAL")]
    assert [r["order_ref"] for r in mof.open_orders(rows)] == ["ACCEPTED", "PARTIAL"]


def test_cancel_and_modify_availability():
    assert mof.can_cancel(_row()) and mof.can_modify(_row())
    assert mof.can_cancel(_row(order_type="MKT"))
    assert not mof.can_modify(_row(order_type="MKT"))
    for status in ("CANCEL_PENDING", "CANCEL_UNKNOWN", "IB_UNKNOWN", "SUBMISSION_UNKNOWN"):
        assert not mof.can_cancel(_row(status=status))
        assert not mof.can_modify(_row(status=status))
    assert not mof.can_cancel(None) and not mof.can_modify(None)


def test_cancel_pending_row_keeps_the_rest():
    pending = mof.cancel_pending_row(_row())
    assert (pending["status"], pending["status_label"], pending["status_message"]) == (
        "CANCEL_PENDING", "取消確認中", "已記錄取消要求，等待 IB 確認")
    assert pending["symbol"] == "ZZZZ"


def test_replacement_still_waiting():
    assert mof.replacement_waiting(_row(status="CANCEL_PENDING"))
    assert not mof.replacement_waiting(_row(status="CANCELLED"))


# ── 送單結果 ───────────────────────────────────────────────────────────

def test_submit_status_and_log_line():
    ok = _row()
    assert not mof.needs_attention(ok)
    assert mof.submit_status_text(ok) == "Rosa ZZZZ：IB accepted"
    assert mof.submit_log_line(ok) == (
        "[>] Manual order [Rosa] BUY 3sh ZZZZ LMT 12.5 USD REGULAR — ACCEPTED")
    rejected = _row(status="REJECTED", status_label="IB 拒絕", status_message="Margin")
    assert mof.needs_attention(rejected)
    assert mof.submit_status_text(rejected) == "Rosa ZZZZ：Margin"
    assert mof.submit_log_line(rejected).endswith("— REJECTED (Margin)")
    cancelled = _row(status="CANCELLED", status_message="")
    assert mof.submit_log_line(cancelled).endswith("— CANCELLED ()")
    mkt = _row(status="PARTIAL", order_type="MKT", limit_price=None, session=None)
    assert mof.submit_log_line(mkt) == (
        "[>] Manual order [Rosa] BUY 3sh ZZZZ MKT REGULAR — PARTIAL")
    bare = _row(status="SENT")
    del bare["status_label"]
    assert mof.submit_status_text(bare) == "Rosa ZZZZ：SENT"


# ── 取消／修改 ─────────────────────────────────────────────────────────

def test_cancel_prompt():
    assert mof.cancel_prompt(_row()) == (
        "向 IB 要求取消 Rosa 的 ZZZZ 3 股委託？\n\n取消完成前仍可能成交。")


def test_form_matches_row_and_form_from_row():
    row = _row(action="SELL_SHORT", quantity=10, session="PRE_MARKET")
    loaded = mof.form_from_row(row)
    assert loaded == ManualOrderForm(
        account="Rosa", symbol="ZZZZ", input_mode=mof.SHARES_MODE, quantity="10",
        amount="", percent="", order_type="LMT", price="12.5", session="盤前",
        action="賣出", allow_short=True,
    )
    assert mof.form_matches_row(loaded, row)
    assert mof.form_matches_row(_form(symbol="zzzz ", quantity="99"), _row())
    assert not mof.form_matches_row(_form(account="Taylor"), _row())
    assert not mof.form_matches_row(_form(session="盤後"), _row())
    assert not mof.form_matches_row(_form(allow_short=True), _row())


def test_modify_checks():
    row = _row(quantity=10)
    same = mof.parse_request(_form(symbol="ZZZZ", quantity="10"))
    with pytest.raises(ValueError, match="請先變更價格或股數。"):
        mof.check_modify(row, same, 10)
    moved = mof.parse_request(_form(symbol="ZZZZ", quantity="10", session="盤後"))
    with pytest.raises(ValueError, match="修改只允許變更原委託的價格或股數。"):
        mof.check_modify(row, moved, 10)
    mkt = mof.parse_request(_form(symbol="ZZZZ", order_type="MKT", price=""))
    with pytest.raises(ValueError, match="修改限價單時請選擇 LMT。"):
        mof.modify_quantity(mkt)
    cash = mof.parse_request(_form(input_mode=mof.CASH_MODE, amount="100", price="13"))
    assert mof.modify_quantity(cash) == 7
    mof.check_modify(row, mof.parse_request(_form(symbol="ZZZZ", quantity="4")), 4)
    short_row = _row(action="SELL_SHORT")
    mof.check_modify(short_row, mof.parse_request(
        _form(symbol="ZZZZ", action="賣出", allow_short=True, price="13")), 3)


def test_modify_prompt():
    row = _row(quantity=10)
    cash = mof.parse_request(_form(symbol="ZZZZ", input_mode=mof.CASH_MODE,
                                   amount="100", price="13", session="盤前"))
    assert mof.modify_prompt(row, cash, 7) == (
        "先取消目前的 10 股 ZZZZ 委託，等 IB 確認後再預覽新條件：\n"
        "預估交易目標 $100 → 7 股，LMT $13.00（盤前）\n\n"
        "原委託取消前仍可能成交；不會自動送出新委託。")
    shares = mof.parse_request(_form(symbol="ZZZZ", quantity="4"))
    assert "\n4 股，LMT $12.50（一般時段）\n" in mof.modify_prompt(row, shares, 4)


def test_replacement_after_cancel():
    request = mof.parse_request(_form(symbol="ZZZZ", input_mode=mof.CASH_MODE,
                                      amount="100", price="13", action="賣出",
                                      allow_short=True, session="盤前"))
    filled = mof.replacement_after_cancel(_row(status="FILLED", status_label="已成交"), request)
    assert filled == mof.Replacement("IB 已回報原委託：已成交；不會送出修改後委託。", None)
    none_left = mof.replacement_after_cancel(
        _row(status="CANCELLED", filled_quantity=7), request)
    assert none_left == mof.Replacement("取消確認前的成交已達修改後股數；沒有可重下的數量。", None)
    redo = mof.replacement_after_cancel(_row(status="CANCELLED", filled_quantity=2), request)
    assert redo.message == (
        "原委託已取消，期間成交 2 股。新委託數量已調整為 5 股；按「預覽委託」後再確認送出。")
    assert redo.form == ManualOrderForm(
        account="Rosa", symbol="ZZZZ", input_mode=mof.SHARES_MODE, quantity="5",
        amount="", percent="", order_type="LMT", price="13.0", session="盤前",
        action="賣出", allow_short=True,
    )


def test_shared_trading_modules_import_no_gui_toolkit():
    """Tk 與 Qt 共用的三個模組不能帶進任何 GUI 框架（在乾淨行程裡檢查）。"""
    import subprocess
    import sys
    from pathlib import Path

    automation = Path(__file__).resolve().parents[1]
    script = (
        "import sys; sys.path[:0] = sys.argv[1:3]\n"
        "import manual_order_form, sync_preview_rows, takeover_preview_text\n"
        "gui = ('tkinter', '_tkinter', 'PySide6', 'PyQt5', 'PyQt6', 'shiboken6')\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in gui))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script, str(automation), str(automation.parent)],
        capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip() == "[]"
