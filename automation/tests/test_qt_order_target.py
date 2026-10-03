"""新 Panel 的送單目標（#183）：paper 與實盤各跑一遍手動單、Sync Pos、交給策略接管。

只從送單目標的 interface 操作（手動單 service、Sync Pos 的計算／通知／送出、接管的
暫停／預覽／送出／通知），經過真的送單閘門，跑真的 manual_orders、sync_position、
strategy_takeover、manual_attribution；IB 換成假的 broker、訊號換成固定值、紀錄檔
放 tmp_path、Telegram 用假的。不需要 QApplication。

斷言看得到的結果：連到哪個 host／port／clientId、閘門有沒有拒絕、哪份紀錄檔多了
資料或根本沒被建立、state 有沒有寫、假 Telegram 收到什麼、Output Log 回傳哪些行、
trade_log 有沒有寫。Qt 畫面流程在 test_qt_sync_pos／test_qt_takeover／test_qt_trading。
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from tests.test_manual_orders import FakeIB, FakeIBState

from account_utils import ROLE_MANUAL_ORDER, ROLE_REBALANCE, ROLE_TAKEOVER
from manual_orders import ManualOrderService, ManualOrderStore
from qt_panel.broker_gate import PaperTarget, SendGate, SendRefused
from qt_panel.mode import PanelMode
from qt_panel.order_target import OrderFiles, order_target_for

PAPER = PaperTarget(host="127.0.0.1", port=4013, account="DU001", client_id=92)
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


class SyncBroker:
    """假的 IB：記下連線與送單；帳號、持倉、報價由 state 決定。"""

    def __init__(self, state):
        self.state = state
        self.connected = False
        self.client = NS(port=4013)
        state.instances.append(self)

    def connect(self, host, port, *, clientId, timeout):
        self.connected = True
        self.state.connects.append((host, port, clientId))

    def managedAccounts(self):
        return list(self.state.managed)

    def disconnect(self):
        self.connected = False

    def isConnected(self):
        return self.connected

    def openTrades(self):
        return []

    def qualifyContracts(self, *contracts):
        return list(contracts)

    def reqTickers(self, *contracts):
        return [NS(contract=c, bid=99.9, ask=100.1, last=100.0, close=100.0,
                   midpoint=lambda: 100.0) for c in contracts]

    def placeOrder(self, contract, order):
        order.orderId = 500 + len(self.state.placed)
        self.state.placed.append((contract.symbol, order.action, int(order.totalQuantity),
                                  order.orderType, order.account))
        return NS(order=order, contract=contract, fills=[], orderStatus=NS(status="Submitted"))

    def sleep(self, *_):
        pass


class TakeoverBroker(SyncBroker):
    """SyncBroker 加上掛單清單與成交行為（fill／reject／working）。"""

    def reqAllOpenOrders(self):
        return list(self.state.open_orders)

    def placeOrder(self, contract, order):
        trade = super().placeOrder(contract, order)
        trade.log = []
        trade.orderStatus = NS(status="Submitted", filled=0)
        sign = 1 if order.action == "BUY" else -1
        qty = int(order.totalQuantity)
        if self.state.behavior == "fill":
            trade.orderStatus = NS(status="Filled", filled=qty)
            self.state.positions[contract.symbol] = (
                self.state.positions.get(contract.symbol, 0.0) + sign * qty)
        elif self.state.behavior == "reject":
            trade.orderStatus = NS(status="Inactive", filled=0)
            trade.log.append(NS(message="Order rejected - reason: margin"))
        return trade


@pytest.fixture
def broker():
    return NS(managed=["DU001"], positions={"AAPL": 100.0, "XYZ": 10.0}, instances=[],
              connects=[], placed=[], ctxs=[], persist=[], gate=None, open_orders=[],
              behavior="fill", sp500_states=[])


@pytest.fixture
def paper_files(tmp_path):
    return OrderFiles(orders=tmp_path / "paper_orders.sqlite3",
                      attribution=tmp_path / "paper_attribution.sqlite3")


@pytest.fixture
def live_files(tmp_path):
    return OrderFiles(orders=tmp_path / "live_orders.sqlite3",
                      attribution=tmp_path / "live_attribution.sqlite3")


@pytest.fixture
def sync_env(broker, live_files, monkeypatch):
    """固定訊號與 IB 讀數；sync_position、rebalance_plan、manual_attribution 用真的。
    manual_attribution 的預設檔也指到 tmp 的實盤檔：萬一有路徑沒指定，寫進去的資料
    會被下面「實盤紀錄沒被建立」的斷言抓到，而且碰不到真的檔案。"""
    import ib_fetcher
    import manual_attribution as ma
    import rebalance_ib
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

    def nav(ib, ctx):
        broker.ctxs.append(ctx)
        return 100_000.0

    monkeypatch.setattr(signals, "get_sr_weights", sr_weights)
    monkeypatch.setattr(signals, "get_sp500_weights", sp500_weights)
    monkeypatch.setattr(signals, "get_sp500_universe", lambda historical=False: set(JOINED))
    monkeypatch.setattr(ib_fetcher, "get_nav", nav)
    monkeypatch.setattr(ib_fetcher, "get_positions", lambda ib, ctx: dict(broker.positions))
    monkeypatch.setattr(ib_fetcher, "get_prices",
                        lambda ib, syms, ctx, **k: {s: 100.0 for s in syms})
    monkeypatch.setattr(rebalance_ib, "check_fills", lambda trades: [])
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


def make_target(mode, broker_factory, *, paper_files, live_files, logs, loader=None,
                paper_target=PAPER):
    target, problem = order_target_for(
        mode, send_gate=SendGate(mode, broker_factory), paper_target=paper_target,
        paper_problem="尚未設定 paper 連線",
        account_ctx_loader=loader or (live_ctx if mode is PanelMode.PRODUCTION else base_ctx),
        log=logs.append, paper_files=paper_files, live_files=live_files)
    assert problem == ""
    return target


@pytest.fixture
def logs():
    return []


@pytest.fixture
def paper(broker, paper_files, live_files, logs):
    return make_target(PanelMode.PARALLEL, lambda: TakeoverBroker(broker),
                       paper_files=paper_files, live_files=live_files, logs=logs)


@pytest.fixture
def live(broker, paper_files, live_files, logs):
    broker.managed = ["U001"]
    return make_target(PanelMode.PRODUCTION, lambda: TakeoverBroker(broker),
                       paper_files=paper_files, live_files=live_files, logs=logs)


# ── 哪個模式拿到哪個送單目標 ─────────────────────────────────────────

def test_mode_decides_the_order_target(paper_files, live_files):
    common = dict(send_gate=SendGate(PanelMode.PARALLEL, object), paper_problem="缺 paper",
                  account_ctx_loader=base_ctx, log=print, paper_files=paper_files,
                  live_files=live_files)
    live_target, _ = order_target_for(PanelMode.PRODUCTION, paper_target=PAPER, **common)
    assert live_target.text == "實盤帳戶（各帳戶自己的 Gateway）"
    paper_target, _ = order_target_for(PanelMode.PARALLEL, paper_target=PAPER, **common)
    assert paper_target.text == "paper 帳號 DU001（127.0.0.1:4013）"
    assert order_target_for(PanelMode.PARALLEL, paper_target=None, **common) == (None,
                                                                                "缺 paper")


def test_live_files_default_to_the_attribution_defaults_at_build_time():
    """沒給紀錄檔時，實盤用 manual_attribution 當下的預設路徑（conftest 指到暫存目錄），
    不在 import 時就固定成 live 目錄的檔案。"""
    import manual_attribution as ma

    target, _ = order_target_for(
        PanelMode.PRODUCTION, send_gate=SendGate(PanelMode.PRODUCTION, object),
        paper_target=None, paper_problem="", account_ctx_loader=live_ctx, log=print)
    assert target.files == OrderFiles(orders=ma.DEFAULT_MANUAL_ORDERS_PATH,
                                      attribution=ma.DEFAULT_LEDGER_PATH)


# ── 送單閘門：paper 目標一律只放行 paper 帳號 ───────────────────────

@pytest.mark.parametrize("path", ["sync", "takeover"])
def test_paper_target_refuses_a_live_gateway_before_anything_is_read(
        paper, broker, takeover_signals, paper_files, path):
    pause_xyz(paper_files)
    broker.managed = ["U001"]                    # paper port 其實接到實盤 Gateway
    with pytest.raises(SendRefused, match="U001"):
        if path == "sync":
            paper.sync().compute("Taylor", lambda m: None)
        else:
            paper.takeover().preview("Taylor", "XYZ", lambda m: None)
    assert broker.ctxs == [] and broker.placed == []
    assert all(not ib.connected for ib in broker.instances)


def test_paper_manual_orders_are_refused_on_a_live_gateway(
        broker, paper_files, live_files, logs):
    ib = FakeIBState()
    ib.managed = ["U001"]
    target = make_target(PanelMode.PARALLEL, lambda: FakeIB(ib), paper_files=paper_files,
                         live_files=live_files, logs=logs)
    service = target.manual_order_service(NAMES, _service_factory)
    with pytest.raises(SendRefused, match="U001"):
        service.preview("Taylor", "AAPL", 5, 100.0)
    assert ib.placed == []


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


def test_paper_manual_orders_use_the_paper_account_store_and_output_log(
        paper_files, live_files, logs, telegram):
    ib = _ib("DU001")
    target = make_target(PanelMode.PARALLEL, lambda: FakeIB(ib), paper_files=paper_files,
                         live_files=live_files, logs=logs)
    service = target.manual_order_service(NAMES, _service_factory)
    trade = _cancel_and_close(service, ib)

    assert (trade.order.account, trade.order.totalQuantity) == ("DU001", 5)
    service.submit(service.preview("Yvonne", "AAPL", 1, 100.0))   # 每個帳戶名稱都導向 paper
    assert ib.placed[-1].order.account == "DU001"
    assert set(ib.client_ids) == {92}             # paper 手動單 clientId = 基準號
    assert service.store.path == paper_files.orders
    assert not live_files.orders.exists()         # 實盤手動單紀錄沒被建立
    assert any(line.startswith("[手動單·paper] Taylor：") and "已取消" in line for line in logs)
    assert telegram.messages == []


def test_live_manual_orders_keep_the_tk_identity_store_and_telegram(
        paper_files, live_files, logs, telegram):
    ib = _ib("U001")
    # 切換前 Tk Panel 送出的單：同一個實盤紀錄檔、同一個 clientId role
    tk = _service_factory(store=ManualOrderStore(live_files.orders),
                          ib_factory=lambda: FakeIB(ib), account_ctx_factory=live_ctx,
                          notifier=lambda *a: True)
    tk_order = tk.submit(tk.preview("Taylor", "ZZZZ", 4, 100.0))
    tk_trade = ib.open_trades.pop()

    target = make_target(PanelMode.PRODUCTION, lambda: FakeIB(ib), paper_files=paper_files,
                         live_files=live_files, logs=logs)
    service = target.manual_order_service(NAMES, _service_factory)
    assert service.store.path == live_files.orders
    trade = _cancel_and_close(service, ib)
    assert (trade.order.account, trade.order.totalQuantity) == ("U001", 5)
    assert set(ib.client_ids) == {20 + ROLE_MANUAL_ORDER}      # 和 Tk 同一個手動單 clientId
    # 結案通知（還有 Tk 那張單暫時查不到的提醒）都發該帳戶 Telegram
    assert {dest["token"] for _msg, dest in telegram.messages} == {LIVE_DEST["token"]}
    assert any("AAPL" in msg and "已取消" in msg for msg, _dest in telegram.messages)
    assert not paper_files.orders.exists()

    # Tk 那張單切換後成交：同一個 clientId 認得它
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

def test_paper_sync_uses_the_paper_account_and_writes_no_live_record(
        paper, broker, sync_env, paper_files, telegram, trade_log, monkeypatch):
    import manual_attribution as ma
    import signals
    import skewls_state

    skew_saves = []
    monkeypatch.setattr(signals, "get_skewls_weights", lambda: ({"SH": 0.2}, "2026-09-29"))
    monkeypatch.setattr(skewls_state, "save_state", lambda *a: skew_saves.append(a))
    pause_xyz(paper_files, "Rosa")
    sync = paper.sync()
    assert (sync.venue, sync.target_text) == ("paper", "paper 帳號 DU001（127.0.0.1:4013）")
    assert "Telegram" not in sync.helper_text and "trade_log" not in sync.helper_text

    plan = sync.compute("Rosa", lambda m: None)
    [ctx] = broker.ctxs
    assert (ctx.name, ctx.account, ctx.host, ctx.port, ctx.client_id) == (
        "Rosa", "DU001", "127.0.0.1", 4013, 93)
    assert ctx.price_feed_port is None and ctx.dry_run is False and ctx.tg_token == ""
    assert broker.persist == [False]                    # SP500 state 只讀不寫
    assert sync.take_notices(plan) == [
        "⏸ XYZ 策略單暫停：手動持倉 10 股已進入 SP500 Universe"]
    assert sync.take_notices(sync.compute("Rosa", lambda m: None)) == []   # paper 紀錄記已通知

    placed, _fills = sync.submit(plan, lambda m: None)
    assert ("SH", "BUY", 40, "LMT", "DU001") in broker.placed   # 20% × SkewLS 20% × $100k ÷ $100
    assert [p["symbol"] for p in placed if p["symbol"] == "XYZ"] == []   # 暫停中的不送
    assert {c[1:] for c in broker.connects} == {(4013, 93)}
    assert skew_saves == [] and trade_log == [] and telegram.messages == []
    assert "XYZ" in ma.paused_symbols("Rosa", path=paper_files.attribution)
    assert not sync_env.attribution.exists()            # 實盤歸屬紀錄沒被建立或寫入
    assert not sync_env.orders.exists()


def test_live_sync_uses_the_rebalance_identity_and_reports_like_tk(
        live, broker, sync_env, live_files, telegram, trade_log):
    pause_xyz(live_files)
    broker.positions = {"AAPL": 100.0, "XYZ": 10.0}
    sync = live.sync()
    assert (sync.venue, sync.target_text) == ("實盤", "實盤帳戶（各帳戶自己的 Gateway）")
    assert "Telegram" in sync.helper_text and "trade_log.xlsx" in sync.helper_text

    plan = sync.compute("Taylor", lambda m: None)
    [ctx] = broker.ctxs
    assert (ctx.account, ctx.port, ctx.client_id) == ("U001", 4001, 20 + ROLE_REBALANCE)
    assert broker.persist == [True]                     # 實盤 SP500 state 照 Tk 寫回

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
    assert broker.placed == [("AAPL", "BUY", 150, "LMT", "U001")]
    assert [r["account_name"] for r in trade_log] == ["Taylor"]
    [(msg, dest)] = telegram.messages
    assert "[Taylor] Sync Position — LMT" in msg and dest == LIVE_DEST
    assert not (live_files.attribution.parent / "paper_attribution.sqlite3").exists()


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

def test_paper_takeover_uses_the_paper_ledger_and_only_the_output_log(
        paper, broker, takeover_signals, sync_env, paper_files, live_files, telegram):
    import manual_attribution as ma

    pause_xyz(paper_files)
    pause_xyz(live_files, "Rosa")                        # 實盤的暫停不會出現在 paper
    takeover = paper.takeover()
    assert takeover.paused("Taylor") == {
        "XYZ": "手動持倉 10 股已進入 SP500 Universe"}
    assert takeover.paused("Rosa") == {}
    live_before = live_files.attribution.read_bytes()

    preview = takeover.preview("Taylor", "XYZ", lambda m: None)
    ctx = broker.ctxs[0]
    assert (ctx.account, ctx.port, ctx.client_id) == ("DU001", 4013, 94)
    assert ctx.price_feed_port is None and ctx.dry_run is False and ctx.tg_token == ""
    result = takeover.submit(preview, lambda m: None)
    assert result.status == "COMPLETED", result.message
    assert broker.placed == [("XYZ", "BUY", 240, "LMT", "DU001")]
    assert {c[2] for c in broker.connects} == {94}

    lines = takeover.take_notices("Taylor")
    assert any(line.startswith("✅ XYZ 已交給策略接管") for line in lines)
    assert takeover.take_notices("Taylor") == []         # paper 紀錄記已通知，只提一次
    assert "XYZ" not in ma.paused_symbols("Taylor", path=paper_files.attribution)
    assert telegram.messages == [] and broker.sp500_states == []
    assert live_files.attribution.read_bytes() == live_before   # 實盤紀錄一個 byte 都沒動


def test_live_takeover_uses_the_takeover_identity_and_telegram(
        live, broker, takeover_signals, live_files, paper_files, telegram):
    import manual_attribution as ma

    pause_xyz(live_files)
    pause_xyz(paper_files, "Rosa")                       # paper 的暫停不會出現在實盤
    takeover = live.takeover()
    assert (takeover.venue, takeover.target_text) == ("實盤", "實盤帳戶（各帳戶自己的 Gateway）")
    assert set(takeover.paused("Taylor")) == {"XYZ"} and takeover.paused("Rosa") == {}

    preview = takeover.preview("Taylor", "XYZ", lambda m: None)
    assert (broker.ctxs[0].account, broker.ctxs[0].client_id) == ("U001", 20 + ROLE_TAKEOVER)
    result = takeover.submit(preview, lambda m: None)
    assert result.status == "COMPLETED", result.message
    assert broker.placed == [("XYZ", "BUY", 240, "LMT", "U001")]
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


def test_paper_takeover_and_sync_connections_do_not_share_a_client_id(
        paper, broker, takeover_signals, paper_files):
    """手動單 92、Sync Pos 93、接管 94：三者同時連線也不會互踢。"""
    pause_xyz(paper_files)
    paper.sync().compute("Taylor", lambda m: None)
    paper.takeover().preview("Taylor", "XYZ", lambda m: None)
    assert [c[2] for c in broker.connects] == [93, 94]
