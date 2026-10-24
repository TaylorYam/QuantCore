"""策略單送出（#205）：一條 in-memory Broker 連線、一份策略單、一個 ctx。

只看 interface：送進假 Broker 的單（順序、股數、限價、outsideRth、account、orderId）、
撤單、等待秒數、回傳的 SendResult 與 log 文字。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace as NS

import pytest

import manual_attribution
import strategy_orders
from strategy_orders import Narrator, fill_summary, send
from tests.fake_broker import FakeBroker

MANUAL_OPEN = NS(order=NS(orderId=7, orderRef="QUANTCORE_MANUAL:ROSA:abc"),
                 contract=NS(symbol="BTC"))
TAKEOVER_OPEN = NS(order=NS(orderId=9, orderRef=manual_attribution.TAKEOVER_REF_PREFIX + "x"),
                   contract=NS(symbol="XYZ"))
STRATEGY_OPEN = NS(order=NS(orderId=8, orderRef=""), contract=NS(symbol="AAPL"))


def _ctx(order_type="LMT", dry_run=False, outside_rth=True, wait=30):
    from account_ctx import AccountCtx
    return AccountCtx(
        name="Rosa", account="U00000001", host="127.0.0.1", port=4999,
        client_id=99, dry_run=dry_run, order_type=order_type, outside_rth=outside_rth,
        wait_fill_sec=wait, alloc_sr=0.0, alloc_sp500=0.5, alloc_skewls=0.0,
        price_feed_port=None,
    )


@pytest.fixture
def env(monkeypatch):
    """假 Broker；沒有暫停中的標的；送單間隔記下來不真的睡。"""
    e = NS(fake=FakeBroker(managed=["U00000001"], price=50.0), sleeps=[], paused={},
           gate_paths=[])

    def paused(account_name, path=None):
        e.gate_paths.append(path)
        if isinstance(e.paused, Exception):
            raise e.paused
        return dict(e.paused)

    monkeypatch.setattr(manual_attribution, "paused_symbols", paused)
    monkeypatch.setattr(strategy_orders.time, "sleep", e.sleeps.append)
    return e


def _send(e, orders, ctx=None, **kw):
    ctx = ctx or _ctx()
    return send(e.fake.connect(ctx), orders, ctx, **kw)


def _sent(e):
    return [(p.symbol, p.action, p.qty) for p in e.fake.placed]


def order(symbol, action="BUY", qty=10, **kw):
    return {"symbol": symbol, "action": action, "qty": qty, "strategy": "SR", **kw}


# ── 送單順序與間隔 ────────────────────────────────────────────────────

def test_sells_go_before_buys_each_followed_by_a_short_pause(env):
    result = _send(env, [order("XLK", "BUY", 5), order("XLE", "SELL", 3),
                         order("XLB", "BUY", 2), order("XLU", "SELL", 1)])
    assert _sent(env) == [("XLE", "SELL", 3), ("XLU", "SELL", 1),
                          ("XLK", "BUY", 5), ("XLB", "BUY", 2)]
    assert env.sleeps == [0.2] * 4
    assert env.fake.waits == [30]                       # 全部送完才等成交
    assert [o["symbol"] for o in result.placed] == ["XLE", "XLU", "XLK", "XLB"]


def test_every_order_carries_the_account_and_outside_rth(env):
    _send(env, [order("XLK")], _ctx(outside_rth=True))
    [p] = env.fake.placed
    assert (p.account, p.outside_rth, p.order_type) == ("U00000001", True, "LMT")


def test_placed_orders_keep_the_old_dict_format_with_the_ib_order_id(env):
    result = _send(env, [order("XLE", "SELL", 3), order("XLK", "BUY", 5)])
    assert result.placed == [
        {"symbol": "XLE", "action": "SELL", "qty": 3, "lmt_price": round(99.9 * 0.997, 2),
         "strategy": "SR", "order_id": "500"},
        {"symbol": "XLK", "action": "BUY", "qty": 5, "lmt_price": round(100.1 * 1.003, 2),
         "strategy": "SR", "order_id": "501"},
    ]


def test_zero_quantity_is_not_sent(env):
    result = _send(env, [order("XLK", qty=0), order("XLE", qty=4)])
    assert _sent(env) == [("XLE", "BUY", 4)]
    assert [o["symbol"] for o in result.placed] == ["XLE"]


def test_planned_orders_are_accepted_too(env):
    from rebalance_plan import PlannedOrder
    result = _send(env, [PlannedOrder(symbol="XLK", action="BUY", qty=5, price=101.0,
                                      strategy="SR", target=5, current=0)])
    assert _sent(env) == [("XLK", "BUY", 5)]
    assert result.placed[0]["strategy"] == "SR"


# ── 撤掉策略舊單 ──────────────────────────────────────────────────────

def test_existing_strategy_orders_are_cancelled_but_manual_and_takeover_orders_kept(env, caplog):
    env.fake.open_orders = [MANUAL_OPEN, STRATEGY_OPEN, TAKEOVER_OPEN]
    caplog.set_level(logging.INFO)
    result = _send(env, [order("XLK")])
    assert env.fake.cancelled == [STRATEGY_OPEN.order]
    assert result.cancelled == 1
    assert env.fake.waits == [2, 30]                     # 撤單後等 2 秒，再等成交
    assert "  Cancelling 1 existing open orders..." in caplog.messages


def test_nothing_to_send_cancels_nothing(env):
    env.fake.open_orders = [STRATEGY_OPEN]
    result = _send(env, [])
    assert env.fake.cancelled == [] and env.fake.waits == []
    assert result == strategy_orders.SendResult([], [], [], 0)


# ── dry run ──────────────────────────────────────────────────────────

def test_dry_run_neither_sends_nor_cancels_nor_waits(env, caplog):
    env.fake.open_orders = [STRATEGY_OPEN]
    caplog.set_level(logging.INFO)
    result = _send(env, [order("XLK", qty=5)], _ctx(dry_run=True))
    assert env.fake.placed == [] and env.fake.cancelled == [] and env.fake.waits == []
    assert [o["symbol"] for o in result.placed] == ["XLK"]
    assert "order_id" not in result.placed[0]
    assert result.fills == []
    assert "  [DRY RUN] LMT BUY      5 XLK  (SR)" in caplog.messages


# ── 手動持倉暫停閘門（#106）─────────────────────────────────────────

def test_paused_symbols_are_held_back(env):
    env.paused = {"XLK": "手動持倉"}
    result = _send(env, [order("XLK"), order("XLE")])
    assert _sent(env) == [("XLE", "BUY", 10)]
    assert [o["symbol"] for o in result.placed] == ["XLE"]


def test_unreadable_pause_ledger_sends_and_cancels_nothing(env):
    env.fake.open_orders = [STRATEGY_OPEN]
    env.paused = manual_attribution.ManualAttributionError("db locked")
    with pytest.raises(manual_attribution.ManualAttributionError):
        _send(env, [order("XLE")])
    assert env.fake.placed == [] and env.fake.cancelled == []


def test_the_gate_reads_the_ledger_the_caller_names(env, tmp_path):
    _send(env, [order("XLE")], attribution_path=tmp_path / "paper.sqlite3")
    assert env.gate_paths == [tmp_path / "paper.sqlite3"]


# ── 限價 ─────────────────────────────────────────────────────────────

def test_lmt_uses_the_quote_and_the_unified_limit_rule(env):
    _send(env, [order("XLK", "BUY"), order("XLE", "SELL")])
    assert [(p.symbol, p.lmt) for p in env.fake.placed] == [
        ("XLE", round(99.9 * 0.997, 2)), ("XLK", round(100.1 * 1.003, 2))]


def test_without_quotes_the_orders_own_estimate_comes_first(env):
    env.fake.quote = {"bid": None, "ask": None, "mid": None, "last": None, "close": None}
    _send(env, [order("XLK", price=40.0)])
    assert env.fake.placed[0].lmt == round(40.0 * 1.003, 2)


def test_without_quotes_or_estimate_the_connection_reference_price_is_used(env):
    env.fake.quote = {"bid": None, "ask": None, "mid": None, "last": None, "close": None}
    _send(env, [order("XLK")])
    assert env.fake.placed[0].lmt == round(50.0 * 1.003, 2)          # FakeBroker.price


def test_an_order_without_any_price_is_skipped_with_a_warning(env, caplog, monkeypatch):
    env.fake.quote = {"bid": None, "ask": None, "mid": None, "last": None, "close": None}
    env.fake.price = 0.0
    seen = []
    result = _send(env, [order("XLK", qty=3)], log_fn=seen.append)
    assert env.fake.placed == [] and env.fake.waits == []
    assert result.placed == [] and result.fills == []
    assert result.skipped == [{"symbol": "XLK", "action": "BUY", "qty": 3, "reason": "no price"}]
    assert "    XLK: no price, skipping" in caplog.messages
    assert seen == ["    XLK: no price, skipping"]


def test_moo_needs_no_quote_and_records_the_estimate_rounded(env, monkeypatch):
    def no_quotes(*_a):
        raise AssertionError("MOO 不要報價")
    monkeypatch.setattr(type(env.fake.connect(_ctx())), "quotes", no_quotes)
    result = _send(env, [order("XLK", price=101.236)], _ctx(order_type="MOO", outside_rth=False))
    [p] = env.fake.placed
    assert (p.order_type, p.tif, p.outside_rth) == ("MKT", "OPG", False)
    assert result.placed[0]["lmt_price"] == 101.24


# ── 進度文字 ─────────────────────────────────────────────────────────

def test_the_caller_narrates_its_own_steps(env):
    said = []

    class Steps(Narrator):
        def placing(self, count):
            said.append(("placing", count))

        def priced(self, src, lmt):
            said.append(("priced", src["symbol"], lmt, src["reason"]))

        def checking_fills(self):
            said.append(("fills",))

    _send(env, [order("XLE", "SELL", 1, reason="r1"), order("XLK", reason="r2")],
          narrator=Steps())
    assert said == [("placing", 2), ("priced", "XLE", round(99.9 * 0.997, 2), "r1"),
                    ("priced", "XLK", round(100.1 * 1.003, 2), "r2"), ("fills",)]


def test_cancel_progress_is_mirrored_to_the_callers_log(env):
    env.fake.open_orders = [STRATEGY_OPEN]
    seen = []
    _send(env, [order("XLK")], log_fn=seen.append)
    assert seen == ["  Cancelling 1 existing open orders..."]


# ── 成交彙整 ─────────────────────────────────────────────────────────

def test_fills_are_summarised_per_order(env):
    result = _send(env, [order("XLK", qty=5)])
    assert result.fills == [{"symbol": "XLK", "action": "BUY", "qty": 5, "filled": 5,
                             "avg_price": round(100.1 * 1.003, 2), "status": "Filled"}]


def test_partial_and_unfilled_orders(env):
    env.fake.behavior, env.fake.partial_qty = "partial", 2
    assert _send(env, [order("XLK", qty=5)]).fills[0]["filled"] == 2
    env.fake.behavior = "working"
    [f] = _send(env, [order("XLE", qty=5)]).fills
    assert (f["filled"], f["avg_price"], f["status"]) == (0, 0.0, "Submitted")


def _trade(*fills, symbol="XLK", qty=200, status="Filled"):
    return NS(contract=NS(symbol=symbol), order=NS(action="BUY", totalQuantity=qty),
              orderStatus=NS(status=status),
              fills=[NS(execution=NS(shares=s, price=p, avgPrice=a)) for s, p, a in fills])


def test_several_executions_average_at_each_executions_own_price(caplog):
    """100@10 再 100@12：IB 的 avgPrice 是累計均價（10、11），正確均價是 11（不是 10.5）。"""
    caplog.set_level(logging.INFO)
    [f] = fill_summary([_trade((100, 10.0, 10.0), (100, 12.0, 11.0))])
    assert (f["filled"], f["avg_price"]) == (200, 11.0)
    assert "  Fill: BUY XLK  200/200sh @ $11.00  [Filled]" in caplog.messages


def test_a_single_execution_is_unchanged_and_class_shares_use_the_canonical_symbol():
    [f] = fill_summary([_trade((2, 501.5, 501.5), symbol="BRK B", qty=2)])
    assert f == {"symbol": "BRK.B", "action": "BUY", "qty": 2, "filled": 2,
                 "avg_price": 501.5, "status": "Filled"}


# ── 建 IB 單 ─────────────────────────────────────────────────────────

def test_make_order_by_order_type():
    moo = strategy_orders.make_order("BUY", 3, 0, _ctx(order_type="MOO"))
    assert (moo.orderType, moo.tif, moo.outsideRth, moo.account) == ("MKT", "OPG", False, "U00000001")
    lmt = strategy_orders.make_order("SELL", 3, 12.5, _ctx(order_type="LMT", outside_rth=True))
    assert (lmt.orderType, lmt.lmtPrice, lmt.outsideRth) == ("LMT", 12.5, True)
    mkt = strategy_orders.make_order("BUY", 3, 0, _ctx(order_type="MKT", outside_rth=True))
    assert (mkt.orderType, mkt.tif) == ("MKT", "GTC")


# ── 排程腳本不再被當函式庫 import（#205）──────────────────────────────

def test_no_module_imports_the_21_00_script():
    """16:00、Sync Pos、接管（含函式內的延遲 import）都不再借排程腳本的函式。"""
    import ast
    from pathlib import Path

    here = Path(strategy_orders.__file__).parent
    offenders = []
    for path in here.rglob("*.py"):
        if "tests" in path.relative_to(here).parts or path.name == "rebalance_ib.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module]
            else:
                continue
            if "rebalance_ib" in names:
                offenders.append(str(path.relative_to(here)))
    assert offenders == []
