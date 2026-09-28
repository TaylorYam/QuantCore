"""
手動持倉歸屬與策略送單暫停（#106）。

歸屬（manual_attribution）：啟用時的既有策略外持倉、Panel 手動單的實際成交
（部分成交、賣出、放空、回補）、IB App 交易、重啟、Universe 變化、對帳不一致、
讀檔失敗、帳戶／標的隔離。送單入口：rebalance_ib.main、Sync Pos、
sp500_daily_check 用 fake IB 走真實流程 — 暫停標的不送、其他單照舊、
Panel 手動單不被「全部取消」、暫停只通知一次。

全部離線：假 IB、假訊號、tmp_path 的 SQLite；不連 IB、不發 Telegram。
"""

from __future__ import annotations

import sqlite3
from datetime import date
from types import SimpleNamespace as NS

import pytest

import manual_attribution as ma

UNIVERSE = {"AAPL", "MSFT", "SPY", "SSO"}
JOINED = UNIVERSE | {"XYZ"}          # XYZ 被加入 S&P 500 之後


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """每個測試自己的歸屬紀錄與手動單資料庫（預設路徑也指過去）。"""
    ledger = tmp_path / "manual_attribution.sqlite3"
    orders = tmp_path / "manual_orders.sqlite3"
    monkeypatch.setattr(ma, "DEFAULT_LEDGER_PATH", ledger)
    monkeypatch.setattr(ma, "DEFAULT_MANUAL_ORDERS_PATH", orders)
    return NS(ledger=ledger, orders=orders)


