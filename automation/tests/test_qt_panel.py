"""新 PySide6 Panel 骨架（#147，ADR-0012）。

從主視窗操作，外部依賴（Gateway 狀態來源、時鐘、系統匣）一律注入假的；
不連 IB、不讀真實 config。沒裝 PySide6／pytest-qt 的環境（例如 live 機器
直接跑 pytest）整檔略過。
"""
from __future__ import annotations

import ast
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

if os.environ.get("CI"):
    import PySide6  # noqa: F401  CI 一定要真的跑，不能整檔略過還顯示綠燈
    import pytestqt  # noqa: F401
else:
    pytest.importorskip("PySide6")
    pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt  # noqa: E402

ACCOUNTS = [
    {"name": "Taylor", "port": 4001},
    {"name": "Rosa", "port": 4002},
    {"name": "Yvonne", "port": 4003},
]


class FakeGatewaySource:
    def __init__(self):
        self.on_state = None
        self.started = 0
        self.stopped = 0
        self.rechecks = 0

    def start(self, on_state):
        self.on_state = on_state
        self.started += 1

    def stop(self):
        self.stopped += 1

    def recheck(self):
        self.rechecks += 1


class FakeTray:
    def __init__(self, on_show, on_quit):
        self.on_show = on_show
        self.on_quit = on_quit
        self.shown = False

    def show(self):
        self.shown = True

    def hide(self):
        self.shown = False


class FakePorts:
    """gateway_start.Ports 的假實作：記下每一步呼叫的順序。"""

    def __init__(self, *, running=(), stored=None, logged_in=("Taylor", "Rosa", "Yvonne"),
                 gate=None):
        from gateway_start import Credentials
        self.running = set(running)
        self.stored = dict(stored) if stored is not None else {
            a: Credentials(a.lower(), "pw") for a in ("Taylor", "Rosa", "Yvonne")}
        self.logged_in = set(logged_in)
        self.gate = gate
        self.calls = []
        self.ask = None

    def running_live_accounts(self):
        if self.gate is not None:
            self.gate.wait(5)
        return set(self.running)

    def get_credentials(self, account):
        return self.stored.get(account)

    def set_credentials(self, account, creds):
        self.calls.append(("set", account))
        self.stored[account] = creds

    def ask_credentials(self, account):
        return self.ask(account)

    def launch(self, account):
        self.calls.append(("launch", account))

    def fill_login(self, account, creds):
        self.calls.append(("fill", account))

    def submit_login(self, account):
        self.calls.append(("submit", account))

    def wait_logged_in(self, account, timeout_sec):
        self.calls.append(("wait", account))
        return account in self.logged_in


def holding(sym, sh, cost, px, name=""):
    return {"symbol": sym, "shares": sh, "avg_cost": cost, "mkt_px": px,
            "mkt_val": sh * px, "upnl": (px - cost) * sh,
            "upnl_pct": (px / cost - 1) * 100 if cost else 0, "name": name}


def portfolio_dict(name, nav=100_000.0, cushion=35.0, etf=(), stocks=(), other=()):
    etf, stocks, other = list(etf), list(stocks), list(other)
    strat = sum(x["mkt_val"] for x in etf + stocks)
    oth = sum(x["mkt_val"] for x in other)
    return {"available": True, "date": "2026-09-30", "nav": nav, "cushion": cushion,
            "etf_holdings": etf, "stock_holdings": stocks, "other_holdings": other,
            "strategy_mktval": strat, "other_mktval": oth, "total_mktval": strat + oth,
            "total_upnl": sum(x["upnl"] for x in etf + stocks + other),
            "source": f"IB Live [{name}]"}


class FakePortfolio:
    """portfolio_fetch 的假實作：每帳戶回傳設定好的 dict，或丟出例外。"""

    def __init__(self):
        self.results = {
            "Taylor": portfolio_dict(
                "Taylor", nav=50_000.0,
                etf=[holding("XLK", 40, 200.0, 230.0), holding("SIJ", 100, 12.0, 11.0)],
                stocks=[holding("AAPL", 30, 150.0, 200.0, "Apple Inc")],
                other=[holding("BTC", 50, 30.0, 45.0, "Bitcoin ETF")]),
            "Rosa": portfolio_dict("Rosa", nav=80_000.0, etf=[holding("XLB", 100, 90.0, 95.0)]),
            "Yvonne": portfolio_dict("Yvonne", nav=110_000.0, cushion=8.0),
        }
        self.calls = []

    def __call__(self, name):
        self.calls.append(name)
        result = self.results[name]
        if isinstance(result, Exception):
            raise result
        return result


class FakeDialogs:
    def __init__(self, answers=None):
        self.answers = answers or {}
        self.asked = []
        self.messages = []

    def ask_credentials(self, account, username=""):
        self.asked.append((account, username, threading.current_thread() is threading.main_thread()))
        return self.answers.get(account)

    def show_message(self, title, text, *, error=False):
        self.messages.append((title, text, error))


