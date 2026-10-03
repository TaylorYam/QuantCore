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

from datetime import datetime
from types import SimpleNamespace as NS

import pytest

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


CTX = NS(account="U1", name="Taylor", host="127.0.0.1", port=4001, client_id=22,
         tg_token="T", tg_chat_id="C")


def test_main_no_moo_orders_stays_silent(monkeypatch):
    import moo_fill_check as m
    from notify import Digest
    ib = broker_with()
    monkeypatch.setattr(m.ib_fetcher, "connect", ib.connect)
    d = Digest("MOO 成交")
    m.main(CTX, digest=d)
    assert d.render() is None
    assert ib.closed == 1


def test_main_reports_fills_into_digest(monkeypatch):
    import moo_fill_check as m
    from notify import Digest
    ib = broker_with(
        trades=[trade("ERY", "BUY", perm=111), trade("SPY", "BUY", perm=5, order_type="LMT", tif="DAY")],
        fills=[fill("ERY", "BOT", 55, 12.41, perm=111)],
        positions={"ERY": 55.0},
    )
    monkeypatch.setattr(m.ib_fetcher, "connect", ib.connect)
    d = Digest("MOO 成交", icon="✅")
    m.main(CTX, digest=d)
    msg = d.render()
    assert "<b>Taylor</b> 1/1\n🟢 BUY 55 ERY @ 12.41" in msg
    assert "SPY" not in msg            # 非 MOO（LMT/DAY）不列入


def test_main_connect_failure_adds_error_section(monkeypatch):
    import moo_fill_check as m
    from notify import Digest

    def boom(ctx):
        raise ConnectionRefusedError("refused <port>")

    monkeypatch.setattr(m.ib_fetcher, "connect", boom)
    d = Digest("MOO 成交", icon="✅")
    m.main(CTX, digest=d)
    msg = d.render()
    assert msg.startswith("<b>❌ MOO 成交</b>")
    assert "<b>Taylor</b> ❌ IB 連線失敗" in msg
    assert "refused &lt;port&gt;" in msg


# ── run_all()：三帳戶合一則 + 當日排程總結（#38）──────────────────────

WEEKDAY_2131 = datetime(2026, 9, 15, 21, 31, 5)


def _run_all(monkeypatch, ibs, *, summary=True, now=WEEKDAY_2131, names=("Taylor", "Rosa", "Yvonne")):
    import moo_fill_check as m
    import notify
    import run_status
    sent, seen = [], []
    monkeypatch.setattr(notify, "tg_send",
                        lambda msg, *, token, chat_id: sent.append((msg, token, chat_id)))
    monkeypatch.setattr(m.ib_fetcher, "connect",
                        lambda ctx: (ibs.get(ctx.name) or broker_with()).connect(ctx))

    def fake_summary(now_, accounts, moo_outcomes):
        seen.append((now_, list(accounts), dict(moo_outcomes)))
        return "✅ 9/15 排程都跑完：（總結）", "ok"

    monkeypatch.setattr(run_status, "daily_summary", fake_summary)
    failures = m.run_all(list(names), summary=summary, now=now)
    return failures, sent, seen


def test_run_all_quiet_day_sends_only_the_summary(fake_config, monkeypatch):
    failures, sent, seen = _run_all(monkeypatch, {})
    assert failures == []
    assert sent == [("✅ 9/15 排程都跑完：（總結）", "FAKE_TOKEN", "FAKE_CHAT")]
    assert seen == [(WEEKDAY_2131, ["Taylor", "Rosa", "Yvonne"],
                     {"Taylor": ("ok", None), "Rosa": ("ok", None), "Yvonne": ("ok", None)})]


def test_run_all_fills_then_summary_in_one_message(fake_config, monkeypatch):
    ib = broker_with(trades=[trade("ERY", "BUY", perm=111)],
                     fills=[fill("ERY", "BOT", 55, 12.41, perm=111)],
                     positions={"ERY": 55.0})
    _, sent, seen = _run_all(monkeypatch, {"Taylor": ib})
    assert len(sent) == 1
    msg = sent[0][0]
    assert msg.startswith("<b>✅ MOO 成交 ")
    assert "<b>Taylor</b> 1/1\n🟢 BUY 55 ERY @ 12.41" in msg
    assert msg.endswith("\n\n✅ 9/15 排程都跑完：（總結）")
    assert seen[0][2]["Taylor"] == ("ok", "1/1")


def test_run_all_without_summary_stays_silent(fake_config, monkeypatch):
    """手動跑單一帳戶（summary=False）：沒單照舊不發。"""
    _, sent, seen = _run_all(monkeypatch, {}, summary=False, names=("Taylor",))
    assert sent == [] and seen == []


def test_run_all_daytime_run_has_no_summary(fake_config, monkeypatch):
    _, sent, seen = _run_all(monkeypatch, {}, now=datetime(2026, 9, 15, 10, 0))
    assert sent == [] and seen == []


def test_run_all_ctx_failure_is_reported(fake_config, monkeypatch):
    """ctx 建不起來（帳戶名打錯、accounts.json 壞掉）以前只寫 log。"""
    failures, sent, seen = _run_all(monkeypatch, {}, names=("Taylor", "Nobody"))
    assert [n for n, _ in failures] == ["Nobody"]
    msg = sent[0][0]
    assert msg.startswith("<b>❌ MOO 成交 ")
    assert "<b>Nobody</b> ❌ 成交回報失敗" in msg
    assert seen[0][2]["Nobody"] == ("error", "❌ 成交回報失敗")
