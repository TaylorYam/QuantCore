"""sync_preview_rows：Sync Pos 預覽的列內容與 Output Log 摘要（Tk／Qt 共用，不依賴 GUI）。"""

from account_ctx import AccountCtx
from rebalance_plan import PlannedOrder as PO
from sync_position import SyncPlan

import sync_preview_rows as rows


def _ctx(sr=0.3, sp=0.5, sk=0.2):
    return AccountCtx(name="Taylor", account="U0", host="127.0.0.1", port=1, client_id=1,
                      dry_run=True, order_type="LMT", outside_rth=True, wait_fill_sec=0,
                      alloc_sr=sr, alloc_sp500=sp, alloc_skewls=sk, price_feed_port=None,
                      tg_token="", tg_chat_id="")


def _plan(**changes):
    base = dict(
        ctx=_ctx(),
        orders=[PO("AAPL", "BUY", 150, 100.0, "SP500", 250, 100),
                PO("XLK", "SELL", 12, 210.55, "SR+SkewLS", -5, 7)],
        nav=123456.78, sr_wgt={"XLE": 0.4, "XLK": 0.6},
        sp500_wgt={"AAPL": 0.25, "MSFT": 0.5}, sr_date="2026-09-25",
        sp500_date="2026-09-26",
        prices={"AAPL": 100.0, "MSFT": 410.2, "XLK": 210.55, "XLE": 88.0, "SEF": 40.1,
                "XLC": 95.5, "MAN": 7.0, "OLD": 5.0},
        targets={"AAPL": {"shares": 250}, "MSFT": {"shares": 75}, "XLK": {"shares": -5},
                 "XLE": {"shares": 252}, "SEF": {"shares": 30}, "XLC": {"shares": -40}},
        current_pos={"AAPL": 100.0, "XLK": 7.0, "SEF": 27.0, "XLC": -40.0, "MAN": 3.0,
                     "OLD": 2.0},
        current_other_holdings=["MAN"],
        skewls_wgt={"SEF": 0.05, "XLC": -0.15}, skewls_date="2026-09-24",
    )
    return SyncPlan(**{**base, **changes})


def test_plan_subtitle_shows_skewls_date_only_when_allocated():
    assert rows.plan_subtitle(_plan()) == (
        "NAV: $123,457   |   SR: 2026-09-25   |   SP500: 2026-09-26   |   SkewLS: 2026-09-24")
    assert rows.plan_subtitle(_plan(ctx=_ctx(sk=0.0))) == (
        "NAV: $123,457   |   SR: 2026-09-25   |   SP500: 2026-09-26")


def test_target_sections_per_strategy_with_overlap_note():
    sr, sp, sk = rows.target_sections(_plan())
    assert (sr.strategy, sr.title, sr.color) == ("SR", "SR (30% of NAV)", "blue")
    assert sr.lines == [
        "  XLK      60.0%  →   106sh  @ $210.55  = $22,318  [combined: -5sh]",
        "  XLE      40.0%  →   168sh  @ $88.00  = $14,784  [combined: +252sh]",
    ]
    assert (sp.strategy, sp.title, sp.color) == ("SP500", "SP500 Momentum (50% of NAV)", "green")
    assert sp.lines[0] == "  MSFT     50.0%  →    75sh  @ $410.20  = $30,765"
    assert (sk.strategy, sk.title, sk.color) == ("SkewLS", "Skew LS (20% of NAV)", "red")
    assert sk.lines == [
        "  SHORT XLC     -15.0%  →   -39sh  @ $95.50  = $-3,724  [combined: -40sh]",
        "  LONG  SEF      +5.0%  →   +31sh  @ $40.10  = $+1,243  [combined: +30sh]",
    ]


def test_target_sections_empty_when_all_cash():
    assert rows.target_sections(_plan(sr_wgt={}, sp500_wgt={}, skewls_wgt={})) == []