def _manual_order(path, ref, account, symbol, action, filled, status,
                  quantity=None):
    """寫一筆 Panel 手動單 — 用 manual_orders 自己的 schema 建表。"""
    from manual_orders import ManualOrderStore

    ManualOrderStore(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO manual_orders (
                   order_ref, account_name, account_id, symbol, action, quantity,
                   limit_price, tif, outside_rth, contract_id, status,
                   status_message, filled_quantity, created_at, updated_at)
               VALUES (?, ?, 'U0', ?, ?, ?, 10, 'DAY', 0, 1, ?, '', ?, ?, ?)
               ON CONFLICT(order_ref) DO UPDATE SET
                   status=excluded.status,
                   filled_quantity=excluded.filled_quantity""",
            (ref, account, symbol, action, quantity or max(filled, 1), status,
             filled, f"2026-09-28T00:00:{len(ref):02d}", "2026-09-28T00:00:00"))


def _eval(account, positions, universe=UNIVERSE):
    return ma.evaluate(account, positions, universe)


# ── 歸屬 ─────────────────────────────────────────────────────────────

def test_enable_records_existing_non_strategy_holdings(paths):
    """啟用 = 第一次觀察：當下已是策略外的持倉（含碎股）記為手動，不暫停任何標的。"""
    r = _eval("Rosa", {"BTC": 65.1783, "XLK": 15.0, "AAPL": 3.0, "SEF": 3.0})
    assert r.manual == {"BTC": 65.1783}
    assert r.paused == {} and r.new_notices == []


def test_no_ledger_means_nothing_paused_and_no_file_created(paths):
    kept, held = ma.gate_orders("Rosa", [{"symbol": "AAPL"}, {"symbol": "XYZ"}])
    assert [o["symbol"] for o in kept] == ["AAPL", "XYZ"] and held == []
    assert not paths.ledger.exists()
    assert ma.read_manual_fills("Rosa") == [] and not paths.orders.exists()


def test_ib_app_trades_on_non_strategy_symbol_follow_ib_silently(paths, caplog):
    """IB App／TWS 買賣策略外標的：股數跟著 IB，不暫停、不通知（使用者 09-28 決定）。"""
    _eval("Rosa", {"BTC": 65.1783})
    r = _eval("Rosa", {"BTC": 70.0, "SKHY": 120.0})
    assert r.manual == {"BTC": 70.0, "SKHY": 120.0}
    assert r.paused == {} and r.new_notices == []
    r = _eval("Rosa", {"SKHY": 120.0})          # BTC 在 App 賣光
    assert r.manual == {"SKHY": 120.0}


def test_collision_pauses_only_that_account_and_symbol(paths):
    """手動持倉的標的進入 SP500 Universe → 只停 Rosa／XYZ；Taylor 的 XYZ 是策略部位。"""
    _eval("Rosa", {"XYZ": 10.0, "AAPL": 5.0})
    _eval("Taylor", {"AAPL": 2.0})
    rosa = _eval("Rosa", {"XYZ": 10.0, "AAPL": 5.0}, JOINED)
    taylor = _eval("Taylor", {"AAPL": 2.0, "XYZ": 4.0}, JOINED)

    assert list(rosa.paused) == ["XYZ"]
    assert "手動持倉 10 股已進入 SP500 Universe" in rosa.paused["XYZ"]
    assert rosa.new_notices == [("XYZ", rosa.paused["XYZ"])]
    assert taylor.paused == {}

    orders = [{"symbol": "XYZ", "action": "SELL"}, {"symbol": "AAPL", "action": "BUY"}]
    assert [o["symbol"] for o in ma.gate_orders("Rosa", orders)[0]] == ["AAPL"]
    assert [o["symbol"] for o in ma.gate_orders("Taylor", orders)[0]] == ["XYZ", "AAPL"]


def test_pause_survives_restart_and_notifies_once(paths):
    _eval("Rosa", {"XYZ": 10.0})
    first = _eval("Rosa", {"XYZ": 10.0}, JOINED)
    ma.mark_notified("Rosa", [s for s, _ in first.new_notices])
    # 重啟 = 新行程只剩檔案
    again = _eval("Rosa", {"XYZ": 10.0}, JOINED)
    assert again.paused == first.paused and again.new_notices == []
    assert ma.paused_symbols("Rosa") == first.paused


def test_fills_update_attribution_not_submissions(paths):
    """送出不算、成交才算；部分成交照實際股數；觀察後的成交只加差額。"""
    _manual_order(paths.orders, "M:1", "Rosa", "XYZ", "BUY", 0, "ACCEPTED", 10)
    r = _eval("Rosa", {}, UNIVERSE)
    assert r.manual == {}                                # 送出但未成交
    _manual_order(paths.orders, "M:1", "Rosa", "XYZ", "BUY", 4, "PARTIAL", 10)
    r = _eval("Rosa", {"XYZ": 4.0})
    assert r.manual == {"XYZ": 4.0}
    _manual_order(paths.orders, "M:1", "Rosa", "XYZ", "BUY", 7, "CANCELLED", 10)
    # 成交紀錄更新了、還沒再觀察 IB 就碰撞：4（觀察）+ 3（之後的成交）
    r = _eval("Rosa", {"XYZ": 7.0}, JOINED)
    assert r.manual == {"XYZ": 7.0}
    assert "手動持倉 7 股" in r.paused["XYZ"]
    assert "對不上" not in r.paused["XYZ"]


def test_fills_before_any_observation_count_in_full(paths):
    """手動單成交後、還沒被觀察就碰撞：成交量全算手動。"""
    _manual_order(paths.orders, "M:1", "Rosa", "XYZ", "BUY", 10, "FILLED")
    r = _eval("Rosa", {"XYZ": 10.0}, JOINED)
    assert r.manual == {"XYZ": 10.0} and "手動持倉 10 股" in r.paused["XYZ"]


def test_sell_short_and_cover_signs(paths):
    _manual_order(paths.orders, "M:1", "Rosa", "XYZ", "SELL_SHORT", 5, "FILLED")
    r = _eval("Rosa", {"XYZ": -5.0}, JOINED)
    assert r.manual == {"XYZ": -5.0} and "手動持倉 -5 股" in r.paused["XYZ"]
    _manual_order(paths.orders, "M:2", "Rosa", "XYZ", "COVER", 5, "FILLED")
    r = _eval("Rosa", {}, JOINED)
    assert r.manual == {} and r.paused == {}


def test_manual_sell_to_zero_releases_and_a_new_episode_notifies_again(paths):
    _manual_order(paths.orders, "M:1", "Rosa", "XYZ", "BUY", 10, "FILLED")
    _eval("Rosa", {"XYZ": 10.0})
    _manual_order(paths.orders, "M:2", "Rosa", "XYZ", "SELL", 0, "ACCEPTED", 10)
    r = _eval("Rosa", {"XYZ": 10.0}, JOINED)
    assert "未結案" in r.paused["XYZ"]
    ma.mark_notified("Rosa", ["XYZ"])

    _manual_order(paths.orders, "M:2", "Rosa", "XYZ", "SELL", 10, "FILLED", 10)
    r = _eval("Rosa", {}, JOINED)
    assert r.paused == {} and ma.paused_symbols("Rosa") == {}

    _manual_order(paths.orders, "M:3", "Rosa", "XYZ", "BUY", 0, "SUBMISSION_UNKNOWN", 3)
    r = _eval("Rosa", {}, JOINED)
    assert r.new_notices == [("XYZ", r.paused["XYZ"])]


def test_ib_differs_from_manual_record_stays_paused_with_both_numbers(paths):
    """碰撞後在 App 賣掉一部分：紀錄對不上 → 照樣暫停，原因寫出兩邊股數（等 #107）。"""
    _eval("Rosa", {"XYZ": 10.0})
    r = _eval("Rosa", {"XYZ": 7.0}, JOINED)
    assert "手動持倉 10 股" in r.paused["XYZ"] and "IB 實際 7 股" in r.paused["XYZ"]
    r = _eval("Rosa", {}, JOINED)                    # 全賣光也不自動解除
    assert "IB 實際 0 股" in r.paused["XYZ"]


def test_unknown_status_or_action_is_unresolved(paths):
    _manual_order(paths.orders, "M:1", "Rosa", "XYZ", "BUY", 0, "SOMETHING_NEW")
    assert "未結案" in _eval("Rosa", {}, JOINED).paused["XYZ"]


def test_manual_orders_of_other_accounts_are_ignored(paths):
    _manual_order(paths.orders, "M:1", "Taylor", "XYZ", "BUY", 10, "FILLED")
    assert _eval("Rosa", {"XYZ": 3.0}, JOINED).paused == {}
    assert "XYZ" in _eval("Taylor", {"XYZ": 10.0}, JOINED).paused


def test_empty_universe_keeps_previous_pauses_and_ledger(paths):
    """成分股讀檔異常（只剩 SPY/SSO）：不把 SP500 持股誤記成手動、既有暫停不解除。"""
    _eval("Rosa", {"XYZ": 10.0})
    paused = _eval("Rosa", {"XYZ": 10.0}, JOINED).paused
    r = _eval("Rosa", {"XYZ": 10.0, "AAPL": 5.0}, {"SPY", "SSO"})
    assert r.paused == paused
    r = _eval("Rosa", {"XYZ": 10.0, "AAPL": 5.0}, JOINED)
    assert "AAPL" not in r.manual and r.paused == paused


def test_corrupt_ledger_fails_closed(paths):
    paths.ledger.write_bytes(b"not a sqlite database" * 100)
    with pytest.raises(ma.ManualAttributionError):
        _eval("Rosa", {"BTC": 1.0})
    with pytest.raises(ma.ManualAttributionError):
        ma.gate_orders("Rosa", [{"symbol": "AAPL"}])


def test_empty_ledger_file_has_no_pauses(paths):
    """第一次寫入沒完成（只留下空檔）不等於損毀：沒有暫停，下次照常啟用。"""
    sqlite3.connect(paths.ledger).close()
    assert ma.paused_symbols("Rosa") == {}
    assert _eval("Rosa", {"BTC": 1.0}).manual == {"BTC": 1.0}


def test_corrupt_manual_orders_fails_closed(paths):
    paths.orders.write_bytes(b"not a sqlite database" * 100)
    with pytest.raises(ma.ManualAttributionError):
        _eval("Rosa", {"BTC": 1.0})


def test_manual_orders_reader_is_read_only(paths):
    _manual_order(paths.orders, "M:1", "Rosa", "BTC", "BUY", 1, "FILLED")
    before = paths.orders.read_bytes()
    fills = ma.read_manual_fills("Rosa")
    assert [(f.symbol, f.action, f.filled_quantity, f.status) for f in fills] == [
        ("BTC", "BUY", 1, "FILLED")]
    assert paths.orders.read_bytes() == before


def test_strategy_open_trades_skip_manual_orders():
    manual = NS(order=NS(orderRef="QUANTCORE_MANUAL:ROSA:abc"))
    strategy = NS(order=NS(orderRef=""))
    assert ma.strategy_open_trades([manual, strategy]) == [strategy]


# ── 對帳報表（rebalance_plan）────────────────────────────────────────

def _row(sym, category, delta=5):
    from rebalance_plan import DeltaRow
    return DeltaRow(sym, category, "r", 10, 10 - delta, delta, 10.0, 0.05,
                    "SP500", 0.03)


def test_mark_paused_is_identity_without_pauses():
    import rebalance_plan as plan
    rows = [_row("AAPL", "actionable")]
    assert plan.mark_paused(rows, {}) is rows


def test_paused_rows_are_not_daily_red_alarms():
    import rebalance_plan as plan
    rows = plan.mark_paused(
        [_row("XYZ", "actionable"), _row("AAPL", "aligned", 0), _row("BTC", "manual", 0)],
        {"XYZ": "手動持倉 10 股已進入 SP500 Universe", "BTC": "x"})
    assert [r.category for r in rows] == ["paused", "aligned", "manual"]
    alarms, advisories = plan.drift_alert_lines(rows)
    assert alarms == [] and advisories == []
    lines = plan.drift_report_lines(rows)
    assert "⏸ 手動持倉暫停 1 檔: XYZ+5" in lines
    assert lines[-1] == "✅ 對齊 1 檔"


def test_tg_section_sent_for_new_pause_only():
    import rebalance_ib
    assert rebalance_ib.rebalance_tg_section([], [], order_type="MOO",
                                             dry_run=False) is None
    head, body, level = rebalance_ib.rebalance_tg_section(
        [], [], order_type="MOO", dry_run=False,
        pause_notices=["⏸ XYZ 策略單暫停：手動持倉 10 股 <已進入>"])
    assert head == "沒有送單" and level == "warn"
    assert body == ["⏸ XYZ 策略單暫停：手動持倉 10 股 &lt;已進入&gt;"]


# ── 送單入口：fake IB 走真實流程 ─────────────────────────────────────

class FakeIB:
    """記錄下單／取消；持倉、NAV、報價都是固定值。"""

    def __init__(self, positions, open_trades=()):
        self._positions = positions
        self._open = list(open_trades)
        self.placed: list[tuple[str, str, int]] = []
        self.cancelled: list[int] = []

    def accountValues(self, account):
        return [NS(tag="NetLiquidation", currency="USD", value="100000")]

    def positions(self, account=""):
        from ib_insync import Stock
        return [NS(account=account, contract=Stock(s, "SMART", "USD"),
                   position=q, avgCost=1.0) for s, q in self._positions.items()]

    def openTrades(self):
        return list(self._open)

    def cancelOrder(self, order):
        self.cancelled.append(order.orderId)

    def qualifyContracts(self, *contracts):
        return list(contracts)

    def reqTickers(self, *contracts):
        return [NS(contract=c, bid=99.9, ask=100.1, last=100.0, close=100.0,
                   midpoint=lambda: 100.0) for c in contracts]

    def placeOrder(self, contract, order):
        order.orderId = 1000 + len(self.placed)
        self.placed.append((contract.symbol, order.action, int(order.totalQuantity)))
        return NS(order=order, contract=contract, fills=[],
                  orderStatus=NS(status="Submitted"))

    def sleep(self, *_):
        pass

    def isConnected(self):
        return True

    def disconnect(self):
        pass


MANUAL_OPEN = NS(order=NS(orderId=7, orderRef="QUANTCORE_MANUAL:ROSA:abc"),
                 contract=NS(symbol="BTC"))
STRATEGY_OPEN = NS(order=NS(orderId=8, orderRef=""), contract=NS(symbol="AAPL"))


def _ctx(name="Rosa", dry_run=False):
    from account_ctx import AccountCtx
    return AccountCtx(
        name=name, account="U00000001", host="127.0.0.1", port=4999,
        client_id=99, dry_run=dry_run, order_type="MOO", outside_rth=False,
        wait_fill_sec=0, alloc_sr=0.0, alloc_sp500=0.5, alloc_skewls=0.0,
        price_feed_port=None, tg_token="", tg_chat_id="",
    )


@pytest.fixture
def rebalance_env(paths, monkeypatch):
    """rebalance_ib.main 的真實流程；訊號固定：SP500 想要 AAPL 250 股、XYZ 250 股。"""
    import rebalance_ib
    import signals

    env = NS(sp500_calls=[], ib=None, positions={"AAPL": 100.0, "XYZ": 10.0, "BTC": 1.5})

    def connect(ctx):
        env.ib = FakeIB(env.positions, [MANUAL_OPEN, STRATEGY_OPEN])
        return env.ib

    def sp500_weights(holdings, account_name, ctx):
        env.sp500_calls.append(list(holdings))
        return {"AAPL": 0.5, "XYZ": 0.5}, "2026-09-23"

    monkeypatch.setattr(rebalance_ib, "connect_ib", connect)
    monkeypatch.setattr(rebalance_ib, "get_prices",
                        lambda ib, syms, ctx: {s: 100.0 for s in syms})
    monkeypatch.setattr(rebalance_ib, "get_positions_detail", lambda ib, ctx: [])
    monkeypatch.setattr(rebalance_ib, "append_trade_log", lambda *a, **k: None)
    monkeypatch.setattr(rebalance_ib, "compute_sr_weights", lambda ctx: ({}, "N/A"))
    monkeypatch.setattr(rebalance_ib, "compute_sp500_weights", sp500_weights)
    monkeypatch.setattr(signals, "get_sp500_universe", lambda historical=True: set(JOINED))
    return env


def _run_rebalance(name="Rosa", mode="sp500"):
    import rebalance_ib
    from notify import Digest
    digest = Digest("調倉 T", icon="📤")
    rebalance_ib.main(mode=mode, ctx=_ctx(name), digest=digest)
    return digest


def test_rebalance_holds_only_the_paused_symbol(rebalance_env, caplog):
    # 啟用時 XYZ 還是策略外（手動）；之後 XYZ 進入 SP500 Universe
    ma.evaluate("Rosa", {"AAPL": 100.0, "XYZ": 10.0, "BTC": 1.5}, UNIVERSE)
    caplog.set_level("INFO")
    digest = _run_rebalance()

    assert rebalance_env.ib.placed == [("AAPL", "BUY", 150)]      # XYZ +240 被擋
    assert rebalance_env.ib.cancelled == [8]                       # 手動單 7 沒被取消
    assert rebalance_env.sp500_calls == [["AAPL", "XYZ"]]          # 訊號輸入不變
    text = digest.render()
    assert "⏸ XYZ 策略單暫停：手動持倉 10 股已進入 SP500 Universe" in text
    assert "🔴" not in text
    assert "⏸ 手動持倉暫停 1 檔: XYZ+240" in caplog.text

    # 同一次暫停只通知一次；AAPL 之後已對齊 → 安靜日沒有訊息
    rebalance_env.positions["AAPL"] = 250.0
    assert _run_rebalance().render() is None
    assert rebalance_env.ib.placed == []


def test_rebalance_without_manual_record_is_unchanged(rebalance_env):
    """沒有手動紀錄的帳戶：XYZ 是策略持股，照常送單（啟用當下就在 Universe 內）。"""
    _run_rebalance("Taylor")
    assert rebalance_env.ib.placed == [("AAPL", "BUY", 150), ("XYZ", "BUY", 240)]


def test_rebalance_unreadable_ledger_stops_account_before_state(rebalance_env, paths):
    paths.ledger.write_bytes(b"corrupt" * 100)
    import rebalance_ib
    from notify import Digest
    digest = Digest("調倉 T", icon="📤")
    with pytest.raises(ma.ManualAttributionError):
        rebalance_ib.main(mode="sp500", ctx=_ctx(), digest=digest)
    assert rebalance_env.ib.placed == [] and rebalance_env.sp500_calls == []
    assert digest.outcomes(["Rosa"])["Rosa"][0] == "error"


def test_place_orders_gate_holds_persisted_pause_even_if_caller_forgot(paths):
    import rebalance_ib
    ma.evaluate("Rosa", {"XYZ": 10.0}, UNIVERSE)
    ma.evaluate("Rosa", {"XYZ": 10.0}, JOINED)
    ib = FakeIB({})
    placed, _ = rebalance_ib.place_orders(
        ib, [{"symbol": "XYZ", "action": "SELL", "qty": 10},
             {"symbol": "AAPL", "action": "BUY", "qty": 1}], _ctx())
    assert [o["symbol"] for o in placed] == ["AAPL"]
    assert ib.placed == [("AAPL", "BUY", 1)]


@pytest.fixture
def sync_env(paths, monkeypatch):
    import ib_fetcher
    import signals

    env = NS(ib=None, positions={"AAPL": 100.0, "XYZ": 10.0}, sent=[])

    def connect(ctx, *a, **k):
        env.ib = FakeIB(env.positions, [MANUAL_OPEN, STRATEGY_OPEN])
        return env.ib

    monkeypatch.setattr(ib_fetcher, "connect", connect)
    monkeypatch.setattr(ib_fetcher, "get_prices",
                        lambda ib, syms, ctx, **k: {s: 100.0 for s in syms})
    monkeypatch.setattr(signals, "get_sr_weights", lambda: ({}, "N/A"))
    monkeypatch.setattr(signals, "get_sp500_weights",
                        lambda holdings, name: ({"AAPL": 0.5, "XYZ": 0.5}, "2026-09-23"))
    monkeypatch.setattr(signals, "get_sp500_universe", lambda historical=True: set(JOINED))
    return env


def test_sync_pos_previews_paused_orders_and_never_sends_them(sync_env):
    import sync_position
    ma.evaluate("Rosa", {"AAPL": 100.0, "XYZ": 10.0}, UNIVERSE)
    plan = sync_position.compute_sync_plan(_ctx())
    assert [(o.symbol, o.qty) for o in plan.orders] == [("AAPL", 150)]
    assert [(o.symbol, o.qty) for o in plan.paused_orders] == [("XYZ", 240)]
    assert "手動持倉 10 股" in plan.paused["XYZ"]

    sent = []
    assert sync_position.notify_new_pauses(plan, send=lambda m: sent.append(m) or True)
    assert "XYZ 策略單暫停" in sent[0]
    assert sync_position.compute_sync_plan(_ctx()).pause_notices == []

    placed, _ = sync_position.submit_sync_orders(plan)
    assert [o["symbol"] for o in placed] == ["AAPL"]
    assert sync_env.ib.placed == [("AAPL", "BUY", 150)]
    assert sync_env.ib.cancelled == [8]


_PREVIEW_SCRIPT = r"""
import json, sys, tkinter as tk
sys.path[:0] = sys.argv[1:3]
from account_ctx import AccountCtx
from rebalance_plan import PlannedOrder
from sync_position import SyncPlan
import sync_preview_window as spw

tk.Toplevel.grab_set = lambda self: None
root = tk.Tk(); root.withdraw()
ctx = AccountCtx(name="Rosa", account="U0", host="127.0.0.1", port=1, client_id=1,
                 dry_run=True, order_type="LMT", outside_rth=True, wait_fill_sec=0,
                 alloc_sr=0.0, alloc_sp500=0.5, alloc_skewls=0.0, price_feed_port=None,
                 tg_token="", tg_chat_id="")
plan = SyncPlan(
    ctx=ctx, orders=[PlannedOrder("AAPL", "BUY", 150, 100.0, "SP500", 250, 100)],
    nav=100000.0, sr_wgt={}, sp500_wgt={"AAPL": 0.5, "XYZ": 0.5}, sr_date="N/A",
    sp500_date="D", prices={"AAPL": 100.0, "XYZ": 100.0},
    targets={"AAPL": {"shares": 250, "strategy": "SP500", "alloc_w": 0.25},
             "XYZ": {"shares": 250, "strategy": "SP500", "alloc_w": 0.25}},
    current_pos={"AAPL": 100.0, "XYZ": 10.0}, current_other_holdings=[],
    paused={"XYZ": "手動持倉 10 股已進入 SP500 Universe"},
    paused_orders=[PlannedOrder("XYZ", "BUY", 240, 100.0, "SP500", 250, 10)])
logs = []
theme = dict.fromkeys(("bg", "bg2", "bg3", "accent", "accent_hl", "text",
                       "text_dim", "blue", "green", "red", "btn_bg"), "#000000")
w = spw.SyncPreviewWindow(parent=root, account_name="Rosa", theme=theme,
                          log=logs.append, on_confirm=lambda p: None,
                          btn_factory=lambda parent, text, cmd, *a, **k:
                              tk.Button(parent, text=text, command=cmd))
w.show_computing(); root.update(); w.show_plan(plan); root.update()

def texts(widget):
    for child in widget.winfo_children():
        if isinstance(child, (tk.Label, tk.Button)):
            yield child.cget("text")
        yield from texts(child)

print(json.dumps({"labels": list(texts(w.win)), "logs": logs}, ensure_ascii=False))
root.destroy()
"""


def test_sync_pos_preview_window_shows_pause_reason():
    """Panel 顯示原因：預覽視窗列出暫停標的、原本要下的單與原因；交易清單只剩可送的單。
    Tk 在自己的行程裡跑（同行程第二個 Tk root 不可靠，見 test_panel_gui.own_process）。"""
    import json
    import subprocess
    import sys
    from pathlib import Path

    automation = Path(__file__).resolve().parents[1]
    done = subprocess.run(
        [sys.executable, "-c", _PREVIEW_SCRIPT, str(automation), str(automation.parent)],
        capture_output=True, timeout=120, env={**__import__("os").environ,
                                               "PYTHONIOENCODING": "utf-8"})
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[-2000:]
    out = json.loads(done.stdout.decode("utf-8"))
    labels = out["labels"]
    assert "⏸ 手動持倉暫停（這些標的的策略單不會送出）" in labels
    assert any("XYZ" in t and "原本要 BUY 240sh (10→250)" in t for t in labels)
    assert "    手動持倉 10 股已進入 SP500 Universe" in labels
    assert "Required Trades (需要交易: 1 筆)" in labels
    assert "  ⏸ XYZ 策略單暫停：手動持倉 10 股已進入 SP500 Universe" in out["logs"]


def test_sync_pos_notice_not_marked_when_telegram_fails(sync_env):
    import sync_position
    ma.evaluate("Rosa", {"AAPL": 100.0, "XYZ": 10.0}, UNIVERSE)
    plan = sync_position.compute_sync_plan(_ctx())
    assert not sync_position.notify_new_pauses(plan, send=lambda m: False)
    assert sync_position.compute_sync_plan(_ctx()).pause_notices == plan.pause_notices


class _Tue0915(date):
    @classmethod
    def today(cls):
        return date(2026, 9, 15)


@pytest.fixture
def dip_env(paths, fake_config, monkeypatch, tmp_path):
    """sp500_daily_check 熊市 dip-buy 觸發（SPY 回撤 16%）。"""
    import pandas as pd
    import ib_fetcher
    import signals
    import sp500_daily_check
    import sp500_state
    from strategies.sp500_momentum import utils

    env = NS(ib=None)
    monkeypatch.setattr(sp500_state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(sp500_daily_check, "date", _Tue0915)
    monkeypatch.setattr(utils, "load_benchmark", lambda path: pd.DataFrame(
        {"Close": [750.0, 757.3]}, index=pd.to_datetime(["2026-09-11", "2026-09-14"])))
    monkeypatch.setattr(signals, "get_market_regime_state", lambda ref: {
        "SPY_Close": 500.0, "SPY_MA200": 600.0, "SPY_DD": -0.16})
    monkeypatch.setattr(signals, "get_sp500_universe", lambda historical=True: set(UNIVERSE))

    def connect(ctx, *a, **k):
        env.ib = FakeIB({"AAPL": 10.0}, [MANUAL_OPEN, STRATEGY_OPEN])
        return env.ib

    monkeypatch.setattr(ib_fetcher, "connect", connect)
    monkeypatch.setattr(ib_fetcher, "get_prices",
                        lambda ib, syms, ctx, **k: {s: 100.0 for s in syms})
    monkeypatch.setattr(ib_fetcher, "get_positions_detail", lambda ib, ctx: [])
    env.state_file = tmp_path / "sp500_state_Taylor.json"
    return env


def _daily(name="Taylor"):
    import sp500_daily_check
    from notify import Digest
    d = Digest("日度風控 T", icon="📤")
    sp500_daily_check.run_daily_check(name, d)
    return d


@pytest.fixture
def dip_ctx(monkeypatch):
    import account_ctx
    monkeypatch.setattr(account_ctx.AccountCtx, "from_name",
                        classmethod(lambda cls, name, role: _ctx(name)))


def test_daily_check_dip_buy_keeps_manual_orders(dip_env, dip_ctx):
    from strategies.sp500_momentum import config as sp_cfg
    _daily()
    assert dip_env.ib.cancelled == [8]                            # 手動單 7 沒被取消
    assert [p[0] for p in dip_env.ib.placed] == [sp_cfg.DIP_BUY_TICKER]
    assert dip_env.state_file.exists()


def test_daily_check_unreadable_ledger_keeps_dip_trigger_for_tomorrow(
        dip_env, dip_ctx, paths):
    paths.ledger.write_bytes(b"corrupt" * 100)
    with pytest.raises(ma.ManualAttributionError):
        _daily()
    assert dip_env.ib.placed == [] and dip_env.ib.cancelled == []
    assert not dip_env.state_file.exists()                        # dip 觸發沒被消耗
