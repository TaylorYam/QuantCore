"""
Unit tests for moo_fill_check（#26：MOO 成交回報精簡 + 0/0sh 修正）.

Regression anchor: 2026-07 起每則 21:31 回報都是 `0/0sh`、沒有成交價。
MOO 單是 21:00 由 rebalance 的 client 送出，21:31 這支 client 只看得到
completed order — ib_insync 0.9.86 `wrapper.completedOrder` 只帶 status，
orderStatus.filled / avgFillPrice / order.totalQuantity 都是 0。股數與成交價
必須從 ib.fills()（executions）依 permId 對應。

Fake objects mirror the ib_insync attributes the module reads. No IB, no network.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from tests.fake_broker import install_connect

pytest.importorskip("ib_insync")


def trade(sym, action, *, perm=0, total=0, status="Filled", filled=0.0,
          avg=0.0, order_type="MKT", tif="OPG", fills=()):
    return NS(
        contract=NS(symbol=sym),
        order=NS(action=action, totalQuantity=total, permId=perm,
                 orderType=order_type, tif=tif),
        orderStatus=NS(status=status, filled=filled, avgFillPrice=avg),
        fills=list(fills),
    )


def fill(sym, side, shares, price, *, perm=0, exec_id=None, order_id=1):
    return NS(
        contract=NS(symbol=sym),
        execution=NS(side=side, shares=float(shares), price=float(price),
                     permId=perm, orderId=order_id,
                     execId=exec_id or f"{sym}.{perm}.{shares}.{price}"),
    )


# ── summarize_moo_trades ──────────────────────────────────────────────

def test_completed_order_takes_shares_and_price_from_executions():
    """0/0sh 回歸：completed order 沒帶數量，改用 permId 對應的成交紀錄。"""
    from moo_fill_check import format_fill_line, summarize_moo_trades
    t = trade("ERY", "BUY", perm=111)          # total=0, filled=0, avg=0
    fills = [fill("ERY", "BOT", 30, 12.40, perm=111),
             fill("ERY", "BOT", 25, 12.42, perm=111)]
    [row] = summarize_moo_trades([t], fills, {"ERY": 55.0})
    assert row.filled == 55
    assert row.avg_price == pytest.approx((30 * 12.40 + 25 * 12.42) / 55)
    assert row.complete
    assert format_fill_line(row) == "🟢 BUY 55 ERY @ 12.41"


def test_sell_to_flat_line():
    from moo_fill_check import format_fill_line, summarize_moo_trades
    t = trade("SEF", "SELL", perm=7)
    [row] = summarize_moo_trades([t], [fill("SEF", "SLD", 21, 15.02, perm=7)], {})
    assert format_fill_line(row) == "🔴 SELL 21 SEF @ 15.02"


def test_short_and_cover_direction_uses_post_position():
    from moo_fill_check import format_fill_line, summarize_moo_trades
    rows = summarize_moo_trades(
        [trade("XLC", "SELL", perm=1), trade("XLE", "BUY", perm=2)],
        [fill("XLC", "SLD", 10, 100.0, perm=1), fill("XLE", "BOT", 8, 90.0, perm=2)],
        {"XLC": -10.0, "XLE": 0.0},
    )
    assert format_fill_line(rows[0]) == "🔻 SHORT 10 XLC @ 100.00"
    assert format_fill_line(rows[1]) == "💚 COVER 8 XLE @ 90.00"


def test_partial_fill_is_flagged():
    from moo_fill_check import format_fill_line, summarize_moo_trades
    t = trade("XLF", "BUY", perm=5, total=100, status="Cancelled")
    [row] = summarize_moo_trades([t], [fill("XLF", "BOT", 40, 10.0, perm=5)], {"XLF": 40.0})
    assert not row.complete
    assert format_fill_line(row) == "🟢 BUY 40/100 XLF @ 10.00 ⚠️ 部分成交 [Cancelled]"


def test_unknown_positions_do_not_turn_buys_into_covers():
    """部位查詢失敗時不判斷方向 — 否則所有有成交的 BUY 都會被標成 COVER。"""
    from moo_fill_check import format_fill_line, summarize_moo_trades
    [row] = summarize_moo_trades([trade("XLB", "BUY", perm=2)],
                                 [fill("XLB", "BOT", 12, 88.1, perm=2)], None)
    assert format_fill_line(row) == "🟢 BUY 12 XLB @ 88.10"


def test_unfilled_order_with_unknown_quantity():
    from moo_fill_check import format_fill_line, summarize_moo_trades
    [row] = summarize_moo_trades([trade("XLK", "BUY", perm=9, status="Cancelled")], [], {})
    assert not row.complete
    assert format_fill_line(row) == "🟢 BUY XLK ⚠️ 未成交 [Cancelled]"


def test_pending_order_shows_hourglass():
    from moo_fill_check import format_fill_line, summarize_moo_trades
    t = trade("XLK", "BUY", perm=9, total=10, status="PreSubmitted")
    [row] = summarize_moo_trades([t], [], {})
    assert format_fill_line(row) == "🟢 BUY 0/10 XLK ⏳ 未成交 [PreSubmitted]"


def test_perm_id_missing_falls_back_to_symbol_and_side():
    from moo_fill_check import summarize_moo_trades
    t = trade("XLB", "BUY", perm=0)
    fills = [fill("XLB", "SLD", 5, 88.0), fill("XLB", "BOT", 12, 88.1)]
    [row] = summarize_moo_trades([t], fills, {"XLB": 12.0})
    assert row.filled == 12 and row.avg_price == pytest.approx(88.1)


def test_perm_id_present_does_not_claim_other_orders_fills():
    """permId 對不上（例如同 symbol 另有 Panel LMT 成交）不能硬套。"""
    from moo_fill_check import summarize_moo_trades
    t = trade("XLB", "BUY", perm=42, status="Cancelled")
    [row] = summarize_moo_trades([t], [fill("XLB", "BOT", 12, 88.1, perm=99)], {})
    assert row.filled == 0


def test_same_session_trade_uses_order_status():
    from moo_fill_check import summarize_moo_trades
    t = trade("XLV", "BUY", perm=3, total=4, filled=4.0, avg=140.5)
    [row] = summarize_moo_trades([t], [], {"XLV": 24.0})
    assert (row.qty, row.filled, row.avg_price) == (4, 4, 140.5)


def test_unset_double_quantity_is_treated_as_unknown():
    from moo_fill_check import summarize_moo_trades
    t = trade("XLK", "BUY", perm=9, total=1.7976931348623157e308, status="Cancelled")
    [row] = summarize_moo_trades([t], [], {})
    assert row.qty == 0


# ── sections ─────────────────────────────────────────────────────────

def test_section_counts_complete_orders():
    from moo_fill_check import MooFill, moo_section
    ok = MooFill("ERY", "BUY", 0, 55, 12.41, "Filled", 55.0)
    bad = MooFill("XLK", "BUY", 0, 0, 0.0, "Cancelled", 0.0)
    headline, body, level = moo_section([ok, ok])
    assert (headline, level) == ("2/2", "ok")
    headline, body, level = moo_section([ok, bad])
    assert (headline, level) == ("1/2", "warn")
    assert body[1] == "🟢 BUY XLK ⚠️ 未成交 [Cancelled]"
    assert moo_section([]) is None


def test_summarize_fills_aggregates_executions():
    from moo_fill_check import moo_section, summarize_fills
    rows = summarize_fills(
        [fill("XLE", "SLD", 30, 57.20, perm=1), fill("XLE", "SLD", 17, 57.30, perm=1),
         fill("XLB", "BOT", 33, 51.41, perm=2)],
        {"XLB": 33.0},
    )
    assert [(r.symbol, r.action, r.filled) for r in rows] == [("XLE", "SELL", 47), ("XLB", "BUY", 33)]
    headline, body, level = moo_section(rows, from_executions=True)
    assert headline == "2 筆（從成交紀錄回溯）"
    assert body[1] == "🟢 BUY 33 XLB @ 51.41"


# ── main()：沒單靜默、失敗回報 ─────────────────────────────────────────

def broker_with(trades=(), fills=(), positions=None):
    """in-memory Broker：這次連線看到的今日委託、成交與持倉。"""
    from tests.fake_broker import FakeBroker
    fake = FakeBroker(managed=["U1"], positions=positions)
    fake.session_trades, fake.session_fills = list(trades), list(fills)
    return fake


CTX = NS(account="U1", name="Taylor", host="127.0.0.1", port=4001, client_id=22)


def test_main_connects_through_the_broker_and_logs_the_same_line(monkeypatch, caplog):
    """#208：直接向 Broker 要連線（timeout 20 秒），連上後那行 log 和 #185 之前逐字相同。"""
    import moo_fill_check as m
    from notify import Digest
    fake = broker_with()
    seen = []
    install_connect(monkeypatch, lambda ctx, **kw: seen.append(kw) or fake.connect(ctx))
    caplog.set_level("INFO")
    m.main(CTX, digest=Digest("MOO 成交"))
    assert seen == [{"timeout": 20}]
    assert "Connected  host=127.0.0.1 port=4001 clientId=22 account=U1" in caplog.messages