class FakeStore:
    def __init__(self, data=None, read_error=None, write_error=None):
        self.data = dict(data or {})
        self.read_error = read_error
        self.write_error = write_error
        self.writes = []

    def read(self, account):
        if self.read_error:
            raise self.read_error
        return self.data.get(account)

    def write(self, account, creds):
        if self.write_error:
            raise self.write_error
        self.writes.append((account, creds))
        self.data[account] = creds


@pytest.fixture
def source():
    return FakeGatewaySource()


@pytest.fixture
def ports():
    return FakePorts()


@pytest.fixture
def dialogs():
    return FakeDialogs()


@pytest.fixture
def portfolio():
    return FakePortfolio()


@pytest.fixture
def make_window(qtbot, source, ports, dialogs, portfolio):
    from qt_panel.main_window import MainWindow
    from qt_panel.mode import PanelMode

    def make(**kw):
        kw.setdefault("accounts", ACCOUNTS)
        kw.setdefault("mode", PanelMode.PARALLEL)
        kw.setdefault("gateway_source", source)
        kw.setdefault("gateway_ports_factory", lambda ask: setattr(ports, "ask", ask) or ports)
        kw.setdefault("credential_store", FakeStore())
        kw.setdefault("portfolio_fetch", portfolio)
        kw.setdefault("portfolio_next_refresh", lambda now: now + timedelta(days=1))
        kw.setdefault("dialogs", dialogs)
        kw.setdefault("read_data_date", lambda: "2026-09-28")
        kw.setdefault("today_jobs", lambda now: [])
        kw.setdefault("tk_panel_running", lambda: True)
        kw.setdefault("taipei_now", lambda: datetime(2026, 9, 29, 20, 30))
        kw.setdefault("clock", lambda: datetime(2026, 9, 29, 20, 30, 5))
        w = MainWindow(**kw)
        qtbot.addWidget(w)
        w.start()
        w.show()
        return w

    return make


# ── 模式 ─────────────────────────────────────────────────────────────

def test_mode_defaults_to_parallel():
    from qt_panel.mode import PanelMode, resolve_mode

    assert resolve_mode({}) == (PanelMode.PARALLEL, "")
    assert resolve_mode({"qt_panel_mode": "parallel"}) == (PanelMode.PARALLEL, "")


def test_production_mode_is_not_available_yet_and_stays_parallel():
    from qt_panel.mode import PanelMode, resolve_mode

    mode, note = resolve_mode({"qt_panel_mode": "production"})
    assert mode is PanelMode.PARALLEL
    assert "正式模式" in note


def test_unknown_mode_value_stays_parallel():
    from qt_panel.mode import PanelMode, resolve_mode

    mode, note = resolve_mode({"qt_panel_mode": "yolo"})
    assert mode is PanelMode.PARALLEL
    assert "yolo" in note


# ── 主視窗 ───────────────────────────────────────────────────────────

def test_header_marks_parallel_mode_and_shows_the_clock(make_window):
    w = make_window()
    text = w.header_text()
    assert "並行模式" in text and "只看不下單" in text
    assert "2026-09-29" in text and "20:30:05" in text


def test_nav_has_three_screens_and_switches_pages(make_window, qtbot):
    w = make_window()
    assert w.nav_labels() == ["Dashboard", "Portfolio", "Trading"]
    assert w.current_page() == "Dashboard"
    qtbot.mouseClick(w.nav_button("Portfolio"), Qt.LeftButton)
    assert w.current_page() == "Portfolio"
    qtbot.mouseClick(w.nav_button("Trading"), Qt.LeftButton)
    assert w.current_page() == "Trading"


def test_dashboard_lists_every_account_gateway_before_any_status(make_window):
    w = make_window()
    assert w.dashboard.gateway_names() == ["Taylor", "Rosa", "Yvonne"]
    assert {n: w.dashboard.gateway_state(n) for n in w.dashboard.gateway_names()} == {
        "Taylor": "init", "Rosa": "init", "Yvonne": "init"}
    assert "port 4002" in w.dashboard.gateway_text("Rosa")


def test_gateway_states_from_the_source_show_on_the_cards(make_window, source):
    w = make_window()
    assert source.started == 1
    source.on_state("Taylor", "online", "")
    source.on_state("Rosa", "connecting", "")
    source.on_state("Yvonne", "offline", "connection refused")

    assert w.dashboard.gateway_state("Taylor") == "online"
    assert "線上" in w.dashboard.gateway_text("Taylor")
    assert "連線中" in w.dashboard.gateway_text("Rosa")
    assert "離線" in w.dashboard.gateway_text("Yvonne")
    assert "connection refused" in w.dashboard.gateway_text("Yvonne")


