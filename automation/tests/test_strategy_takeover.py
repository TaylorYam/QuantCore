"""
單一標的交給策略接管（#107）。

以 fake IB 走 strategy_takeover 的真實流程：預覽（Account、標的、現有股數、
策略整數目標、差額與方向；目標 0 = 賣出現有股數）、取消、未結案手動單與
掛單的拒絕、確認前重算讓舊預覽失效、成功、拒單、部分成交、逾時後重啟再
完成、per-Strategy 取整，以及其他 Account／標的不受影響。Panel 的兩個視窗
在自己的 Tk 行程裡驗證。全部離線：不連 IB、不發 Telegram、不寫 SP500 state。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pandas as pd
import pytest

import manual_attribution as ma
import strategy_takeover as st
from sync_position import SyncRecords

UNIVERSE = {"AAPL", "MSFT", "SPY", "SSO"}
JOINED = UNIVERSE | {"XYZ", "ABC"}          # XYZ、ABC 之後被加入 S&P 500


class FakeIB:
    """持倉、掛單、成交行為可設定；記錄每一筆送單。"""

    def __init__(self, env):
        self.env = env
        self.connected = True

    def accountValues(self, account):
        return [NS(tag="NetLiquidation", currency="USD", value=str(self.env.nav))]

    def positions(self, account=""):
        from ib_insync import Stock
        return [NS(account=account, contract=Stock(s, "SMART", "USD"),
                   position=q, avgCost=1.0) for s, q in self.env.positions.items()]

    def reqAllOpenOrders(self):
        return list(self.env.open_orders)

    def qualifyContracts(self, *contracts):
        return list(contracts)

    def reqTickers(self, *contracts):
        return [NS(contract=c, bid=99.9, ask=100.1, last=100.0, close=100.0,
                   midpoint=lambda: 100.0) for c in contracts]

    def placeOrder(self, contract, order):
        behavior = self.env.behavior
        if behavior == "raise":
            raise ConnectionError("socket closed")
        self.env.placed.append(NS(symbol=contract.symbol, action=order.action,
                                  qty=int(order.totalQuantity), lmt=order.lmtPrice,
                                  ref=order.orderRef, outside_rth=order.outsideRth,
                                  account=order.account))
        trade = NS(order=order, contract=contract, log=[],
                   orderStatus=NS(status="PreSubmitted", filled=0))
        self.env.trade = trade
        return trade

    def sleep(self, *_):
        trade, env = self.env.trade, self.env
        if trade is None or trade.orderStatus.status != "PreSubmitted":
            return
        sym, qty = trade.contract.symbol, int(trade.order.totalQuantity)
        sign = 1 if trade.order.action == "BUY" else -1
        if env.behavior == "fill":
            trade.orderStatus.status, trade.orderStatus.filled = "Filled", qty
            env.positions[sym] = env.positions.get(sym, 0.0) + sign * qty
        elif env.behavior == "reject":
            trade.orderStatus.status = "Inactive"
            trade.log.append(NS(message="Order rejected - reason: margin"))
        elif env.behavior == "partial":
            trade.orderStatus.status, trade.orderStatus.filled = "Submitted", 100
            env.positions[sym] = env.positions.get(sym, 0.0) + sign * 100
        elif env.behavior == "working":
            trade.orderStatus.status = "Submitted"

    def isConnected(self):
        return self.connected

    def disconnect(self):
        self.connected = False


def _ctx(name="Rosa", dry_run=False, alloc=(0.0, 0.5, 0.0)):
    from account_ctx import AccountCtx
    return AccountCtx(
        name=name, account={"Rosa": "U2", "Taylor": "U1"}[name], host="127.0.0.1",
        port=4999, client_id=98, dry_run=dry_run, order_type="MOO",
        outside_rth=False, wait_fill_sec=3, alloc_sr=alloc[0], alloc_sp500=alloc[1],
        alloc_skewls=alloc[2], price_feed_port=None, tg_token="T", tg_chat_id="C",
    )


def _manual_order(path, ref, account, symbol, action, filled, status, quantity=10):
    from manual_orders import ManualOrderStore
    ManualOrderStore(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO manual_orders (
                   order_ref, account_name, account_id, symbol, action, quantity,
                   limit_price, tif, outside_rth, contract_id, status,
                   status_message, filled_quantity, created_at, updated_at)
               VALUES (?, ?, 'U0', ?, ?, ?, 10, 'DAY', 0, 1, ?, '', ?, 't', 't')""",
            (ref, account, symbol, action, quantity, status, filled))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Rosa 手動持有 XYZ 10 股、ABC 5 股，兩檔之後都進了 SP500 Universe。"""
    import ib_fetcher
    import signals
    import sp500_state
    from strategies.sp500_momentum import signal as sp_signal

    env = NS(nav=100_000.0, positions={"AAPL": 100.0, "XYZ": 10.0, "ABC": 5.0},
             open_orders=[], placed=[], trade=None, behavior="fill",
             sp500={"AAPL": 0.5, "XYZ": 0.5}, sr={}, skewls={},
             state_dir=tmp_path / "state", orders=tmp_path / "manual_orders.sqlite3")
    env.state_dir.mkdir()
    monkeypatch.setattr(ma, "DEFAULT_LEDGER_PATH", tmp_path / "ledger.sqlite3")
    monkeypatch.setattr(ma, "DEFAULT_MANUAL_ORDERS_PATH", env.orders)
    monkeypatch.setattr(sp500_state, "STATE_DIR", env.state_dir)
    def no_default(*_a, **_k):
        raise AssertionError("接管不該走 ib_fetcher.connect 的預設連線")

    monkeypatch.setattr(ib_fetcher, "connect", no_default)
    # 實盤送單目標給的連線與紀錄（#183：沒有預設，一律明確給）
    env.records = SyncRecords(connect=lambda ctx: FakeIB(env),
                              attribution_ledger=tmp_path / "ledger.sqlite3",
                              manual_orders_store=env.orders, persist_strategy_state=True)
    monkeypatch.setattr(ib_fetcher, "get_prices",
                        lambda ib, syms, ctx, **k: {s: 100.0 for s in syms})
    monkeypatch.setattr(signals, "get_sp500_universe", lambda historical=True: set(JOINED))
    monkeypatch.setattr(signals, "get_sr_weights", lambda: (dict(env.sr), "2026-09-28"))
    monkeypatch.setattr(signals, "get_skewls_weights",
                        lambda: (dict(env.skewls), "2026-09-28"))

    def live(holdings, state, as_of_date=None):
        env.sp500_holdings = list(holdings)
        return dict(env.sp500), pd.Timestamp("2026-09-22"), dict(state, touched=True)

    monkeypatch.setattr(sp_signal, "get_latest_weights_live", live)
    # 啟用時 XYZ、ABC 還是策略外 = 手動
    ma.evaluate("Rosa", dict(env.positions), UNIVERSE)
    ma.evaluate("Rosa", dict(env.positions), JOINED)
    return env


# ── 預覽 ─────────────────────────────────────────────────────────────

def test_preview_shows_account_symbol_target_delta_and_direction(env):
    p = st.build_preview(_ctx(), "XYZ", records=env.records)
    assert (p.account_name, p.symbol, p.current, p.manual) == ("Rosa", "XYZ", 10.0, 10.0)
    assert (p.target, p.parts, p.delta, p.action, p.quantity) == (
        250, (("SP500", 250),), 240, "BUY", 240)
    text = "\n".join(p.lines())
    assert "IB 現有股數：10" in text and "策略整數目標：250 股（SP500 250）" in text
    assert "差額：+240 股 — 買進 240 股" in text
    assert env.sp500_holdings == ["AAPL", "XYZ", "ABC"]      # 與 Sync Pos 同一份持股輸入
    assert list(env.state_dir.iterdir()) == []               # 不寫 SP500 state


def test_zero_target_means_selling_the_existing_shares(env):
    env.sp500 = {"AAPL": 1.0}
    p = st.build_preview(_ctx(), "XYZ", records=env.records)
    assert (p.target, p.delta, p.action, p.quantity) == (0, -10, "SELL", 10)
    assert "策略目標為 0，將賣出現有 10 股" in "\n".join(p.lines())


def test_cancelled_preview_sends_nothing_and_keeps_the_pause(env):
    st.build_preview(_ctx(), "XYZ", records=env.records)     # 使用者按 Cancel
    assert env.placed == []
    assert set(ma.paused_symbols("Rosa")) == {"XYZ", "ABC"}
    assert ma.pending_takeover("Rosa", "XYZ") is None


def test_unresolved_manual_order_refuses_takeover(env):
    _manual_order(env.orders, "M:1", "Rosa", "XYZ", "SELL", 0, "ACCEPTED")
    with pytest.raises(st.TakeoverRefused, match="未結案"):
        st.build_preview(_ctx(), "XYZ", records=env.records)


def test_open_order_on_the_symbol_refuses_takeover(env):
    tws = NS(order=NS(account="U2", action="SELL", totalQuantity=3),
             contract=NS(symbol="XYZ"), orderStatus=NS(status="Submitted"))
    other = NS(order=NS(account="U2", action="BUY", totalQuantity=1),
               contract=NS(symbol="AAPL"), orderStatus=NS(status="Submitted"))
    env.open_orders = [other]
    st.build_preview(_ctx(), "XYZ", records=env.records)     # 別的標的的掛單不擋
    env.open_orders = [tws, other]
    with pytest.raises(st.TakeoverRefused, match="1 筆掛單（SELL 3）"):
        st.build_preview(_ctx(), "XYZ", records=env.records)


def test_symbol_that_is_not_paused_cannot_be_taken_over(env):
    with pytest.raises(st.TakeoverRefused, match="沒有被手動持倉暫停"):
        st.build_preview(_ctx(), "AAPL", records=env.records)


def test_per_strategy_rounding_is_kept(env):
    """同一檔被 SR 與 Skew LS 同時持有：各自取整後相加（ADR-0008），不是合權重再取整。"""
    _manual_order(env.orders, "M:1", "Rosa", "XLK", "BUY", 5, "FILLED", 5)
    env.positions["XLK"] = 5.0
    env.sr = {"XLK": 0.003625}          # 100000 × 0.8 × w / 100 = 2.9  → int  → 2
    env.skewls = {"XLK": 0.0046667}     # 100000 × 0.3 × w / 100 ≈ 1.4  → round → 1
    p = st.build_preview(_ctx(alloc=(0.8, 0.5, 0.3)), "XLK", records=env.records)
    assert p.parts == (("SR", 2), ("SkewLS", 1)) and p.target == 3   # 合權重 4.3 → 4


# ── 送單 ─────────────────────────────────────────────────────────────

def test_changed_position_invalidates_the_old_preview(env):
    p = st.build_preview(_ctx(), "XYZ", records=env.records)
    env.positions["XYZ"] = 12.0                               # 預覽後在 App 又買 2 股
    with pytest.raises(st.TakeoverStale) as info:
        st.submit(p, _ctx(), records=env.records)
    assert info.value.fresh.current == 12.0 and info.value.fresh.delta == 238
    assert env.placed == [] and ma.pending_takeover("Rosa", "XYZ") is None


def test_success_sends_only_that_symbol_and_releases_only_its_pause(env):
    ma.evaluate("Taylor", {"XYZ": 7.0}, UNIVERSE)
    ma.evaluate("Taylor", {"XYZ": 7.0}, JOINED)

    result = st.submit(st.build_preview(_ctx(), "XYZ", records=env.records), _ctx(),
                       records=env.records)

    assert result.status == "COMPLETED", result.message
    [o] = env.placed
    assert (o.symbol, o.action, o.qty, o.lmt, o.outside_rth, o.account) == (
        "XYZ", "BUY", 240, round(100.1 * 1.003, 2), True, "U2")
    assert o.ref.startswith("QUANTCORE_TAKEOVER:ROSA:")
    assert set(ma.paused_symbols("Rosa")) == {"ABC"}           # 只解除 XYZ
    assert set(ma.paused_symbols("Taylor")) == {"XYZ"}         # 其他 Account 不受影響
    assert ma.gate_orders("Rosa", [{"symbol": "XYZ"}])[1] == []
    assert list(env.state_dir.iterdir()) == []

    sent = []
    assert st.notify(_ctx(), send=lambda m: sent.append(m) or True, records=env.records)
    assert "✅ XYZ 已交給策略接管" in sent[0]
    assert not st.notify(_ctx(), send=lambda m: sent.append(m) or True, records=env.records)   # 只通知一次

    # 之後的觀察：XYZ 全歸策略，不再暫停
    after = ma.evaluate("Rosa", dict(env.positions), JOINED)
    assert "XYZ" not in after.paused and "XYZ" not in after.manual


def test_preview_and_submit_without_records_refuse_before_connecting(env):
    """#183：沒有「預設實盤」— 忘了給連線與紀錄就報錯，不會不知情地繞過送單閘門。"""
    with pytest.raises(TypeError):
        st.build_preview(_ctx(), "XYZ")
    with pytest.raises(TypeError, match="SyncRecords"):
        st.build_preview(_ctx(), "XYZ", records=None)
    p = st.build_preview(_ctx(), "XYZ", records=env.records)
    with pytest.raises(TypeError):
        st.submit(p, _ctx())
    with pytest.raises(TypeError, match="SyncRecords"):
        st.submit(p, _ctx(), records=None)
    with pytest.raises(TypeError):
        st.notify(_ctx(), lambda m: True)
    assert env.placed == [] and ma.pending_takeover("Rosa", "XYZ") is None