def test_main_no_moo_orders_stays_silent(monkeypatch):
    import moo_fill_check as m
    from notify import Digest
    fake = broker_with()
    install_connect(monkeypatch, fake.connect)
    d = Digest("MOO 成交")
    m.main(CTX, digest=d)
    assert d.render() is None
    assert fake.closed == 1


def test_main_reports_fills_into_digest(monkeypatch):
    import moo_fill_check as m
    from notify import Digest
    fake = broker_with(
        trades=[trade("ERY", "BUY", perm=111), trade("SPY", "BUY", perm=5, order_type="LMT", tif="DAY")],
        fills=[fill("ERY", "BOT", 55, 12.41, perm=111)],
        positions={"ERY": 55.0},
    )
    install_connect(monkeypatch, fake.connect)
    d = Digest("MOO 成交", icon="✅")
    m.main(CTX, digest=d)
    msg = d.render()
    assert "<b>Taylor</b> 1/1\n🟢 BUY 55 ERY @ 12.41" in msg
    assert "SPY" not in msg            # 非 MOO（LMT/DAY）不列入


def test_main_connect_failure_adds_error_section(monkeypatch):
    import moo_fill_check as m
    from notify import Digest

    def boom(ctx, **_kw):
        raise ConnectionRefusedError("refused <port>")

    install_connect(monkeypatch, boom)
    d = Digest("MOO 成交", icon="✅")
    m.main(CTX, digest=d)
    msg = d.render()
    assert msg.startswith("<b>❌ MOO 成交</b>")
    assert "<b>Taylor</b> ❌ IB 連線失敗" in msg
    assert "refused &lt;port&gt;" in msg