def test_gateway_state_reported_from_a_worker_thread_reaches_the_card(make_window, source, qtbot):
    w = make_window()
    t = threading.Thread(target=source.on_state, args=("Rosa", "online", ""))
    t.start()
    t.join()
    qtbot.waitUntil(lambda: w.dashboard.gateway_state("Rosa") == "online", timeout=2000)


def test_state_for_an_unknown_account_is_ignored(make_window, source):
    w = make_window()
    source.on_state("Nobody", "online", "")
    assert w.dashboard.gateway_names() == ["Taylor", "Rosa", "Yvonne"]


def test_trading_is_still_a_placeholder_that_points_to_tk(make_window):
    w = make_window()
    assert "Tk Panel" in w.page_text("Trading")


def test_recheck_button_asks_the_source_to_probe_again(make_window, source, qtbot):
    w = make_window()
    qtbot.mouseClick(w.dashboard.recheck_button, Qt.LeftButton)
    assert source.rechecks == 1


class MutableClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def test_gateways_are_rechecked_automatically_at_the_next_morning_check_time(make_window, source):
    """並行期間新 Panel 不值班，但比照 Morning check：每個 NYSE 交易日台北 07:00
    自動重新檢查一次（只重連已斷的帳戶），Gateway 每天 06:00 自動重啟後才會恢復。"""
    taipei = MutableClock(datetime(2026, 9, 29, 20, 30))      # 週二晚上
    w = make_window(taipei_now=taipei)
    assert "09-30 07:00" in w.dashboard.auto_recheck_text()

    w.tick_auto_recheck()
    assert source.rechecks == 0

    taipei.now = datetime(2026, 9, 30, 7, 0, 30)
    w.tick_auto_recheck()
    assert source.rechecks == 1
    assert "10-01 07:00" in w.dashboard.auto_recheck_text()

    w.tick_auto_recheck()                                        # 同一天不再重複
    assert source.rechecks == 1


def test_auto_recheck_skips_weekends_and_nyse_holidays(make_window, source):
    taipei = MutableClock(datetime(2026, 10, 2, 8, 0))          # 週五早上
    w = make_window(taipei_now=taipei)
    assert "10-05 07:00" in w.dashboard.auto_recheck_text()     # 下週一


def test_auto_recheck_stops_after_quit(make_window, source):
    taipei = MutableClock(datetime(2026, 9, 29, 20, 30))
    w = make_window(taipei_now=taipei, tray_factory=None)
    w.close()
    taipei.now = datetime(2026, 9, 30, 7, 1)
    w.tick_auto_recheck()
    assert source.rechecks == 0


def test_dashboard_says_watch_runs_in_the_tk_panel(make_window):
    assert "Tk Panel" in make_window().dashboard.watch_note()


def test_new_panel_has_no_path_to_watch_telegram_or_orders():
    """並行模式不值班、不發 Telegram、不下單（ADR-0012）：qt_panel 完全不 import 這些模組。"""
    forbidden = {"monitor_engine", "notify", "manual_orders", "sync_position",
                 "strategy_takeover", "rebalance_ib"}
    package = Path(__file__).resolve().parents[1] / "qt_panel"
    imported = set()
    for path in package.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
    assert not imported & forbidden


# ── 關閉與系統匣 ─────────────────────────────────────────────────────

def test_close_hides_to_tray_and_keeps_gateway_status_running(make_window, source):
    trays = []
    w = make_window(tray_factory=lambda **kw: trays.append(FakeTray(**kw)) or trays[-1])
    assert trays[0].shown
    w.close()
    assert not w.isVisible()
    assert source.stopped == 0
    trays[0].on_show()
    assert w.isVisible()


def test_quit_from_tray_stops_the_gateway_source(make_window, source):
    trays = []
    w = make_window(tray_factory=lambda **kw: trays.append(FakeTray(**kw)) or trays[-1])
    trays[0].on_quit()
    assert source.stopped == 1
    assert not trays[0].shown
    assert not w.isVisible()


def test_without_a_tray_close_really_quits(make_window, source):
    w = make_window(tray_factory=None)
    w.close()
    assert source.stopped == 1


# ── Gateway 狀態來源接線 ─────────────────────────────────────────────

class FakeMonitor:
    instances: list["FakeMonitor"] = []

    def __init__(self, account_names, on_state_change=None, on_connected=None,
                 on_disconnected=None, *, role, readonly):
        self.account_names = account_names
        self.on_state_change = on_state_change
        self.on_disconnected = on_disconnected
        self.role = role
        self.readonly = readonly
        self.started = self.stopped = False
        self.restarted = []
        FakeMonitor.instances.append(self)

    def start(self):
        self.started = True

    def stop(self, timeout=3.0):
        self.stopped = True

    def restart_account_thread(self, name):
        self.restarted.append(name)
        return name == "Rosa"