def test_zero_target_success_sells_everything(env):
    env.sp500 = {"AAPL": 1.0}
    result = st.submit(st.build_preview(_ctx(), "XYZ", records=env.records), _ctx(),
                       records=env.records)
    assert result.status == "COMPLETED"
    assert [(o.action, o.qty) for o in env.placed] == [("SELL", 10)]
    assert env.positions["XYZ"] == 0


def test_no_difference_hands_over_without_an_order(env):
    env.sp500 = {"AAPL": 0.98, "XYZ": 0.02}                  # 100000 × 0.5 × 0.02 / 100 = 10
    result = st.submit(st.build_preview(_ctx(), "XYZ", records=env.records), _ctx(),
                       records=env.records)
    assert result.status == "COMPLETED" and env.placed == []
    assert "XYZ" not in ma.paused_symbols("Rosa")


def test_rejected_order_keeps_the_pause(env):
    env.behavior = "reject"
    result = st.submit(st.build_preview(_ctx(), "XYZ", records=env.records), _ctx(),
                       records=env.records)
    assert result.status == "FAILED" and "IB 拒單（Order rejected - reason: margin）" in result.message
    assert "XYZ" in ma.paused_symbols("Rosa")
    assert ma.pending_takeover("Rosa", "XYZ") is None
    lines = [n.line for n in ma.pending_notices("Rosa")]
    assert any(line.startswith("❌ XYZ 接管未完成：IB 拒單") for line in lines)


