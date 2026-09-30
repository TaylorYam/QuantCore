"""新 Panel 的 Trading：送單閘門＋手動單（#151，ADR-0012）。

從主視窗操作：注入假的 IB 連線工廠（Broker 端）與 paper 連線設定，像使用者一樣
填欄位、按預覽、按送出，檢查 Broker 端收到了什麼、畫面寫了什麼。不連 IB、
不發 Telegram、不讀真實 config。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace as NS

import pytest

from tests.test_qt_panel import (  # noqa: F401  fixtures
    dialogs, extras, make_window, portfolio, ports, source,
)
from tests.test_manual_orders import FakeIB, FakeIBState

from PySide6.QtCore import Qt

from manual_orders import DEFAULT_STORE_PATH, ManualOrderService, ManualOrderStore
from qt_panel.broker_gate import PaperTarget

PAPER = PaperTarget(host="127.0.0.1", port=4013, account="DU001", client_id=92)
# 2026-09-29（週二）美東 14:00，一般時段；最近已收盤的交易日是 09-28
NOW = datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc)


def order_factory(action, quantity, price):
    return NS(action=action, totalQuantity=quantity, lmtPrice=price, orderType="LMT",
              tif="DAY", outsideRth=False, account="", orderRef="", orderId=0, permId=0,
              cashQty="UNSET")


class FakeSymbols:
    names = {"AAPL": "Apple Inc. - Common Stock", "AAL": "American Airlines Group Inc.",
             "AMZN": "Amazon.com, Inc. - Common Stock"}

    def name_for(self, symbol):
        return self.names.get(symbol.strip().upper())

    def search(self, query, limit=8):
        q = query.strip().upper()
        return [(s, n) for s, n in self.names.items() if q and s.startswith(q)][:limit]


@pytest.fixture
def ib_state():
    state = FakeIBState()
    state.managed = ["DU001"]
    state.positions_by_account = {"DU001": {}}
    state.net_liquidation = {"DU001": "20000"}
    state.daily_bars = [(date(2026, 9, 28), 100.0)]
    return state


@pytest.fixture
def services(tmp_path):
    built = []

    def build(**kw):
        service = ManualOrderService(
            account_names=["Taylor", "Rosa", "Yvonne"],
            store=ManualOrderStore(tmp_path / "paper.sqlite3"),
            universe_loader=lambda: {"SPY", "SSO"}, order_factory=order_factory,
            clock=lambda: NOW, **kw)
        built.append((service, kw))
        return service

    build.built = built
    return build


@pytest.fixture
def trading_window(make_window, ib_state, services, qtbot):
    def make(**kw):
        kw.setdefault("paper_target", PAPER)
        kw.setdefault("broker_factory", lambda: FakeIB(ib_state))
        kw.setdefault("manual_order_service", services)
        kw.setdefault("symbol_loader", FakeSymbols)
        w = make_window(**kw)
        qtbot.mouseClick(w.nav_button("Trading"), Qt.LeftButton)
        return w
    return make


def fill(panel, *, account="Taylor", symbol="AAPL", quantity="5", price="100",
         action="買進"):
    panel.account_box.setCurrentText(account)
    panel.symbol_edit.setText(symbol)
    panel.action_box.setCurrentText(action)
    panel.quantity_edit.setText(quantity)
    panel.price_edit.setText(price)


def click_and_wait(qtbot, panel, button):
    qtbot.mouseClick(button, Qt.LeftButton)
    qtbot.waitUntil(lambda: not panel.busy(), timeout=3000)


def wait_refreshed(qtbot, panel):
    qtbot.waitUntil(lambda: not panel.refreshing(), timeout=3000)


# ── 送單閘門（AC1）──────────────────────────────────────────────────

def test_live_gateway_on_the_paper_port_gets_no_order_and_the_screen_says_why(
        trading_window, ib_state, qtbot):
    ib_state.managed = ["U001"]                 # paper port 其實接到實盤 Gateway
    w = trading_window()
    panel = w.trading.manual
    fill(panel)
    click_and_wait(qtbot, panel, panel.preview_button)
    text = panel.preview_text.text()
    assert "送單閘門拒絕" in text and "U001" in text and "沒有送出任何單" in text
    assert not panel.confirm_button.isEnabled()
    assert ib_state.placed == []
    assert all(not ib.connected for ib in ib_state.ib_instances)


def test_gateway_turning_live_before_the_recheck_blocks_the_send(
        trading_window, ib_state, dialogs, qtbot):
    w = trading_window()
    panel = w.trading.manual
    fill(panel)
    click_and_wait(qtbot, panel, panel.preview_button)
    assert panel.confirm_button.isEnabled()
    ib_state.managed = ["U001"]
    click_and_wait(qtbot, panel, panel.confirm_button)
    assert "送單閘門拒絕" in panel.preview_text.text()
    assert dialogs.confirms == []
    assert ib_state.placed == []


def test_gateway_turning_live_while_the_confirm_dialog_is_open_blocks_the_send(
        trading_window, ib_state, dialogs, qtbot):
    w = trading_window()
    panel = w.trading.manual
    fill(panel)
    click_and_wait(qtbot, panel, panel.preview_button)

    def switch_then_yes(title, text, *, warning=False):
        ib_state.managed = ["U001"]
        return True

    dialogs.confirm = switch_then_yes
    click_and_wait(qtbot, panel, panel.confirm_button)
    qtbot.waitUntil(lambda: "送單閘門拒絕" in panel.preview_text.text(), timeout=3000)
    assert panel.form_status.text() == "送單前重新檢查未通過"
    assert ib_state.placed == []


def test_every_trading_connection_goes_through_the_gate(trading_window, services):
    w = trading_window()
    [(_service, kw)] = services.built
    assert kw["ib_factory"] == w.send_gate.broker
    assert kw["account_ctx_factory"]("Rosa", 6).account == "DU001"


# ── paper：預覽 → 二次確認 → 送出 → 狀態追蹤（AC2、AC4）─────────────

def test_paper_order_previews_confirms_sends_and_tracks_status(
        trading_window, ib_state, dialogs, services, qtbot):
    w = trading_window()
    assert "paper 帳號 DU001" in w.page_text("Trading")
    panel = w.trading.manual
    assert [panel.account_box.itemText(i) for i in range(panel.account_box.count())] == [
        "Taylor", "Rosa", "Yvonne"]
    fill(panel)
    click_and_wait(qtbot, panel, panel.preview_button)

    # 預覽文字就是共用的 ManualOrderPreview.confirmation_text（Tk 版顯示的同一段）
    service = services.built[0][0]
    expected = service.preview("Taylor", "AAPL", 5, 100.0).confirmation_text
    assert panel.preview_text.text() == expected
    assert "Account：Taylor" in expected and "限價：$100.0" in expected
    assert ib_state.placed == []

    click_and_wait(qtbot, panel, panel.confirm_button)
    [(title, prompt, warning)] = dialogs.confirms
    assert title == "再次核對委託" and not warning
    assert prompt.startswith(expected)
    assert "送往：paper 帳號 DU001（127.0.0.1:4013）" in prompt

    [trade] = ib_state.placed
    assert (trade.order.action, trade.order.totalQuantity, trade.order.lmtPrice,
            trade.order.account) == ("BUY", 5, 100.0, "DU001")
    assert trade.order.orderRef.startswith("QUANTCORE_MANUAL")
    assert set(ib_state.client_ids) == {92}
    wait_refreshed(qtbot, panel)
    [row] = panel.order_table()
    assert row[:6] == ("Taylor", "AAPL", "買進", "5", "LMT $100.00", "一般時段 RTH / DAY")
    assert "Taylor AAPL" in panel.form_status.text()

    # 部分成交
    trade.orderStatus.filled = 2
    trade.orderStatus.avgFillPrice = 99.5
    ib_state.open_trades.remove(trade)
    ib_state.completed_trades.append(trade)
    click_and_wait(qtbot, panel, panel.refresh_button)
    wait_refreshed(qtbot, panel)
    [row] = panel.order_table()
    assert row[6] == "部分成交" and row[7] == "2/5 @ $99.50"


def test_declining_the_confirm_dialog_sends_nothing(trading_window, ib_state, dialogs, qtbot):
    w = trading_window()
    panel = w.trading.manual
    fill(panel)
    click_and_wait(qtbot, panel, panel.preview_button)
    dialogs.confirm_answers = [False]
    click_and_wait(qtbot, panel, panel.confirm_button)
    assert len(dialogs.confirms) == 1
    assert ib_state.placed == []


def test_changing_a_field_after_preview_requires_a_new_preview(trading_window, qtbot):
    w = trading_window()
    panel = w.trading.manual
    fill(panel)
    click_and_wait(qtbot, panel, panel.preview_button)
    panel.price_edit.setText("101")
    assert not panel.confirm_button.isEnabled()
    assert "重新預覽" in panel.preview_text.text()


def test_invalid_field_is_explained_without_contacting_ib(trading_window, ib_state, dialogs, qtbot):
    w = trading_window()
    panel = w.trading.manual
    fill(panel, quantity="abc")
    qtbot.mouseClick(panel.preview_button, Qt.LeftButton)
    assert dialogs.messages == [("手動下單", "整股股數必須是正整數。", True)]
    assert ib_state.ib_instances == []


def test_price_warning_is_shown_and_the_confirm_dialog_warns(
        trading_window, ib_state, dialogs, qtbot):
    w = trading_window()
    panel = w.trading.manual
    fill(panel, price="120")                    # 比最近收盤 $100 高 20%
    click_and_wait(qtbot, panel, panel.preview_button)
    assert panel.preview_text.text().startswith("⚠ 價格警示")
    click_and_wait(qtbot, panel, panel.confirm_button)
    assert dialogs.confirms[0][2] is True


def test_cancel_waits_for_ib_and_the_result_goes_to_the_log_not_telegram(
        trading_window, ib_state, dialogs, qtbot):
    w = trading_window()
    panel = w.trading.manual
    fill(panel)
    click_and_wait(qtbot, panel, panel.preview_button)
    click_and_wait(qtbot, panel, panel.confirm_button)
    wait_refreshed(qtbot, panel)
    panel.orders.selectRow(0)
    assert panel.cancel_button.isEnabled()
    ib_state.cancel_status = "Cancelled"
    click_and_wait(qtbot, panel, panel.cancel_button)
    assert dialogs.confirms[-1][0] == "取消手動委託"
    [trade] = ib_state.placed
    assert ib_state.cancelled_orders == [trade.order]
    ib_state.open_trades.remove(trade)
    ib_state.completed_trades.append(trade)
    panel.refresh_status()
    wait_refreshed(qtbot, panel)
    assert panel.order_table() == []
    qtbot.waitUntil(lambda: "[手動單·paper] Taylor" in w.dashboard.log_text(), timeout=2000)
    assert "已取消" in w.dashboard.log_text()


def test_ib_expiry_is_tracked_as_expired(trading_window, ib_state, qtbot):
    w = trading_window()
    panel = w.trading.manual
    fill(panel)
    click_and_wait(qtbot, panel, panel.preview_button)
    click_and_wait(qtbot, panel, panel.confirm_button)
    wait_refreshed(qtbot, panel)
    [trade] = ib_state.placed
    trade.orderStatus.status = "Cancelled"
    trade.completed_status = "Expired (5)"
    ib_state.open_trades.remove(trade)
    ib_state.completed_trades.append(trade)
    panel.refresh_status()
    wait_refreshed(qtbot, panel)
    assert panel.order_table() == []
    qtbot.waitUntil(lambda: "已失效" in w.dashboard.log_text(), timeout=2000)


def test_modify_loads_the_order_then_cancels_before_offering_a_new_preview(
        trading_window, ib_state, dialogs, qtbot):
    w = trading_window()
    panel = w.trading.manual
    fill(panel)
    click_and_wait(qtbot, panel, panel.preview_button)
    click_and_wait(qtbot, panel, panel.confirm_button)
    wait_refreshed(qtbot, panel)
    panel.symbol_edit.clear()
    panel.orders.selectRow(0)
    qtbot.mouseClick(panel.modify_button, Qt.LeftButton)
    assert panel.symbol_edit.text() == "AAPL" and panel.price_edit.text() == "100.0"
    panel.price_edit.setText("98")
    panel.orders.selectRow(0)
    ib_state.cancel_status = "Cancelled"
    click_and_wait(qtbot, panel, panel.modify_button)
    assert dialogs.confirms[-1][0] == "取消後修改手動委託"
    qtbot.waitUntil(lambda: "原委託已取消" in panel.preview_text.text(), timeout=3000)
    assert panel.quantity_edit.text() == "5" and panel.price_edit.text() == "98.0"
    assert len(ib_state.placed) == 1           # 新委託要再預覽、再確認才送
    assert not panel.confirm_button.isEnabled()


def test_open_orders_left_from_last_time_are_refreshed_on_start(
        trading_window, ib_state, services, qtbot, tmp_path):
    w = trading_window()
    panel = w.trading.manual
    fill(panel)
    click_and_wait(qtbot, panel, panel.preview_button)
    click_and_wait(qtbot, panel, panel.confirm_button)
    wait_refreshed(qtbot, panel)
    w.shutdown()

    w2 = trading_window()
    panel2 = w2.trading.manual
    wait_refreshed(qtbot, panel2)
    assert [row[1] for row in panel2.order_table()] == ["AAPL"]


# ── 英數欄位（AC3）──────────────────────────────────────────────────

def test_ascii_fields_turn_off_the_input_method_and_convert_full_width(trading_window, qtbot):
    w = trading_window()
    panel = w.trading.manual
    fields = (panel.symbol_edit, panel.quantity_edit, panel.price_edit, panel.amount_edit,
              panel.percent_box.lineEdit())
    assert all(not f.testAttribute(Qt.WA_InputMethodEnabled) for f in fields)
    panel.price_edit.setText("１８４．４")
    assert panel.price_edit.text() == "184.4"
    panel.price_edit.setText("184。4")
    assert panel.price_edit.text() == "184.4"
    panel.quantity_edit.setText("１０")
    assert panel.quantity_edit.text() == "10"
    panel.symbol_edit.setText("ａａｐｌ")
    assert panel.symbol_edit.text() == "aapl"
    panel.input_mode_box.setCurrentText("帳戶權益 %")
    panel.percent_box.lineEdit().setText("２５")
    assert panel.percent_box.currentText() == "25"


def test_percent_of_equity_uses_the_paper_account_equity(trading_window, ib_state, qtbot):
    w = trading_window()
    panel = w.trading.manual
    fill(panel, quantity="")
    panel.input_mode_box.setCurrentText("帳戶權益 %")
    assert not panel.quantity_edit.isEnabled() and panel.percent_box.isEnabled()
    panel.percent_box.setCurrentText("25")
    click_and_wait(qtbot, panel, panel.preview_button)
    text = panel.preview_text.text()
    assert "$20,000.00 × 25% = $5,000.00" in text
    assert "換算整股股數：50" in text


# ── 代號建議 ─────────────────────────────────────────────────────────

def test_symbol_suggestions_follow_typing_and_enter_accepts(trading_window, qtbot):
    w = trading_window()
    panel = w.trading.manual
    qtbot.waitUntil(panel.symbols_loaded, timeout=2000)
    qtbot.keyClicks(panel.symbol_edit, "AA")
    assert [line.split()[0] for line in panel.suggestion_lines_shown()] == ["AAPL", "AAL"]
    qtbot.keyClick(panel.symbol_edit, Qt.Key_Down)
    qtbot.keyClick(panel.symbol_edit, Qt.Key_Return)
    assert panel.symbol_edit.text() == "AAL"
    assert panel.suggestion_lines_shown() == []
    assert panel.symbol_hint.text() == "American Airlines Group Inc."


def test_unknown_symbol_hint_still_allows_preview(trading_window, qtbot):
    w = trading_window()
    panel = w.trading.manual
    qtbot.waitUntil(panel.symbols_loaded, timeout=2000)
    panel.symbol_edit.setText("ZZZZ")
    assert "清單內找不到此代號" in panel.symbol_hint.text()


# ── 設定與紀錄檔 ─────────────────────────────────────────────────────

def test_paper_orders_use_their_own_store_not_the_tk_manual_orders_file(tmp_path):
    from qt_panel.trading import PAPER_STORE_PATH, paper_manual_order_service

    assert PAPER_STORE_PATH != DEFAULT_STORE_PATH
    service = paper_manual_order_service(["Taylor"], tmp_path / "p.sqlite3")(
        notifier=lambda *_: True)
    assert service.store.path == tmp_path / "p.sqlite3"


def test_without_a_paper_target_no_service_is_built(make_window, services):
    w = make_window(manual_order_service=services, paper_problem="尚未設定 paper 連線")
    assert services.built == []
    assert w.trading.manual is None
    assert "尚未設定 paper 連線" in w.page_text("Trading")


def test_service_start_failure_is_explained(make_window):
    def broken(**_kw):
        raise OSError("磁碟唯讀")

    w = make_window(paper_target=PAPER, manual_order_service=broken)
    assert w.trading.manual is None
    assert "磁碟唯讀" in w.page_text("Trading")