def test_parallel_mode_watches_gateways_with_the_readonly_identity():
    from account_utils import ROLE_PANEL_READONLY
    from qt_panel.gateway_status import gateway_source_for
    from qt_panel.mode import PanelMode

    FakeMonitor.instances = []
    src = gateway_source_for(PanelMode.PARALLEL, ["Taylor", "Rosa"], monitor_cls=FakeMonitor)
    seen = []
    src.start(lambda *args: seen.append(args))
    mon = FakeMonitor.instances[0]

    assert mon.account_names == ["Taylor", "Rosa"]
    assert mon.role == ROLE_PANEL_READONLY
    assert mon.readonly is True
    assert mon.started

    mon.on_state_change("Taylor", "online")
    mon.on_disconnected("Rosa", "timeout")
    assert seen == [("Taylor", "online", ""), ("Rosa", "offline", "timeout")]

    src.recheck()
    assert mon.restarted == ["Taylor", "Rosa"]
    assert seen[-1] == ("Rosa", "connecting", "")      # 重新開始連的帳戶先顯示連線中
    assert ("Taylor", "connecting", "") not in seen     # 還連著的不動

    src.stop()
    assert mon.stopped


# ── 單一實例 ─────────────────────────────────────────────────────────

def test_single_instance_lock_is_separate_from_the_tk_panel():
    from qt_panel.app import lockfile_path, show_signal_path

    assert lockfile_path().name == "quantcore_qt_panel.lock"
    assert show_signal_path().name == "quantcore_qt_panel.show"
    # Tk Panel 用 quantcore_panel.lock／.show（不 import control_panel：會跳 UAC）
    assert lockfile_path().name != "quantcore_panel.lock"


def test_second_launch_signal_brings_the_window_back(make_window, qtbot, tmp_path):
    signal = tmp_path / "show"
    w = make_window(show_signal=signal)
    w.hide()
    signal.write_text("show", encoding="utf-8")
    w.check_show_signal()
    assert w.isVisible()
    assert not signal.exists()


# ── 一鍵啟動 Gateway（#148，規則沿用 gateway_start）────────────────────

def _start_and_wait(w, qtbot):
    assert w.start_gateways()
    qtbot.waitUntil(lambda: not w.gateway_start_running(), timeout=3000)


@pytest.fixture
def no_post_login_wait(monkeypatch):
    import gateway_start as gs
    monkeypatch.setattr(gs, "POST_LOGIN_FIRST_CHECK_SEC", 0)
    monkeypatch.setattr(gs, "POST_LOGIN_RETRY_SEC", 0)
    monkeypatch.setattr(gs, "POST_LOGIN_POLL_SEC", 0.01)


def test_start_is_refused_as_a_batch_when_any_live_gateway_is_running(make_window, ports,
                                                                      dialogs, qtbot):
    ports.running = {"Rosa"}
    w = make_window()
    _start_and_wait(w, qtbot)
    assert ports.calls == []                                     # 什麼都沒開
    [(title, text, error)] = dialogs.messages
    assert "Rosa" in text and "仍在執行" in text
    assert "仍在執行" in w.dashboard.log_text()
    assert w.dashboard.start_gateways_button.isEnabled()


def test_all_logged_in_opens_three_then_logs_in_one_at_a_time_without_a_popup(
        make_window, ports, dialogs, qtbot, no_post_login_wait):
    w = make_window()
    qtbot.mouseClick(w.dashboard.start_gateways_button, Qt.LeftButton)
    qtbot.waitUntil(lambda: not w.gateway_start_running(), timeout=3000)
    order = ("Taylor", "Rosa", "Yvonne")
    assert ports.calls == ([("launch", a) for a in order] + [("fill", a) for a in order]
                           + [step for a in order for step in (("submit", a), ("wait", a))])
    assert dialogs.messages == []                                # 三個都登入：不跳窗
    assert "Yvonne：已登入" in w.dashboard.log_text()
    assert not w.isMinimized()


def test_a_failed_login_is_not_retried_and_is_reported(make_window, ports, dialogs, qtbot,
                                                        no_post_login_wait):
    ports.logged_in = {"Taylor", "Yvonne"}
    w = make_window()
    _start_and_wait(w, qtbot)
    assert ports.calls.count(("submit", "Rosa")) == 1            # 失敗不重試
    assert ("submit", "Yvonne") in ports.calls                   # 也不擋後面的帳戶
    [(_, text, error)] = dialogs.messages
    assert "Rosa：未登入" in text and not error
    assert w.isMinimized()                                       # 讓出畫面給 Rosa 的登入視窗