def test_partial_fill_stays_paused_and_completes_after_restart(env):
    env.behavior = "partial"
    result = st.submit(st.build_preview(_ctx(), "XYZ", records=env.records), _ctx(),
                       records=env.records)
    assert result.status == "PENDING"
    reason = ma.paused_symbols("Rosa")["XYZ"]
    assert reason.startswith("接管中：已送 BUY 240 股（接管後預計 250 股）")
    assert ma.gate_orders("Rosa", [{"symbol": "XYZ"}])[0] == []   # 策略單仍被擋

    # Panel 關掉、隔天 21:00 調倉時剩下的 140 股已成交 — 新行程只剩紀錄檔
    env.positions["XYZ"] = 250.0
    later = ma.evaluate("Rosa", dict(env.positions), JOINED)
    assert later.completed == ["XYZ"] and "XYZ" not in later.paused
    assert any(n.line.startswith("✅ XYZ 已交給策略接管") for n in later.new_notices)


def test_working_order_times_out_as_pending(env):
    env.behavior = "working"
    result = st.submit(st.build_preview(_ctx(), "XYZ", records=env.records), _ctx(),
                       records=env.records)
    assert result.status == "PENDING" and "維持暫停" in result.message
    assert ma.pending_takeover("Rosa", "XYZ").expected_after == 250.0