def test_current_holdings_tag_strategy_and_flag_untargeted():
    holdings = rows.current_holding_rows(_plan())
    assert [(h.text, h.tag, h.tag_color, h.mark, h.mark_color) for h in holdings] == [
        ("  AAPL     100sh  @ $100.00  = $10,000", "SP500", "green", "", "text"),
        ("  OLD        2sh  @ $5.00  = $10", "SP500", "green", "  ← not in target", "red"),
        ("  SEF       27sh  @ $40.10  = $1,083", "SkewLS", "red", "", "text"),
        ("  XLC      -40sh  @ $95.50  = $-3,820", "SkewLS", "red", "", "text"),
        ("  XLK        7sh  @ $210.55  = $1,474", "SR", "blue", "", "text"),
    ]
    assert rows.other_holding_lines(_plan()) == ["  MAN        3sh  @ $7.00  = $21"]


def test_paused_rows_show_the_held_order_or_none():
    plan = _plan(paused={"XYZ": "手動持倉 10 股已進入 SP500 Universe", "QQQ": "另一個原因"},
                 paused_orders=[PO("XYZ", "BUY", 240, 50.0, "SP500", 250, 10)])
    assert rows.paused_rows(plan) == [
        rows.PausedRow("QQQ", "  QQQ     本次沒有對應的策略單", "    另一個原因"),
        rows.PausedRow("XYZ", "  XYZ     原本要 BUY 240sh (10→250)",
                       "    手動持倉 10 股已進入 SP500 Universe"),
    ]


def test_trade_rows_sells_first_then_totals():
    plan = _plan(orders=[PO("AAPL", "BUY", 150, 100.0, "SP500", 250, 100),
                         PO("XLK", "SELL", 12, 210.55, "SR+SkewLS", -5, 7)])
    assert rows.trades_heading(plan) == "Required Trades (需要交易: 2 筆)"
    assert rows.trade_rows(plan) == [
        rows.TradeRow(("SELL", "XLK", "12", "7", "-5", "$210.55", "$2,527", "SR+SkewLS"),
                      "red"),
        rows.TradeRow(("BUY", "AAPL", "150", "100", "250", "$100.00", "$15,000", "SP500"),
                      "green"),
    ]
    assert len(rows.TRADE_COLUMNS) == len(rows.TRADE_COL_WIDTHS) == 8
    assert rows.trade_totals_text(plan) == (
        "SELL: $2,527   |   BUY: $15,000   |   Net: +$12,473")
    net_sell = _plan(orders=[PO("AAA", "SELL", 10, 10.0, "SP500", 0, 10)])
    assert rows.trade_totals_text(net_sell) == "SELL: $100   |   BUY: $0   |   Net: $-100"


def test_confirm_prompt_and_sending_log():
    plan = _plan()
    assert rows.confirm_prompt(plan) == (
        "確定送出 2 筆限價單到 [Taylor]？\n(LMT ±0.3% buffer, GTC, outside RTH)")
    assert rows.sending_log_line(plan) == "[>] Sending 2 LMT orders for [Taylor]..."


def test_log_summary_lines():
    plan = _plan(paused={"XYZ": "原因"}, orders=[])
    lines = rows.log_summary_lines(plan)
    assert lines[0] == lines[-1] == "─" * 40
    assert "  SR (30% of NAV):" in lines
    assert "    XLK     60.0%  →  106sh @ $210.55  [combined w/ overlap: -5sh]" in lines
    assert "    SHORT XLC    -15.0%  →  -39sh @ $95.50  [combined w/ overlap: -40sh]" in lines
    assert "    OLD       2sh @ $5.00 ← NOT in target" in lines
    assert "    MAN" not in "\n".join(lines)
    assert "  Required Trades: 0 筆" in lines
    assert "    Already on target" in lines
    assert "  ⏸ XYZ 策略單暫停：原因" in lines
    with_orders = rows.log_summary_lines(_plan())
    assert "    SELL   12sh XLK    (7→-5)  ~$2,527  [SR+SkewLS]" in with_orders
    assert "    Already on target" not in with_orders