def test_missing_credentials_are_asked_on_the_ui_thread_and_saved(make_window, ports, dialogs,
                                                                  qtbot, no_post_login_wait):
    from gateway_start import Credentials
    del ports.stored["Rosa"]
    dialogs.answers["Rosa"] = Credentials("rosa-user", "secret")
    w = make_window()
    _start_and_wait(w, qtbot)
    assert dialogs.asked == [("Rosa", "", True)]                 # 在主執行緒跳窗
    assert ("set", "Rosa") in ports.calls
    assert ports.stored["Rosa"] == Credentials("rosa-user", "secret")
    assert ("launch", "Taylor") in ports.calls


def test_cancelling_the_credentials_dialog_opens_nothing(make_window, ports, dialogs, qtbot):
    del ports.stored["Yvonne"]
    w = make_window()
    _start_and_wait(w, qtbot)
    assert not [c for c in ports.calls if c[0] == "launch"]
    [(_, text, _)] = dialogs.messages
    assert "已取消" in text and "Yvonne" in text


def test_start_button_is_disabled_while_starting_and_a_second_start_is_ignored(
        make_window, ports, qtbot):
    gate = threading.Event()
    ports.gate = gate
    ports.running = {"Taylor"}
    w = make_window()
    assert w.start_gateways()
    assert not w.dashboard.start_gateways_button.isEnabled()
    assert w.start_gateways() is False
    gate.set()
    qtbot.waitUntil(lambda: w.dashboard.start_gateways_button.isEnabled(), timeout=3000)


def test_an_unexpected_error_is_shown_as_an_error(make_window, dialogs, qtbot):
    def broken(ask):
        raise RuntimeError("psutil exploded")
    w = make_window(gateway_ports_factory=broken)
    _start_and_wait(w, qtbot)
    [(_, text, error)] = dialogs.messages
    assert error and "psutil exploded" in text


class OnlineAfterRecheck(FakeGatewaySource):
    """重新檢查時把指定帳戶報成連上，其餘報離線。"""

    def __init__(self, online):
        super().__init__()
        self.online = set(online)

    def recheck(self):
        super().recheck()
        for name in ("Taylor", "Rosa", "Yvonne"):
            self.on_state(name, "online" if name in self.online else "offline", "")


def test_after_login_gateways_are_rechecked_once_they_accept_api(make_window, qtbot,
                                                                 no_post_login_wait):
    src = OnlineAfterRecheck({"Taylor", "Rosa", "Yvonne"})
    w = make_window(gateway_source=src)
    _start_and_wait(w, qtbot)
    qtbot.waitUntil(lambda: "登入後檢查完成" in w.dashboard.log_text(), timeout=3000)
    assert src.rechecks == 1


def test_after_login_a_gateway_that_stays_offline_is_checked_at_most_three_times(
        make_window, qtbot, no_post_login_wait):
    import gateway_start as gs
    src = OnlineAfterRecheck({"Taylor", "Yvonne"})
    w = make_window(gateway_source=src)
    _start_and_wait(w, qtbot)
    qtbot.waitUntil(lambda: "登入後檢查完成" in w.dashboard.log_text(), timeout=3000)
    assert src.rechecks == gs.POST_LOGIN_MAX_CHECKS
    assert "Rosa 仍未連上" in w.dashboard.log_text()


# ── Gateway 帳密更新 ─────────────────────────────────────────────────

def test_credentials_menu_lists_the_three_live_accounts(make_window):
    w = make_window()
    assert [a.text() for a in w.dashboard.credentials_menu.actions()] == [
        "Taylor", "Rosa", "Yvonne"]


def test_updating_credentials_prefills_the_username_and_saves(make_window, dialogs):
    from gateway_start import Credentials
    store = FakeStore({"Rosa": Credentials("rosa-old", "old")})
    dialogs.answers["Rosa"] = Credentials("rosa-new", "new")
    w = make_window(credential_store=store)
    w.dashboard.credentials_menu.actions()[1].trigger()
    assert dialogs.asked == [("Rosa", "rosa-old", True)]
    assert store.writes == [("Rosa", Credentials("rosa-new", "new"))]
    assert "Rosa Gateway 帳密已更新" in w.dashboard.log_text()
    assert "new" not in w.dashboard.log_text().replace("已更新", "")   # 不把帳密寫進 log
    assert dialogs.messages[-1][2] is False


def test_cancelling_the_credentials_update_saves_nothing(make_window, dialogs):
    store = FakeStore()
    w = make_window(credential_store=store)
    w.edit_gateway_credentials("Taylor")
    assert dialogs.asked == [("Taylor", "", True)]
    assert store.writes == []
    assert dialogs.messages == []


def test_credential_store_errors_are_shown(make_window, dialogs):
    from gateway_start import Credentials
    w = make_window(credential_store=FakeStore(read_error=OSError("denied")))
    w.edit_gateway_credentials("Taylor")
    assert dialogs.messages[-1][2] is True and "denied" in dialogs.messages[-1][1]

    dialogs.answers["Taylor"] = Credentials("t", "p")
    w2 = make_window(credential_store=FakeStore(write_error=OSError("full")))
    w2.edit_gateway_credentials("Taylor")
    assert dialogs.messages[-1][2] is True and "full" in dialogs.messages[-1][1]