def test_unknown_send_result_keeps_the_record(env):
    env.behavior = "raise"
    result = st.submit(st.build_preview(_ctx(), "XYZ", records=env.records), _ctx(),
                       records=env.records)
    assert result.status == "PENDING" and "送單結果不確定" in result.message
    assert ma.pending_takeover("Rosa", "XYZ") is not None and "XYZ" in ma.paused_symbols("Rosa")


def test_dry_run_changes_nothing(env):
    result = st.submit(st.build_preview(_ctx(dry_run=True), "XYZ", records=env.records),
                       _ctx(dry_run=True), records=env.records)
    assert result.status == "DRY_RUN" and env.placed == []
    assert ma.pending_takeover("Rosa", "XYZ") is None and "XYZ" in ma.paused_symbols("Rosa")


def test_other_records_keep_the_live_ledger_untouched(env, tmp_path, monkeypatch):
    """新 Panel 的 paper 接管（#153）：連線、歸屬紀錄、手動單紀錄都用傳進來的那一組。"""
    import ib_fetcher

    def no_live(*_a, **_k):
        raise AssertionError("不該連實盤")

    monkeypatch.setattr(ib_fetcher, "connect", no_live)
    live_ledger = tmp_path / "ledger.sqlite3"
    live_before = live_ledger.read_bytes()
    paper = NS(ledger=tmp_path / "paper_ledger.sqlite3",
               orders=tmp_path / "paper_orders.sqlite3", connects=[])
    _manual_order(paper.orders, "M:9", "Rosa", "ABC", "BUY", 0, "ACCEPTED")

    def connect(ctx):
        paper.connects.append(ctx.client_id)
        return FakeIB(env)

    records = SyncRecords(connect=connect, attribution_ledger=paper.ledger,
                          manual_orders_store=paper.orders, persist_strategy_state=False)
    ma.evaluate("Rosa", dict(env.positions), UNIVERSE, ledger_path=paper.ledger,
                manual_orders_path=paper.orders)
    ma.evaluate("Rosa", dict(env.positions), JOINED, ledger_path=paper.ledger,
                manual_orders_path=paper.orders)
    with pytest.raises(st.TakeoverRefused, match="未結案"):     # 讀 paper 的手動單紀錄
        st.build_preview(_ctx(), "ABC", records=records)

    env.behavior = "reject"
    result = st.submit(st.build_preview(_ctx(), "XYZ", records=records), _ctx(),
                       records=records)
    assert result.status == "FAILED"
    assert "XYZ" in ma.paused_symbols("Rosa", path=paper.ledger)
    assert [n.line for n in ma.pending_notices("Rosa", path=paper.ledger)
            if n.key.startswith("event:")] == [
        "❌ XYZ 接管未完成：IB 拒單（Order rejected - reason: margin）；維持暫停"]

    env.behavior = "fill"
    result = st.submit(st.build_preview(_ctx(), "XYZ", records=records), _ctx(),
                       records=records)
    assert result.status == "COMPLETED", result.message
    assert "XYZ" not in ma.paused_symbols("Rosa", path=paper.ledger)
    assert paper.connects == [98] * 5
    assert live_ledger.read_bytes() == live_before              # 實盤紀錄一個 byte 都沒動
    assert ma.pending_takeover("Rosa", "XYZ") is None


