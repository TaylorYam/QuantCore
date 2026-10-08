"""Panel 的送單目標（#183）：手動單、Sync Pos、交給策略接管送到實盤帳戶。

只從送單目標的 interface 操作（手動單 service、Sync Pos 的計算／通知／送出、接管的
暫停／預覽／送出／通知），跑真的 manual_orders、sync_position、
strategy_takeover、manual_attribution；IB 換成 tests/fake_broker.py 的 in-memory
Broker（手動單用 ib_insync 本體，改接真的 broker.Broker＋test_manual_orders 的假 IB）、
訊號換成固定值、紀錄檔放 tmp_path、Telegram 用假的。不需要 QApplication。

斷言看得到的結果：連到哪個 host／port／clientId、哪份紀錄檔多了資料、state 有沒有
寫、假 Telegram 收到什麼、Output Log 回傳哪些行、trade_log 有沒有寫。Qt 畫面流程在 test_qt_sync_pos／test_qt_takeover／test_qt_trading。
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from tests.fake_broker import FakeBroker
from tests.test_manual_orders import FakeIB, FakeIBState, fake_ib_connect

from account_utils import ROLE_MANUAL_ORDER, ROLE_REBALANCE, ROLE_TAKEOVER
from broker import Broker
from manual_orders import ManualOrderService, ManualOrderStore
from qt_panel.order_target import OrderFiles, OrderTarget

UNIVERSE = {"AAPL", "MSFT", "SPY", "SSO"}
JOINED = UNIVERSE | {"XYZ"}          # XYZ 後來被加入 S&P 500
NAMES = ["Taylor", "Rosa", "Yvonne"]
# 2026-09-29（週二）美東 14:00，一般時段；最近已收盤的交易日是 09-28
NOW = datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc)
SKEWLS_ALLOC = {"Taylor": 0.0, "Rosa": 0.2, "Yvonne": 0.0}
LIVE_DEST = {"token": "LIVE-TOKEN", "chat_id": "LIVE-CHAT"}


def order_factory(action, quantity, price):
    return NS(action=action, totalQuantity=quantity, lmtPrice=price, orderType="LMT",
              tif="DAY", outsideRth=False, account="", orderRef="", orderId=0, permId=0,
              cashQty="UNSET")


def base_ctx(name, role):
    """帳戶的設定（AccountCtx.from_name 的替身）：實盤 Gateway、clientId = 20 + role。"""
    from account_ctx import AccountCtx
    return AccountCtx(
        name=name, account={"Taylor": "U001", "Rosa": "U002", "Yvonne": "U003"}[name],
        host="127.0.0.1", port={"Taylor": 4001, "Rosa": 4002, "Yvonne": 4003}[name],
        client_id=20 + role, dry_run=True, order_type="MOO", outside_rth=False,
        wait_fill_sec=0, alloc_sr=0.0, alloc_sp500=0.5, alloc_skewls=SKEWLS_ALLOC[name],
        price_feed_port=4002, tg_token="LIVE-TOKEN", tg_chat_id="LIVE-CHAT",
    )


def live_ctx(name, role):
    return replace(base_ctx(name, role), dry_run=False)


def sent(broker):
    """送出的單：(標的, 方向, 股數, 單別, 帳號)。"""
    return [(p.symbol, p.action, p.qty, p.order_type, p.account) for p in broker.placed]


@pytest.fixture
def broker():
    """in-memory Broker；persist／gate／sp500_states 是這裡的訊號替身記錄用的。"""
    fake = FakeBroker(managed=["U001"], positions={"AAPL": 100.0, "XYZ": 10.0})
    fake.persist, fake.gate, fake.sp500_states = [], None, []
    return fake


@pytest.fixture
def live_files(tmp_path):
    return OrderFiles(orders=tmp_path / "live_orders.sqlite3",
                      attribution=tmp_path / "live_attribution.sqlite3")


@pytest.fixture
def sync_env(broker, live_files, monkeypatch):
    """固定訊號；IB 讀數來自 in-memory Broker；sync_position、rebalance_plan、
    manual_attribution 用真的。
    manual_attribution 的預設檔也指到 tmp 的實盤檔：萬一有路徑沒指定，寫進去的資料
    會被下面「實盤紀錄沒被建立」的斷言抓到，而且碰不到真的檔案。"""
    import manual_attribution as ma
    import signals

    monkeypatch.setattr(ma, "DEFAULT_LEDGER_PATH", live_files.attribution)
    monkeypatch.setattr(ma, "DEFAULT_MANUAL_ORDERS_PATH", live_files.orders)

    def sr_weights():
        if broker.gate is not None:
            broker.gate.wait(5)
        return {}, "N/A"

    def sp500_weights(holdings, name, persist=True):
        broker.persist.append(persist)
        return {"AAPL": 0.5, "XYZ": 0.5}, "2026-09-29"

    monkeypatch.setattr(signals, "get_sr_weights", sr_weights)
    monkeypatch.setattr(signals, "get_sp500_weights", sp500_weights)
    monkeypatch.setattr(signals, "get_sp500_universe", lambda historical=False: set(JOINED))
    return live_files


@pytest.fixture
def takeover_signals(broker, sync_env, monkeypatch):
    """接管的 SP500 live 權重（不寫 state）；記下任何 SP500 state 寫入。"""
    import sp500_state
    from strategies.sp500_momentum import signal as sp_signal

    def live(holdings, state, as_of_date=None):
        if broker.gate is not None:
            broker.gate.wait(5)
        return {"AAPL": 0.5, "XYZ": 0.5}, pd.Timestamp("2026-09-29"), dict(state)

    monkeypatch.setattr(sp_signal, "get_latest_weights_live", live)
    monkeypatch.setattr(sp500_state, "load_state", lambda name: {})
    monkeypatch.setattr(sp500_state, "save_state",
                        lambda *a, **k: broker.sp500_states.append(a))


def pause_xyz(files: OrderFiles, account="Taylor") -> None:
    """帳戶先有策略外的 XYZ 10 股，之後 XYZ 被加入 S&P 500 → 暫停（記在 files 這份）。"""
    import manual_attribution as ma

    for universe in (UNIVERSE, JOINED):
        ma.evaluate(account, {"AAPL": 100.0, "XYZ": 10.0}, universe,
                    ledger_path=files.attribution, manual_orders_path=files.orders)


@pytest.fixture
def telegram(monkeypatch):
    """假 Telegram：記下 (訊息, 目的地)；ok=False 模擬送不出去。"""
    import account_utils
    import notify

    sent = NS(messages=[], ok=True)

    def send(msg, **dest):
        sent.messages.append((msg, dest))
        return sent.ok

    monkeypatch.setattr(notify, "tg_send", send)
    monkeypatch.setattr(account_utils, "telegram_dest", lambda name=None: dict(LIVE_DEST))
    return sent


@pytest.fixture
def trade_log(monkeypatch):
    import excel_logger
    rows = []
    monkeypatch.setattr(excel_logger, "append_trade_log", lambda *a, **k: rows.append(k))
    return rows


def make_target(broker, *, live_files):
    return OrderTarget(files=live_files, account_ctx_loader=live_ctx,
                       broker=broker)


@pytest.fixture
def live(broker, live_files):
    return make_target(broker, live_files=live_files)


# ── 送單目標 ─────────────────────────────────────────────────────────

def test_live_files_default_to_the_attribution_defaults_at_build_time():
    """沒給紀錄檔時，實盤用 manual_attribution 當下的預設路徑（conftest 指到暫存目錄），
    不在 import 時就固定成 live 目錄的檔案。"""
    import manual_attribution as ma

    target = OrderTarget(account_ctx_loader=live_ctx, broker=FakeBroker())
    assert target.text == "實盤帳戶（各帳戶自己的 Gateway）"
    assert target.files == OrderFiles(orders=ma.DEFAULT_MANUAL_ORDERS_PATH,
                                      attribution=ma.DEFAULT_LEDGER_PATH)


# ── 手動單 ───────────────────────────────────────────────────────────

def _service_factory(**kw):
    return ManualOrderService(account_names=NAMES, universe_loader=lambda: {"SPY", "SSO"},
                              order_factory=order_factory, clock=lambda: NOW, **kw)


def _ib(account):
    state = FakeIBState()
    state.managed = [account]
    state.positions_by_account = {account: {}}
    state.net_liquidation = {account: "20000"}
    state.daily_bars = [(date(2026, 9, 28), 100.0)]
    return state


def _cancel_and_close(service, state):
    """送出一張單、撤單，IB 回報已取消後對帳一次：結案通知就在這時發。"""
    order = service.submit(service.preview("Taylor", "AAPL", 5, 100.0))
    [trade] = state.placed[-1:]
    state.cancel_status = "Cancelled"
    service.cancel(order["order_ref"])
    state.open_trades.remove(trade)
    state.completed_trades.append(trade)
    service.refresh_status()
    return trade


def test_live_manual_orders_keep_the_identity_store_and_telegram(
        live_files, telegram):
    ib = _ib("U001")
    # 先前（例如切換前的 Tk Panel）送出的單：同一個實盤紀錄檔、同一個 clientId role
    tk = _service_factory(store=ManualOrderStore(live_files.orders),
                          connect=fake_ib_connect(ib), account_ctx_factory=live_ctx,
                          notifier=lambda *a: True)
    tk_order = tk.submit(tk.preview("Taylor", "ZZZZ", 4, 100.0))
    tk_trade = ib.open_trades.pop()

    target = make_target(Broker(lambda: FakeIB(ib)), live_files=live_files)
    service = target.manual_order_service(NAMES, _service_factory)
    assert service.store.path == live_files.orders
    trade = _cancel_and_close(service, ib)
    assert (trade.order.account, trade.order.totalQuantity) == ("U001", 5)
    assert set(ib.client_ids) == {20 + ROLE_MANUAL_ORDER}      # 同一個手動單 clientId
    # 結案通知（還有先前那張單暫時查不到的提醒）都發該帳戶 Telegram
    assert {dest["token"] for _msg, dest in telegram.messages} == {LIVE_DEST["token"]}
    assert any("AAPL" in msg and "已取消" in msg for msg, _dest in telegram.messages)

    # 先前那張單後來成交：同一個 clientId 認得它
    tk_trade.orderStatus.status = "Filled"
    tk_trade.orderStatus.filled = 4
    tk_trade.orderStatus.avgFillPrice = 99.0
    ib.completed_trades.append(tk_trade)
    ib.fills.append(NS(contract=tk_trade.contract, execution=NS(
        acctNumber="U001", permId=tk_trade.order.permId, orderId=tk_trade.order.orderId,
        clientId=20 + ROLE_MANUAL_ORDER, shares=4.0, price=99.0, execId="tk-fill",
        orderRef=tk_order["order_ref"])))
    service.refresh_status()
    row = next(r for r in service.list_orders() if r["order_ref"] == tk_order["order_ref"])
    assert (row["status"], row["filled_quantity"]) == ("FILLED", 4)


# ── Sync Pos ─────────────────────────────────────────────────────────

def test_live_sync_uses_the_rebalance_identity_and_reports(
        live, broker, sync_env, live_files, telegram, trade_log):
    pause_xyz(live_files)
    broker.positions = {"AAPL": 100.0, "XYZ": 10.0}
    sync = live.sync()
    assert (sync.venue, sync.target_text) == ("實盤", "實盤帳戶（各帳戶自己的 Gateway）")
    assert "Telegram" in sync.helper_text and "trade_log.xlsx" in sync.helper_text

    plan = sync.compute("Taylor", lambda m: None)
    [ctx] = broker.ctxs
    assert (ctx.account, ctx.port, ctx.client_id) == ("U001", 4001, 20 + ROLE_REBALANCE)
    assert broker.persist == [True]                     # 實盤 SP500 state 寫回

    telegram.ok = False                                 # 暫停通知沒送到：不記已通知
    assert sync.take_notices(plan) == [
        "⏸ XYZ 策略單暫停：手動持倉 10 股已進入 SP500 Universe"]
    plan = sync.compute("Taylor", lambda m: None)
    assert plan.pause_notices != []
    telegram.ok = True
    sync.take_notices(plan)
    assert [dest for _m, dest in telegram.messages] == [LIVE_DEST, LIVE_DEST]
    assert "XYZ 策略單暫停" in telegram.messages[-1][0]
    assert sync.compute("Taylor", lambda m: None).pause_notices == []   # 送達才記已通知

    telegram.messages.clear()
    placed, _fills = sync.submit(plan, lambda m: None)
    assert sent(broker) == [("AAPL", "BUY", 150, "LMT", "U001")]
    assert [r["account_name"] for r in trade_log] == ["Taylor"]
    [(msg, dest)] = telegram.messages
    assert "[Taylor] Sync Position — LMT" in msg and dest == LIVE_DEST


def test_live_sync_report_failures_only_reach_the_output_log(
        live, broker, sync_env, telegram, monkeypatch):
    import excel_logger
    import notify

    def broken(*_a, **_k):
        raise OSError("檔案被 Excel 鎖住")

    monkeypatch.setattr(excel_logger, "append_trade_log", broken)
    monkeypatch.setattr(notify, "tg_send", broken)
    broker.positions = {"AAPL": 100.0}
    sync = live.sync()
    plan = sync.compute("Taylor", lambda m: None)
    lines = []
    placed, _fills = sync.submit(plan, lines.append)     # 單已送出：不當成送單失敗
    assert [p["symbol"] for p in placed] == ["AAPL", "XYZ"]
    assert "[!] Sync Pos trade_log.xlsx 寫入失敗：檔案被 Excel 鎖住" in lines
    assert "[!] Sync Pos Telegram 通知失敗：檔案被 Excel 鎖住" in lines


# ── 交給策略接管 ─────────────────────────────────────────────────────

def test_live_takeover_uses_the_takeover_identity_and_telegram(
        live, broker, takeover_signals, live_files, telegram):
    import manual_attribution as ma

    pause_xyz(live_files)
    takeover = live.takeover()
    assert (takeover.venue, takeover.target_text) == ("實盤", "實盤帳戶（各帳戶自己的 Gateway）")
    assert set(takeover.paused("Taylor")) == {"XYZ"} and takeover.paused("Rosa") == {}

    preview = takeover.preview("Taylor", "XYZ", lambda m: None)
    assert (broker.ctxs[0].account, broker.ctxs[0].client_id) == ("U001", 20 + ROLE_TAKEOVER)
    result = takeover.submit(preview, lambda m: None)
    assert result.status == "COMPLETED", result.message
    assert sent(broker) == [("XYZ", "BUY", 240, "LMT", "U001")]
    assert "XYZ" not in ma.paused_symbols("Taylor", path=live_files.attribution)

    telegram.ok = False                                  # 沒送到：Output Log 照樣顯示，下次再發
    first = takeover.take_notices("Taylor")
    assert any(line.startswith("✅ XYZ 已交給策略接管") for line in first)
    telegram.ok = True
    assert takeover.take_notices("Taylor") == first
    assert takeover.take_notices("Taylor") == []         # 送達才記已通知
    assert [dest for _m, dest in telegram.messages] == [LIVE_DEST, LIVE_DEST]
    assert all("XYZ 已交給策略接管" in m for m, _d in telegram.messages)
    assert broker.sp500_states == []                     # 接管不寫 SP500 state