def test_credentials_dialog_needs_both_fields_and_masks_the_password(qtbot):
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLineEdit
    from gateway_start import Credentials
    from qt_panel.gateway_actions import CredentialsDialog

    d = CredentialsDialog("Rosa", "rosa-user")
    qtbot.addWidget(d)
    assert d.user_edit.text() == "rosa-user"
    assert d.password_edit.echoMode() == QLineEdit.Password
    ok = d.buttons.button(QDialogButtonBox.Ok)
    qtbot.mouseClick(ok, Qt.LeftButton)
    assert d.result() != QDialog.Accepted and d.credentials() is None
    qtbot.keyClicks(d.password_edit, "s3cret")
    qtbot.mouseClick(ok, Qt.LeftButton)
    assert d.credentials() == Credentials("rosa-user", "s3cret")


def test_credentials_dialog_cancel_returns_nothing(qtbot):
    from PySide6.QtWidgets import QDialogButtonBox
    from qt_panel.gateway_actions import CredentialsDialog

    d = CredentialsDialog("Taylor")
    qtbot.addWidget(d)
    qtbot.keyClicks(d.user_edit, "t")
    qtbot.keyClicks(d.password_edit, "p")
    qtbot.mouseClick(d.buttons.button(QDialogButtonBox.Cancel), Qt.LeftButton)
    assert d.credentials() is None


# ── 值班（並行模式只顯示）───────────────────────────────────────────

def test_watch_card_shows_the_three_watches_and_their_next_run(make_window):
    taipei = MutableClock(datetime(2026, 9, 29, 20, 10))         # 週二晚上
    w = make_window(taipei_now=taipei)
    text = w.dashboard.watch_text()
    for name in ("Rosa poll", "TY check", "Morning check"):
        assert name in text
    assert "下次 今天 21:00" in text                              # Rosa poll
    assert "下次 明天 20:05" in text                              # TY check（週三）
    assert "下次 明天 07:00" in text                              # Morning check
    assert "Tk Panel 值班中" in text


def test_watch_card_warns_when_the_tk_panel_is_not_open(make_window):
    w = make_window(tk_panel_running=lambda: False)
    assert "沒有人值班" in w.dashboard.watch_text()


def test_watch_next_run_moves_on_with_the_minute_tick(make_window):
    taipei = MutableClock(datetime(2026, 9, 29, 20, 10))
    w = make_window(taipei_now=taipei)
    taipei.now = datetime(2026, 9, 29, 21, 0, 30)
    w.tick_minute()
    assert "下次 今天 21:30" in w.dashboard.watch_text()


def test_parallel_mode_runs_no_watch_worker(make_window, source, qtbot):
    """並行模式：值班時間到了也不跑任何 worker、不發 Telegram，只更新顯示。"""
    taipei = MutableClock(datetime(2026, 9, 29, 20, 59))
    w = make_window(taipei_now=taipei)
    qtbot.waitUntil(lambda: not [t for t in threading.enumerate()
                                 if t.name.startswith("portfolio-")], timeout=3000)
    before = {t.ident for t in threading.enumerate()}      # 開啟時的權益查詢已結束
    for now in (datetime(2026, 9, 29, 21, 0, 5),                # Rosa poll
                datetime(2026, 9, 30, 20, 5, 5)):               # TY check
        taipei.now = now
        w.tick_minute()
    assert {t.ident for t in threading.enumerate()} <= before
    # 唯一的連線動作是 #147 的交易日 07:00 Gateway 重新檢查（09-30 07:00 已過）；
    # Rosa poll、TY check 本身沒有做任何事
    assert source.rechecks == 1


# ── 今日排程、資料日期、重新整理 ──────────────────────────────────────

def test_today_jobs_card_shows_each_job_state(make_window):
    from qt_panel.dashboard_data import JobRow
    rows = [JobRow("update", "04:30", "資料更新", "ok"),
            JobRow("daily", "04:40", "每日報告", "problem", "Rosa ❌ 失敗"),
            JobRow("rebalance", "21:00", "調倉", "pending")]
    w = make_window(today_jobs=lambda now: rows)
    text = w.dashboard.jobs_text()
    assert "04:30 資料更新 完成" in text
    assert "每日報告 有狀況 Rosa ❌ 失敗" in text                    # 有狀況直接列原因
    assert "21:00 調倉 未到時間" in text


def test_today_jobs_are_read_with_the_local_clock(make_window):
    seen = []
    make_window(today_jobs=lambda now: seen.append(now) or [])
    assert seen[0] == datetime(2026, 9, 29, 20, 30, 5)