def test_takeover_orders_survive_strategy_cancel_all():
    takeover = NS(order=NS(orderRef="QUANTCORE_TAKEOVER:ROSA:abc"))
    strategy = NS(order=NS(orderRef=""))
    assert ma.strategy_open_trades([takeover, strategy]) == [strategy]


# ── Panel（各自的 Tk 行程；同行程第二個 Tk root 不可靠，見 test_panel_gui）─

_GUI_SCRIPT = r"""
import json, sys, tkinter as tk
from tkinter import messagebox
sys.path[:0] = sys.argv[1:3]
from account_ctx import AccountCtx
from rebalance_plan import PlannedOrder
from strategy_takeover import TakeoverPreview, TakeoverResult
from sync_position import SyncPlan
import sync_preview_window as spw
import takeover_window as tw

tk.Toplevel.grab_set = lambda self: None
messagebox.askyesno = lambda *a, **k: True
root = tk.Tk(); root.withdraw()
theme = dict.fromkeys(("bg", "bg2", "bg3", "accent", "accent_hl", "text",
                       "text_dim", "blue", "green", "red", "btn_bg"), "#000000")
btn = lambda parent, text, cmd, *a, **k: tk.Button(parent, text=text, command=cmd)

def texts(widget):
    for child in widget.winfo_children():
        if isinstance(child, (tk.Label, tk.Button)):
            yield child
        yield from texts(child)

out = {}
# 1. Sync Pos 預覽的暫停區塊 → 「交給策略接管」
ctx = AccountCtx(name="Rosa", account="U2", host="127.0.0.1", port=1, client_id=1,
                 dry_run=True, order_type="LMT", outside_rth=True, wait_fill_sec=0,
                 alloc_sr=0.0, alloc_sp500=0.5, alloc_skewls=0.0, price_feed_port=None,
                 tg_token="", tg_chat_id="")
plan = SyncPlan(ctx=ctx, orders=[], nav=100000.0, sr_wgt={}, sp500_wgt={}, sr_date="N/A",
                sp500_date="D", prices={"XYZ": 100.0}, targets={}, current_pos={"XYZ": 10.0},
                current_other_holdings=[], paused={"XYZ": "手動持倉 10 股已進入 SP500 Universe"},
                paused_orders=[PlannedOrder("XYZ", "BUY", 240, 100.0, "SP500", 250, 10)])
picked = []
sync = spw.SyncPreviewWindow(parent=root, account_name="Rosa", theme=theme, log=lambda m: None,
                             btn_factory=btn, on_confirm=lambda p: None,
                             on_takeover=picked.append)
sync.show_computing(); root.update(); sync.show_plan(plan); root.update()
[button] = [w for w in texts(sync.win) if w.cget("text") == "交給策略接管"]
button.invoke(); root.update()
out["picked"] = picked
out["sync_closed"] = not sync._alive()

# 2. 接管視窗：預覽 → 確認 → 結果
preview = TakeoverPreview(account_name="Rosa", symbol="XYZ", current=10.0, manual=10.0,
                          target=0, parts=(("SP500", 0),),
                          pause_reason="手動持倉 10 股已進入 SP500 Universe",
                          price=100.0, nav=100000.0, signal_dates="SR N/A｜SP500 2026-09-22")
confirmed = []
win = tw.TakeoverWindow(parent=root, account_name="Rosa", symbol="XYZ", theme=theme,
                        log=lambda m: None, btn_factory=btn, on_confirm=confirmed.append)
win.show_computing(); win.progress("  Connecting to IB (Rosa)..."); root.update()
win.show_preview(preview); root.update()
out["preview"] = [w.cget("text") for w in texts(win.win)]
[confirm] = [w for w in texts(win.win) if w.cget("text").startswith("確認接管")]
confirm.invoke(); root.update()
out["confirmed"] = [p.symbol for p in confirmed]
win.show_result(TakeoverResult("COMPLETED", "接管完成：IB 0 股，XYZ 恢復策略送單", preview))
root.update()
out["result"] = [w.cget("text") for w in texts(win.win)]
print(json.dumps(out, ensure_ascii=False))
root.destroy()
"""


def test_panel_takeover_button_preview_and_result():
    """Sync Pos 暫停區塊的「交給策略接管」開接管流程；預覽寫明目標 0 要賣出現有股數，
    確認後交給 Panel 送單，結果顯示在同一個視窗。"""
    automation = Path(__file__).resolve().parents[1]
    done = subprocess.run(
        [sys.executable, "-c", _GUI_SCRIPT, str(automation), str(automation.parent)],
        capture_output=True, timeout=120,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[-2000:]
    out = json.loads(done.stdout.decode("utf-8"))
    assert out["picked"] == ["XYZ"] and out["sync_closed"]
    assert "差額：-10 股 — 策略目標為 0，將賣出現有 10 股" in out["preview"]
    assert "確認接管並送出 SELL 10" in out["preview"]
    assert out["confirmed"] == ["XYZ"]
    assert "✅ 接管完成" in out["result"]
    assert "接管完成：IB 0 股，XYZ 恢復策略送單" in out["result"]