def test_manual_refresh_updates_data_date_and_last_update_time(make_window, source, qtbot):
    data = {"date": "2026-09-25"}
    clock = MutableClock(datetime(2026, 9, 29, 20, 30, 5))
    w = make_window(read_data_date=lambda: data["date"], clock=clock)
    assert w.dashboard.data_date_text() == "2026-09-25"
    assert w.dashboard.last_refresh_text() == "2026-09-29 20:30:05"

    data["date"] = "2026-09-28"
    clock.now = datetime(2026, 9, 29, 21, 5, 0)
    qtbot.mouseClick(w.dashboard.refresh_button, Qt.LeftButton)
    assert w.dashboard.data_date_text() == "2026-09-28"
    assert w.dashboard.last_refresh_text() == "2026-09-29 21:05:00"
    assert source.rechecks == 1                                  # 同 Tk：一併重新檢查 Gateway
    assert "重新整理完成" in w.dashboard.log_text()


def test_refresh_survives_a_broken_reader(make_window):
    def broken():
        raise OSError("disk gone")
    w = make_window(read_data_date=broken)
    w.refresh()
    assert "資料日期讀取失敗" in w.dashboard.log_text()
    assert "重新整理完成" in w.dashboard.log_text()


# ── Output Log ───────────────────────────────────────────────────────

def test_log_lines_from_a_worker_thread_reach_the_output_log(make_window, qtbot):
    w = make_window()
    t = threading.Thread(target=w.log, args=("hello from worker",))
    t.start()
    t.join()
    qtbot.waitUntil(lambda: "hello from worker" in w.dashboard.log_text(), timeout=2000)
    assert "20:30:05  hello from worker" in w.dashboard.log_text()


def test_gateway_going_offline_is_logged_with_the_reason(make_window, source):
    w = make_window()
    source.on_state("Rosa", "connecting", "")
    source.on_state("Rosa", "online", "")
    source.on_state("Rosa", "offline", "heartbeat lost")
    source.on_state("Rosa", "offline", "heartbeat lost")          # 同狀態不重複寫
    log = w.dashboard.log_text()
    assert "Rosa 已連上" in log
    assert log.count("Rosa 離線 — heartbeat lost") == 1


def test_clear_empties_the_output_log(make_window, qtbot):
    w = make_window()
    w.log("something")
    qtbot.mouseClick(w.dashboard.clear_log_button, Qt.LeftButton)
    assert w.dashboard.log_text() == ""


def test_post_login_checks_stop_when_the_panel_quits(make_window, qtbot, monkeypatch):
    import gateway_start as gs
    monkeypatch.setattr(gs, "POST_LOGIN_FIRST_CHECK_SEC", 0.05)
    src = OnlineAfterRecheck({"Taylor", "Rosa", "Yvonne"})
    w = make_window(gateway_source=src, tray_factory=None)
    _start_and_wait(w, qtbot)
    w.close()
    qtbot.wait(200)
    assert src.rechecks == 0


# ── Portfolio（#149）────────────────────────────────────────────────

def _done(w, name):
    state = w.portfolio._states.get(name)
    return state is not None and not state.running


def _wait_portfolio(w, qtbot, *names):
    names = names or ("Taylor", "Rosa", "Yvonne")
    qtbot.waitUntil(lambda: all(_done(w, n) for n in names), timeout=3000)


def test_overview_shows_equity_and_unrealized_pnl_for_every_account(make_window, qtbot,
                                                                   portfolio):
    w = make_window()
    _wait_portfolio(w, qtbot)
    assert sorted(portfolio.calls) == ["Rosa", "Taylor", "Yvonne"]      # 開啟時自動讀一次
    text = w.portfolio.card_text("Taylor")
    assert "$50,000" in text and "未平倉損益" in text
    assert "+$" in text                                                   # 未平倉損益為正
    assert "Leverage" in text and "Cushion 35%" in text
    assert "IB Live [Taylor]" in text                                     # 讀數來源
    assert "$110,000" in w.portfolio.card_text("Yvonne")


def test_clicking_an_account_shows_its_holdings_with_strategy_owner(make_window, qtbot):
    w = make_window()
    _wait_portfolio(w, qtbot)
    assert w.portfolio.selected_account() == "Taylor"
    rows = [(r[0], r[1]) for r in w.portfolio.detail.holdings.visible_rows()]
    # 預設順序同 Tk：ETF → 個股 → 手動，各組市值大的在前
    assert rows == [("SR", "XLK"), ("SkewLS", "SIJ"), ("SP500", "AAPL"), ("手動", "BTC")]

    qtbot.mouseClick(w.portfolio.card("Rosa"), Qt.LeftButton)
    assert w.portfolio.selected_account() == "Rosa"
    assert [r[1] for r in w.portfolio.detail.holdings.visible_rows()] == ["XLB"]
    assert "Rosa" in w.portfolio.detail.header_text()
    assert "Report Date: 2026-09-30  [IB Live [Rosa]]" in w.portfolio.detail.header_text()


def test_detail_shows_the_shared_metric_cards_and_donut(make_window, qtbot):
    w = make_window()
    _wait_portfolio(w, qtbot)
    w.portfolio.select("Yvonne")
    metrics = w.portfolio.detail.metrics_text()
    for title in ("Net Asset Value", "Unrealized P&L", "Margin Cushion",
                  "Strategy Exposure", "Account Exposure"):
        assert title in metrics
    assert "8.0%" in metrics                                              # 緩衝 <10%
    w.portfolio.select("Taylor")
    legend = w.portfolio.detail.legend_text()
    assert "XLK" in legend and "Other" in legend and "Cash" in legend


def test_owner_filter_and_numeric_sort(make_window, qtbot):
    w = make_window()
    _wait_portfolio(w, qtbot)
    holdings = w.portfolio.detail.holdings
    qtbot.mouseClick(holdings.filter_buttons["手動"], Qt.LeftButton)
    assert [r[1] for r in holdings.visible_rows()] == ["BTC"]
    qtbot.mouseClick(holdings.filter_buttons["全部"], Qt.LeftButton)
    holdings.table.sortItems(6, Qt.DescendingOrder)                       # 損益（數值排序）
    assert [r[1] for r in holdings.visible_rows()] == ["AAPL", "XLK", "BTC", "SIJ"]


def test_one_account_failing_only_affects_its_own_card(make_window, qtbot, portfolio):
    portfolio.results["Rosa"] = RuntimeError("connection refused")
    w = make_window()
    _wait_portfolio(w, qtbot)
    rosa = w.portfolio.card_text("Rosa")
    assert "讀取失敗" in rosa and "connection refused" in rosa and "$—" in rosa
    assert "$50,000" in w.portfolio.card_text("Taylor")
    assert "$110,000" in w.portfolio.card_text("Yvonne")
    assert "Rosa] 權益／持倉讀取失敗" in w.dashboard.log_text()


def test_a_failed_refresh_keeps_the_last_reading_marked_as_cached(make_window, qtbot,
                                                                    portfolio):
    w = make_window()
    _wait_portfolio(w, qtbot)
    portfolio.results["Taylor"] = RuntimeError("timeout")
    w.refresh_portfolio()
    qtbot.waitUntil(lambda: w.portfolio._states["Taylor"].error == "timeout", timeout=3000)
    card = w.portfolio.card_text("Taylor")
    assert "$50,000" in card                                              # 上次讀數照樣顯示
    assert "快取 · IB Live [Taylor]" in card
    assert "timeout" in card
    assert "快取" in w.portfolio.detail.header_text()


def test_refresh_button_queries_every_account_again(make_window, qtbot, portfolio):
    w = make_window()
    _wait_portfolio(w, qtbot)
    portfolio.calls.clear()
    qtbot.mouseClick(w.portfolio.refresh_button, Qt.LeftButton)
    qtbot.waitUntil(lambda: all(_done(w, n) and w.portfolio._states[n].refresh_source == "manual"
                                for n in ("Taylor", "Rosa", "Yvonne")), timeout=3000)
    assert sorted(portfolio.calls) == ["Rosa", "Taylor", "Yvonne"]
    assert "讀取帳戶權益與持倉" in w.dashboard.log_text()


def test_portfolio_is_read_again_after_gateway_login(make_window, qtbot, portfolio,
                                                      no_post_login_wait):
    src = OnlineAfterRecheck({"Taylor", "Rosa", "Yvonne"})
    w = make_window(gateway_source=src)
    _wait_portfolio(w, qtbot)
    portfolio.calls.clear()
    _start_and_wait(w, qtbot)
    qtbot.waitUntil(lambda: len(portfolio.calls) == 3, timeout=3000)   # 同 Tk：登入後 Refresh IB


def test_portfolio_queries_stop_when_the_panel_quits(make_window, qtbot, portfolio):
    w = make_window(tray_factory=None)
    _wait_portfolio(w, qtbot)
    w.close()
    portfolio.calls.clear()
    assert w.refresh_portfolio() == ()
    assert portfolio.calls == []


def test_readonly_portfolio_query_uses_its_own_client_id(fake_config, monkeypatch):
    """新 Panel 查詢用 69/79/89：心跳佔著 29/39/49，Tk 查詢用 25/35/45（#149）。"""
    import ib_fetcher
    from qt_panel.portfolio_source import fetch_readonly_portfolio

    seen = []
    monkeypatch.setattr(ib_fetcher, "fetch_portfolio",
                        lambda ctx, readonly=False: seen.append((ctx, readonly)) or {})
    fetch_readonly_portfolio("Rosa")
    ctx, readonly = seen[0]
    assert ctx.client_id == 79 and readonly is True
    assert ctx.name == "Rosa"
