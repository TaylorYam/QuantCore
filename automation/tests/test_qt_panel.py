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
    import pyqtgraph  # noqa: F401
    import pytestqt  # noqa: F401
else:
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt  # noqa: E402

ACCOUNTS = [
    {"name": "Taylor", "port": 4001},
    {"name": "Rosa", "port": 4002},
    {"name": "Yvonne", "port": 4003},
]


class FakeGatewaySource:
    """假的 Gateway 來源；也回答值班要的心跳狀態與重連。"""

    def __init__(self):
        self.on_state = None
        self.started = 0
        self.stopped = 0
        self.rechecks = 0
        self.online = {"Taylor": True, "Rosa": True, "Yvonne": True}
        self.restarted = []

    def is_online(self, name):
        return self.online.get(name, False)

    def restart(self, name):
        self.restarted.append(name)
        return True

    def ready(self):
        return self.on_state is not None

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
    from portfolio_snapshot import Holding
    return Holding(symbol=sym, shares=sh, avg_cost=cost, mkt_px=px, mkt_val=sh * px,
                   upnl=(px - cost) * sh, upnl_pct=(px / cost - 1) * 100 if cost else 0,
                   name=name)


def portfolio_dict(name, nav=100_000.0, cushion=35.0, etf=(), stocks=(), other=()):
    """一個帳戶的 PortfolioSnapshot（#208）。"""
    from portfolio_snapshot import PortfolioSnapshot
    etf, stocks, other = tuple(etf), tuple(stocks), tuple(other)
    strat = sum(x.mkt_val for x in etf + stocks)
    oth = sum(x.mkt_val for x in other)
    return PortfolioSnapshot(
        date="2026-09-30", source=f"IB Live [{name}]", available=True, nav=nav,
        cushion=cushion, etf_holdings=etf, stock_holdings=stocks, other_holdings=other,
        strategy_mktval=strat, other_mktval=oth, total_mktval=strat + oth,
        total_upnl=sum(x.upnl for x in etf + stocks + other))


def with_fields(portfolio, name, **fields):
    """FakePortfolio 裡某帳戶的快照換成多了這些欄位的新快照（快照不可變）。"""
    from dataclasses import replace
    portfolio.results[name] = replace(portfolio.results[name], **fields)


class FakePortfolio:
    """portfolio_fetch 的假實作：每帳戶回傳設定好的 PortfolioSnapshot，或丟出例外。"""

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


def nav_series(start, days, first=100_000.0, step=100.0):
    import pandas as pd
    index = pd.date_range(start, periods=days, freq="D")
    return pd.Series([first + i * step for i in range(days)], index=index)


class FakeExtras:
    """權益序列、策略設定、最近成交的假讀取：每帳戶回傳設定好的值，或丟出例外。"""

    def __init__(self):
        self.equity = {
            "Taylor": nav_series("2025-01-01", 600),                     # 到 2026-08-23
            "Rosa": nav_series("2026-06-01", 100, first=80_000.0, step=-50.0),
            "Yvonne": nav_series("2026-09-01", 1, first=110_000.0),
        }
        self.config = {
            "Taylor": {"alloc_sr": 0.80, "alloc_sp500": 0.50, "alloc_skewls": 0.0},
            "Rosa": {"alloc_sr": 0.50, "alloc_sp500": 0.30, "alloc_skewls": 0.20},
            "Yvonne": {"alloc_sr": 0.75, "alloc_sp500": 0.45, "alloc_skewls": 0.30},
        }
        from ledger import TradeRow
        from trades_display import trade_line
        self.trades = {
            "Taylor": [
                trade_line(TradeRow("2026-09-29 21:30:05", "XLK", "BUY", 10, 230.5, "SR",
                                    "Filled", False)),
                trade_line(TradeRow("2026-09-28 21:30:06", "AAPL", "SELL", 3, 0.0, "SP500",
                                    "DRY RUN", True)),
            ],
            "Rosa": [],
            "Yvonne": [],
        }
        from ib_flex import FlexNotConfigured, FlowsResult
        fetched = datetime(2026, 9, 30, 9, 0)
        self.flows = {
            "Taylor": FlowsResult([], fetched),
            "Rosa": FlowsResult([], fetched),
            "Yvonne": FlexNotConfigured("未設定 IB Flex（accounts.json 的 flex_token／flex_query_id）"),
        }
        self.calls = []
        self.trade_allocs = []      # trades_fetch 收到的 (帳戶, alloc)（#231）
        self.saves = []
        self.save_error = None

    def _get(self, kind, table, name):
        self.calls.append((kind, name))
        result = table[name]
        if isinstance(result, Exception):
            raise result
        return result

    def equity_fetch(self, name):
        return self._get("equity", self.equity, name)

    def config_fetch(self, name):
        return self._get("config", self.config, name)

    def flows_fetch(self, name):
        return self._get("flows", self.flows, name)

    def trades_fetch(self, name, alloc=None):
        self.trade_allocs.append((name, alloc))
        return self._get("trades", self.trades, name)

    def config_save(self, name, updates):
        """假的 Allocation 寫入（#220）：記下來並改掉讀取值，不碰 accounts.json。"""
        if self.save_error:
            raise self.save_error
        self.saves.append((name, dict(updates)))
        self.config[name] = {**self.config[name], **updates}


class FakeDialogs:
    def __init__(self, answers=None):
        self.answers = answers or {}
        self.asked = []
        self.messages = []
        self.confirms = []
        self.confirm_answers = []
        self.allocation_answers = []
        self.allocation_asked = []

    def ask_allocation(self, account, current):
        self.allocation_asked.append((account, tuple(current)))
        return self.allocation_answers.pop(0) if self.allocation_answers else None

    def ask_credentials(self, account, username=""):
        self.asked.append((account, username, threading.current_thread() is threading.main_thread()))
        return self.answers.get(account)

    def show_message(self, title, text, *, error=False):
        self.messages.append((title, text, error))

    def confirm(self, title, text, *, warning=False):
        self.confirms.append((title, text, warning))
        return self.confirm_answers.pop(0) if self.confirm_answers else True


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
def extras():
    return FakeExtras()


@pytest.fixture
def make_window(qtbot, source, ports, dialogs, portfolio, extras, tmp_path):
    from types import SimpleNamespace as NS

    from qt_panel.main_window import MainWindow
    from qt_panel.order_target import OrderFiles
    from qt_panel.watch import WatchPorts
    from tests.fake_broker import FakeBroker
    from tests.test_qt_order_target import live_ctx

    def make(**kw):
        # 送單目標的紀錄檔一律放 tmp_path：測試碰不到 live 目錄的實盤紀錄
        kw.setdefault("live_files", OrderFiles(tmp_path / "w_live_orders.sqlite3",
                                               tmp_path / "w_live_attribution.sqlite3"))
        kw.setdefault("accounts", ACCOUNTS)
        kw.setdefault("gateway_source", source)
        # 值班與送單一律用假的：不連 IB、不發 Telegram、手動單不開背景對帳
        kw.setdefault("watch_ports", WatchPorts(telegram=lambda m: None,
                                                cold_connect=lambda n: None,
                                                sleep=lambda s: None, start_delay_ms=0))
        kw.setdefault("broker", FakeBroker(managed=["U001"]))
        kw.setdefault("account_ctx_loader", live_ctx)
        kw.setdefault("manual_order_service", lambda **_kw: NS(
            has_active_orders=lambda: False, has_pending_notifications=lambda: False,
            list_orders=lambda **_: []))
        kw.setdefault("gateway_ports_factory", lambda ask: setattr(ports, "ask", ask) or ports)
        kw.setdefault("credential_store", FakeStore())
        kw.setdefault("portfolio_fetch", portfolio)
        kw.setdefault("equity_fetch", extras.equity_fetch)
        kw.setdefault("cash_flows_fetch", extras.flows_fetch)
        kw.setdefault("strategy_config_fetch", extras.config_fetch)
        kw.setdefault("allocation_save", extras.config_save)
        kw.setdefault("trades_fetch", extras.trades_fetch)
        kw.setdefault("portfolio_next_refresh", lambda now: now + timedelta(days=1))
        kw.setdefault("dialogs", dialogs)
        kw.setdefault("read_data_date", lambda: "2026-09-28")
        kw.setdefault("today_jobs", lambda now: [])
        kw.setdefault("taipei_now", lambda: datetime(2026, 9, 29, 20, 30))
        kw.setdefault("clock", lambda: datetime(2026, 9, 29, 20, 30, 5))
        kw.setdefault("symbol_loader", lambda: None)
        w = MainWindow(**kw)
        qtbot.addWidget(w)
        w.start()
        w.show()
        return w

    return make


# ── 主視窗 ───────────────────────────────────────────────────────────

def test_header_shows_the_clock(make_window):
    w = make_window()
    text = w.header_text()
    assert "2026-09-29" in text and "20:30:05" in text
    assert "並行模式" not in text


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


def test_recheck_button_asks_the_source_to_probe_again(make_window, source, qtbot):
    w = make_window()
    qtbot.mouseClick(w.dashboard.recheck_button, Qt.LeftButton)
    assert source.rechecks == 1


class MutableClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def test_watch_telegram_and_trade_log_live_only_in_watch_and_order_target():
    """值班、Telegram、trade_log、策略送單的模組只能出現在值班的 watch.py 與送單目標
    order_target.py（#183）；畫面與其他模組碰不到。"""
    forbidden = {"monitor_engine", "notify", "rebalance_ib", "excel_logger"}
    telegram_calls = {"deliver_notices", "notify_new_pauses", "strategy_takeover.notify"}
    allowed = {"watch.py", "order_target.py"}
    package = Path(__file__).resolve().parents[1] / "qt_panel"
    offenders = {}
    for path in package.glob("*.py"):
        if path.name in allowed:
            continue
        found = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names} & forbidden
            elif isinstance(node, ast.ImportFrom) and node.module:
                found |= {node.module.split(".")[0]} & forbidden
                if node.module == "strategy_takeover":
                    found |= {f"strategy_takeover.{a.name}" for a in node.names} & telegram_calls
            elif isinstance(node, ast.Attribute):
                name = node.attr
                if isinstance(node.value, ast.Name) and node.attr == "notify":
                    name = f"{node.value.id}.notify"
                found |= {name} & telegram_calls
        if found:
            offenders[path.name] = found
    assert offenders == {}


def test_only_the_allocation_writer_touches_accounts_json():
    """新 Panel 寫 accounts.json 只有一條路（#220）：portfolio_source.write_strategy_allocations。"""
    import account_utils

    writers = {"update_account_config"}                        # account_utils 唯一的寫入函式
    assert all(hasattr(account_utils, w) for w in writers)
    package = Path(__file__).resolve().parents[1] / "qt_panel"
    users = {}
    for path in package.glob("*.py"):
        names = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.alias):
                names.add(node.name.split(".")[-1])
        if names & writers:
            users[path.name] = names & writers
    assert users == {"portfolio_source.py": writers}


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


# ── 單一實例 ─────────────────────────────────────────────────────────

def test_single_instance_lock_and_show_signal_names():
    from qt_panel.app import lockfile_path, show_signal_path

    assert lockfile_path().name == "quantcore_qt_panel.lock"
    assert show_signal_path().name == "quantcore_qt_panel.show"


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

def test_a_fourth_account_shows_up_in_trading_gateway_and_watch(make_window, tmp_path):
    """#211：加帳戶只改 accounts.json — 有 gateway_dir 就進 Gateway 帳密／一鍵啟動清單，
    手動單預設可下（Trading 帳戶選單），ty_check 的進值班卡。"""
    from account_utils import capabilities_for

    accounts = ACCOUNTS + [{"name": "Zoe", "port": 4004}]
    caps = capabilities_for([{"name": a["name"]} for a in ACCOUNTS] + [
        {"name": "Zoe", "gateway_dir": str(tmp_path), "ty_check": True, "color": "#123456"}])
    w = make_window(accounts=accounts, capabilities=caps)
    assert [a.text() for a in w.dashboard.credentials_menu.actions()] == [
        "Taylor", "Rosa", "Yvonne", "Zoe"]
    assert w.trading.manual is not None
    box = w.trading.manual.account_box
    assert [box.itemText(i) for i in range(box.count())] == ["Taylor", "Rosa", "Yvonne", "Zoe"]
    assert "Taylor／Yvonne／Zoe" in w.dashboard.watch_text()
    assert "Rosa poll" in w.dashboard.watch_text()


def test_an_account_without_manual_orders_is_not_in_trading(make_window):
    from account_utils import capabilities_for

    caps = capabilities_for([{"name": "Taylor"}, {"name": "Rosa", "manual_orders": False},
                             {"name": "Yvonne"}])
    w = make_window(capabilities=caps)
    box = w.trading.manual.account_box
    assert [box.itemText(i) for i in range(box.count())] == ["Taylor", "Yvonne"]


def test_no_module_hard_codes_the_account_names():
    """#211：帳戶能力只在 accounts.json（缺欄位的現狀預設只在 account_utils 一處）。"""
    import re

    automation = Path(__file__).resolve().parents[1]
    # 名字並列的 tuple／list、大寫鍵表、拿 Rosa 當條件
    pattern = re.compile(r"""[(\[]\s*["']Taylor["']\s*,\s*["'](Rosa|Yvonne)["']"""
                         r"""|["'](TAYLOR|ROSA|YVONNE)["']\s*:|==\s*["']Rosa["']"""
                         r"""|is_online\(["']Rosa["']\)|["'](Taylor|Rosa|Yvonne)["']\s*:\s*["']#""")
    offenders = []
    for path in list(automation.glob("*.py")) + list((automation / "qt_panel").glob("*.py")):
        if path.name == "account_utils.py":
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert offenders == []


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


# ── 值班卡 ───────────────────────────────────────────────────────────

def test_watch_card_shows_the_three_watches_and_their_next_run(make_window):
    taipei = MutableClock(datetime(2026, 9, 29, 20, 10))         # 週二晚上
    w = make_window(taipei_now=taipei)
    text = w.dashboard.watch_text()
    for name in ("Rosa poll", "TY check", "Morning check"):
        assert name in text
    assert "下次 今天 21:00" in text                              # Rosa poll
    assert "下次 明天 20:05" in text                              # TY check（週三）
    assert "下次 明天 07:00" in text                              # Morning check
    assert "Panel 值班中" in text and "Tk" not in text


def test_watch_next_run_moves_on_with_the_minute_tick(make_window):
    taipei = MutableClock(datetime(2026, 9, 29, 20, 10))
    w = make_window(taipei_now=taipei)
    taipei.now = datetime(2026, 9, 29, 21, 0, 30)
    w.tick_minute()
    assert "下次 今天 21:30" in w.dashboard.watch_text()


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
    assert source.rechecks == 1                                  # 一併重新檢查 Gateway
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
    assert "Account Exposure" in text and "Cushion 35%" in text
    assert "IB Live [Taylor]" in text                                     # 讀數來源
    assert "$110,000" in w.portfolio.card_text("Yvonne")


def test_clicking_an_account_shows_its_holdings_with_strategy_owner(make_window, qtbot):
    w = make_window()
    _wait_portfolio(w, qtbot)
    assert w.portfolio.selected_account() == "Taylor"
    rows = [(r[0], r[1]) for r in w.portfolio.detail.holdings.visible_rows()]
    # 預設順序：ETF → 個股 → 手動，各組市值大的在前
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
    qtbot.waitUntil(lambda: len(portfolio.calls) == 3, timeout=3000)   # 登入後讀一次權益與持倉


def test_portfolio_queries_stop_when_the_panel_quits(make_window, qtbot, portfolio):
    w = make_window(tray_factory=None)
    _wait_portfolio(w, qtbot)
    w.close()
    portfolio.calls.clear()
    assert w.refresh_portfolio() == ()
    assert portfolio.calls == []


def test_portfolio_query_uses_the_panel_identity_readonly(fake_config, monkeypatch):
    """查詢用 ROLE_PANEL（Rosa 35）唯讀：不和心跳 ROLE_MONITOR（37）、排程撞號。"""
    import portfolio_snapshot
    from qt_panel.portfolio_source import fetch_panel_portfolio

    seen = []
    monkeypatch.setattr(portfolio_snapshot, "fetch_snapshot",
                        lambda ctx, readonly=False: seen.append((ctx, readonly))
                        or portfolio_snapshot.PortfolioSnapshot.unavailable("x"))
    fetch_panel_portfolio("Rosa")
    ctx, readonly = seen[0]
    assert ctx.client_id == 35 and readonly is True
    assert ctx.name == "Rosa"


def test_manual_holdings_that_collided_with_a_universe_show_as_manual(make_window, qtbot,
                                                                      portfolio):
    with_fields(portfolio, "Taylor", manual_symbols={"AAPL": "手動 30 股，IB 30 股"})
    w = make_window()
    _wait_portfolio(w, qtbot)
    rows = {r[1]: r[0] for r in w.portfolio.detail.holdings.visible_rows()}
    assert rows["AAPL"] == "手動" and rows["XLK"] == "SR"
    table = w.portfolio.detail.holdings.table
    aapl = next(r for r in range(table.rowCount()) if table.item(r, 1).text() == "AAPL")
    assert "碰撞" in table.item(aapl, 0).toolTip()


def test_long_sector_etfs_show_as_skewls_when_sr_is_off(make_window, qtbot, portfolio):
    """#181：SR 0% 時做多的類股 ETF 歸 SkewLS，篩選 SR 是空的。"""
    with_fields(portfolio, "Taylor", strategy_alloc=(0.0, 1.0))
    w = make_window()
    _wait_portfolio(w, qtbot)
    holdings = w.portfolio.detail.holdings
    rows = {r[1]: r[0] for r in holdings.visible_rows()}
    assert rows["XLK"] == "SkewLS" and rows["SIJ"] == "SkewLS" and rows["AAPL"] == "SP500"
    holdings.set_filter("SR")
    assert holdings.visible_rows() == []
    holdings.set_filter("SkewLS")
    assert {r[1] for r in holdings.visible_rows()} == {"XLK", "SIJ"}


def test_shared_sector_etfs_match_both_strategy_filters(make_window, qtbot, portfolio):
    with_fields(portfolio, "Taylor", strategy_alloc=(0.5, 0.3))
    w = make_window()
    _wait_portfolio(w, qtbot)
    holdings = w.portfolio.detail.holdings
    for key in ("SR", "SkewLS"):
        holdings.set_filter(key)
        assert ("SR+SkewLS", "XLK") in {r[:2] for r in holdings.visible_rows()}


def test_portfolio_reads_carry_the_accounts_allocation(fake_config, monkeypatch):
    import portfolio_snapshot
    from account_ctx import AccountCtx
    from account_utils import ROLE_PANEL
    from qt_panel import portfolio_source

    monkeypatch.setattr(portfolio_snapshot, "fetch_snapshot",
                        lambda ctx, readonly=False: portfolio_snapshot.PortfolioSnapshot(
                            date="2026-10-08", source="IB Live [Rosa]", available=True))
    monkeypatch.setattr(portfolio_source, "read_manual_symbols",
                        lambda name: {"manual_symbols": {}})
    ctx = AccountCtx.from_name("Rosa", ROLE_PANEL)
    expected = (ctx.alloc_sr, ctx.alloc_skewls)
    assert portfolio_source.fetch_panel_portfolio("Rosa").strategy_alloc == expected


def test_manual_attribution_read_failure_is_flagged_not_fatal(make_window, qtbot, portfolio):
    with_fields(portfolio, "Taylor", manual_error="手動歸屬紀錄讀取失敗：locked")
    w = make_window()
    _wait_portfolio(w, qtbot)
    assert "$50,000" in w.portfolio.card_text("Taylor")
    assert "手動歸屬讀取失敗" in w.portfolio.detail.header_text()


def test_manual_symbols_are_read_from_the_attribution_ledger_read_only(tmp_path):
    import sqlite3
    from qt_panel.portfolio_source import read_manual_symbols

    ledger = tmp_path / "manual_attribution.sqlite3"
    assert read_manual_symbols("Taylor", ledger) == {"manual_symbols": {}}   # 尚未啟用
    with sqlite3.connect(ledger) as conn:
        conn.execute("CREATE TABLE pauses (account_name TEXT, symbol TEXT, reason TEXT)")
        conn.execute("INSERT INTO pauses VALUES ('Taylor', 'AAPL', 'why')")
        conn.execute("INSERT INTO pauses VALUES ('Rosa', 'MSFT', 'other')")
    conn.close()
    before = ledger.read_bytes()
    assert read_manual_symbols("Taylor", ledger) == {"manual_symbols": {"AAPL": "why"}}
    assert ledger.read_bytes() == before                                     # 不寫入

    broken = tmp_path / "broken.sqlite3"
    broken.write_bytes(b"not a database")
    assert "manual_error" in read_manual_symbols("Taylor", broken)


# ── 樣式（#162）─────────────────────────────────────────────────────

def test_popup_menus_have_an_opaque_theme_background(qtbot):
    """系統匣右鍵與「Gateway 帳密」選單是獨立的彈出視窗：背景透明會被 Windows 畫成黑色。"""
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QMenu
    from qt_panel import theme

    menu = QMenu()
    qtbot.addWidget(menu)
    menu.setStyleSheet(theme.STYLESHEET)
    menu.addAction(QAction("顯示 Panel", menu))
    menu.addSeparator()
    menu.addAction(QAction("離開", menu))
    menu.adjustSize()
    pixel = menu.grab().toImage().pixelColor(menu.width() // 2, 3)
    assert pixel.alpha() == 255
    assert pixel.name() == theme.SURFACE



@pytest.mark.parametrize("style_name", ["windows11", "windowsvista"])
def test_combo_box_dropdowns_have_an_opaque_theme_background(qtbot, style_name):
    """下拉清單也是獨立的彈出視窗（#169）：背景透明時 Windows 原生樣式畫成黑底
    （離線測試預設的 Fusion 不會，所以指定實機用的樣式）。"""
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QComboBox, QStyleFactory
    from qt_panel import theme

    style = QStyleFactory.create(style_name)
    if style is None:
        pytest.skip(f"{style_name} 樣式只在 Windows 上有")
    combo = QComboBox()
    qtbot.addWidget(combo)
    combo.setStyle(style)
    combo.setStyleSheet(theme.STYLESHEET)
    combo.addItems(["Taylor", "Rosa", "Yvonne"])
    combo.show()
    combo.showPopup()
    view = combo.view()
    qtbot.waitUntil(view.isVisible, timeout=2000)
    popup = view.window()
    image = popup.grab().toImage()
    row = view.visualRect(view.model().index(2, 0))          # 沒選取的那一列，取右側空白處
    in_row = view.viewport().mapTo(popup, QPoint(row.right() - 4, row.center().y()))
    for point in (in_row, QPoint(popup.width() // 2, popup.height() - 2)):
        pixel = image.pixelColor(point)
        assert (pixel.alpha(), pixel.name()) == (255, theme.SURFACE), point
    combo.hidePopup()


def test_chip_corner_radius_fits_the_chip_height(qtbot):
    # 圓角超過高度一半時 Qt 整個不畫圓角（#178 換成正黑體後 Chip 變矮就踩到）
    import re

    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QPushButton

    from qt_panel import theme

    radius = int(re.search(r"QPushButton#Chip \{[^}]*border-radius: (\d+)px",
                           theme.STYLESHEET).group(1))
    chip = QPushButton("1Y")
    chip.setObjectName("Chip")
    chip.setFont(QFont(theme.UI_FONT, 10))
    chip.setStyleSheet(theme.STYLESHEET)
    qtbot.addWidget(chip)
    assert radius * 2 <= chip.sizeHint().height()


def test_scroll_bars_are_slim_theme_bars_without_arrows(qtbot):
    """捲軸用主題的細圓角樣式（#171），不是 Windows 原生捲軸。"""
    from PySide6.QtWidgets import QLabel, QScrollArea, QStyleFactory
    from qt_panel import theme

    area = QScrollArea()
    qtbot.addWidget(area)
    style = QStyleFactory.create("windows11") or QStyleFactory.create("fusion")
    area.setStyle(style)
    area.setStyleSheet(theme.STYLESHEET)
    area.setWidget(QLabel("\n".join(str(i) for i in range(200))))
    area.resize(300, 300)
    area.show()
    bar = area.verticalScrollBar()
    qtbot.waitUntil(lambda: bar.maximum() > 0, timeout=2000)
    assert bar.width() <= 10
    image = bar.grab().toImage()
    colors = {image.pixelColor(x, y).name()
              for x in range(image.width()) for y in range(image.height())}
    assert theme.BORDER_STRONG in colors

# ── 權益曲線、策略參數、最近成交（#150）──────────────────────────────

def _wait_extras(w, qtbot):
    qtbot.waitUntil(lambda: not w.account_extras_loading(), timeout=3000)


def test_equity_chart_plots_the_selected_accounts_daily_nav(make_window, qtbot, extras):
    w = make_window()
    _wait_extras(w, qtbot)
    chart = w.portfolio.detail.equity
    expected = [(f"{d:%Y-%m-%d}", v) for d, v in extras.equity["Taylor"].items()]
    assert chart.points() == expected                                   # 同一份權益序列
    w.portfolio.select("Rosa")
    assert chart.points()[0] == ("2026-06-01", 80_000.0)
    assert len(chart.points()) == 100


def test_range_buttons_set_the_visible_period(make_window, qtbot):
    w = make_window()
    _wait_extras(w, qtbot)
    chart = w.portfolio.detail.equity
    assert chart.checked_range() == "1Y"                                # 預設近一年
    qtbot.mouseClick(chart.range_buttons["1M"], Qt.LeftButton)
    start, end = chart.x_range_dates()
    assert "2026-07-20" <= start <= "2026-07-23" and end >= "2026-08-23"
    qtbot.mouseClick(chart.range_buttons["YTD"], Qt.LeftButton)
    assert "2025-12-20" <= chart.x_range_dates()[0] <= "2026-01-01"
    qtbot.mouseClick(chart.range_buttons["全部"], Qt.LeftButton)
    assert chart.x_range_dates()[0] <= "2025-01-01"
    assert chart.checked_range() == "全部"


def _wheel(widget, steps=1):
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication

    pos = QPointF(widget.width() / 2, widget.height() / 2)
    event = QWheelEvent(pos, widget.mapToGlobal(pos), QPoint(0, 0), QPoint(0, 120 * steps),
                        Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(widget, event)
    return event


def _span(chart):
    x0, x1 = chart.viewbox.viewRange()[0]
    return x1 - x0


def test_wheel_zooms_the_time_axis_only_after_clicking_the_chart(make_window, qtbot):
    w = make_window()
    _wait_extras(w, qtbot)
    w.nav_button("Portfolio").click()
    chart = w.portfolio.detail.equity
    qtbot.mouseClick(chart.range_buttons["全部"], Qt.LeftButton)
    view = chart.plot.viewport()
    before = _span(chart)
    _wheel(view, 2)                                                     # 只是經過：不縮放（#177）
    assert not chart.zoom_enabled()
    assert _span(chart) == pytest.approx(before)
    assert chart.checked_range() == "全部"
    assert "點一下圖" in chart.hover_text()

    qtbot.mouseClick(view, Qt.LeftButton)                               # 點一下才啟用
    assert chart.zoom_enabled()
    assert "已啟用" in chart.hover_text()
    _wheel(view, 2)
    assert _span(chart) < before * 0.9                                  # 放大了
    assert chart.checked_range() is None


def test_leaving_the_chart_turns_wheel_zoom_off_again(make_window, qtbot):
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    w = make_window()
    _wait_extras(w, qtbot)
    w.nav_button("Portfolio").click()
    chart = w.portfolio.detail.equity
    view = chart.plot.viewport()
    qtbot.mouseClick(view, Qt.LeftButton)
    assert chart.zoom_enabled()
    QApplication.sendEvent(view, QEvent(QEvent.Leave))
    assert not chart.zoom_enabled()
    before = _span(chart)
    _wheel(view, 2)
    assert _span(chart) == pytest.approx(before)


def test_wheel_over_an_idle_chart_is_left_for_the_page_to_scroll(make_window, qtbot):
    # 沒被接受的滾輪事件 Qt 會往外層送（頁面捲動）；測試送的是合成事件，Qt 不會自動
    # 往外送，所以這裡只驗證圖沒有吃掉它
    w = make_window()
    _wait_extras(w, qtbot)
    w.nav_button("Portfolio").click()
    view = w.portfolio.detail.equity.plot.viewport()
    assert not _wheel(view, -1).isAccepted()
    qtbot.mouseClick(view, Qt.LeftButton)
    assert _wheel(view, -1).isAccepted()


def test_portfolio_page_fits_a_maximized_full_hd_window(make_window, qtbot):
    w = make_window()
    _wait_extras(w, qtbot)
    w.nav_button("Portfolio").click()
    w.resize(1915, 1020)                                                # 1920×1080 最大化（#177）
    page = w.portfolio.scroll
    qtbot.waitUntil(lambda: page.viewport().height() > 600, timeout=2000)
    qtbot.wait(50)
    assert page.verticalScrollBar().maximum() == 0
    assert page.horizontalScrollBar().maximum() == 0


def test_hovering_the_chart_shows_the_date_and_equity(make_window, qtbot, extras):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    w = make_window()
    _wait_extras(w, qtbot)
    w.nav_button("Portfolio").click()
    chart = w.portfolio.detail.equity
    qtbot.mouseClick(chart.range_buttons["1M"], Qt.LeftButton)
    view = chart.plot.viewport()
    pos = QPointF(chart.plot.mapFromScene(chart.scene_pos_of("2026-08-10")))
    event = QMouseEvent(QMouseEvent.MouseMove, pos, view.mapToGlobal(pos),
                        Qt.NoButton, Qt.NoButton, Qt.NoModifier)
    QApplication.sendEvent(view, event)
    nav = extras.equity["Taylor"]["2026-08-10"]
    assert chart.hover_text().startswith("2026-08-10")
    assert f"${nav:,.0f}" in chart.hover_text()


def test_overview_cards_show_a_sixty_day_sparkline(make_window, qtbot):
    w = make_window()
    _wait_extras(w, qtbot)
    taylor = w.portfolio.card("Taylor").sparkline.points()
    assert taylor[0][0] == "2026-06-24" and taylor[-1][0] == "2026-08-23"   # 近 60 天
    assert "近 60 天業績 +" in w.portfolio.card_text("Taylor")
    assert "近 60 天業績 -" in w.portfolio.card_text("Rosa")
    assert len(w.portfolio.card("Yvonne").sparkline.points()) == 1


def test_clicking_the_sparkline_still_selects_the_account(make_window, qtbot):
    w = make_window()
    _wait_extras(w, qtbot)
    w.nav_button("Portfolio").click()
    spark = w.portfolio.card("Rosa").sparkline
    qtbot.mouseClick(w.portfolio.card("Rosa"), Qt.LeftButton,
                     pos=spark.geometry().center())
    assert w.portfolio.selected_account() == "Rosa"


def test_equity_read_failure_only_affects_that_account(make_window, qtbot, extras):
    extras.equity["Rosa"] = OSError("portfolio_tracker.xlsx locked")
    w = make_window()
    _wait_extras(w, qtbot)
    w.portfolio.select("Rosa")
    assert "權益紀錄讀取失敗" in w.portfolio.detail.equity.empty_text()
    assert w.portfolio.card("Rosa").sparkline.points() == []
    assert "Rosa] 權益紀錄讀取失敗" in w.dashboard.log_text()
    w.portfolio.select("Taylor")
    assert w.portfolio.detail.equity.points()
    assert w.portfolio.detail.equity.empty_text() == ""


def test_strategy_params_show_each_allocation_and_the_leverage(make_window, qtbot):
    from qt_panel import theme

    w = make_window()
    _wait_extras(w, qtbot)
    params = w.portfolio.detail.params
    text = params.text()
    for part in ("SR", "80%", "SP500", "50%", "Skew LS", "0%", "Leverage",
                 "130% (1.3x)", "Borrow: 30%"):
        assert part in text
    assert params.value_color("130% (1.3x)") == theme.ORANGE
    w.portfolio.select("Yvonne")
    assert "150% (1.5x)" in params.text() and "Borrow: 50%" in params.text()
    w.portfolio.select("Rosa")
    assert "100% (1.0x)" in params.text() and "No leverage" in params.text()
    assert params.value_color("100% (1.0x)") == theme.GREEN


# ── 調整 Allocation（#220）─────────────────────────────────────────

def _edit_params(w, qtbot):
    w.nav_button("Portfolio").click()
    qtbot.mouseClick(w.portfolio.detail.params.edit_button, Qt.LeftButton)


def test_adjusting_allocation_saves_only_this_accounts_alloc_fields(
        make_window, qtbot, extras, dialogs):
    w = make_window()
    _wait_extras(w, qtbot)
    w.portfolio.select("Yvonne")
    dialogs.allocation_answers = [(0.70, 0.40, 0.30)]
    _edit_params(w, qtbot)
    assert dialogs.allocation_asked == [("Yvonne", (0.75, 0.45, 0.30))]   # 目前設定當初始值
    assert extras.saves == [("Yvonne", {"alloc_sr": 0.70, "alloc_sp500": 0.40,
                                        "alloc_skewls": 0.30})]
    (title, text, warning), = dialogs.confirms
    assert "Yvonne" in text and "140% (1.4x)" in text and not warning
    params = w.portfolio.detail.params
    assert "70%" in params.text() and "140% (1.4x)" in params.text()   # 寫完重讀刷新
    assert "[OK] [Yvonne] 策略參數已儲存" in w.dashboard.log_text()
    w.portfolio.select("Taylor")
    assert "130% (1.3x)" in params.text()                              # 其他帳戶不變


def test_allocation_dialog_starts_from_the_file_not_the_last_read(
        make_window, qtbot, extras, dialogs):
    """開對話框前重讀設定：別處改過 Allocation 時初始值與首次啟用判斷才正確。"""
    w = make_window()
    _wait_extras(w, qtbot)
    extras.config["Taylor"] = {"alloc_sr": 0.60, "alloc_sp500": 0.50, "alloc_skewls": 0.10}
    dialogs.allocation_answers = [(0.60, 0.50, 0.20)]
    _edit_params(w, qtbot)
    assert dialogs.allocation_asked == [("Taylor", (0.60, 0.50, 0.10))]
    (title, text, warning), = dialogs.confirms
    assert not warning                                                  # Skew LS 已經開過
    assert "130% (1.3x)" in w.portfolio.detail.params.text()                # 存的是 60/50/20


def test_allocation_reread_failure_is_shown_without_opening_the_dialog(
        make_window, qtbot, extras, dialogs):
    w = make_window()
    _wait_extras(w, qtbot)
    extras.config["Taylor"] = OSError("accounts.json missing")
    _edit_params(w, qtbot)
    assert dialogs.allocation_asked == [] and extras.saves == []
    (title, text, error), = dialogs.messages
    assert error and "accounts.json missing" in text
    assert "設定讀取失敗" in w.portfolio.detail.params.text()


def test_cancelling_the_allocation_dialog_or_the_confirmation_saves_nothing(
        make_window, qtbot, extras, dialogs):
    w = make_window()
    _wait_extras(w, qtbot)
    _edit_params(w, qtbot)                                              # 對話框按取消
    assert dialogs.confirms == [] and extras.saves == []
    dialogs.allocation_answers = [(0.60, 0.50, 0.0)]
    dialogs.confirm_answers = [False]
    _edit_params(w, qtbot)                                              # 確認視窗按否
    assert len(dialogs.confirms) == 1 and extras.saves == []
    assert "130% (1.3x)" in w.portfolio.detail.params.text()


def test_unchanged_allocation_is_not_written(make_window, qtbot, extras, dialogs):
    w = make_window()
    _wait_extras(w, qtbot)
    dialogs.allocation_answers = [(0.80, 0.50, 0.0)]
    _edit_params(w, qtbot)
    assert dialogs.confirms == [] and extras.saves == []
    assert "沒有變更" in w.dashboard.log_text()


def test_allocation_above_250_percent_is_refused(make_window, qtbot, extras, dialogs):
    w = make_window()
    _wait_extras(w, qtbot)
    dialogs.allocation_answers = [(1.0, 1.0, 0.55)]
    _edit_params(w, qtbot)
    assert extras.saves == [] and dialogs.confirms == []
    (title, text, error), = dialogs.messages
    assert error and "250%" in text


def test_first_enable_of_skewls_asks_with_a_short_selling_warning(
        make_window, qtbot, extras, dialogs):
    w = make_window()
    _wait_extras(w, qtbot)
    dialogs.allocation_answers = [(0.80, 0.50, 0.10)]
    _edit_params(w, qtbot)
    (title, text, warning), = dialogs.confirms
    assert warning and "放空 XLC" in text
    assert extras.saves == [("Taylor", {"alloc_sr": 0.80, "alloc_sp500": 0.50,
                                        "alloc_skewls": 0.10})]


def test_allocation_save_failure_is_shown_and_the_card_keeps_old_values(
        make_window, qtbot, extras, dialogs):
    extras.save_error = OSError("accounts.json is locked")
    w = make_window()
    _wait_extras(w, qtbot)
    dialogs.allocation_answers = [(0.60, 0.50, 0.0)]
    _edit_params(w, qtbot)
    (title, text, error), = dialogs.messages
    assert error and "accounts.json is locked" in text
    assert "130% (1.3x)" in w.portfolio.detail.params.text()
    assert "[X] [Taylor] 策略參數儲存失敗" in w.dashboard.log_text()


def test_allocation_cannot_be_adjusted_until_the_config_is_read(make_window, qtbot, extras):
    extras.config["Taylor"] = ValueError("Account 'Taylor' not found")
    w = make_window()
    _wait_extras(w, qtbot)
    params = w.portfolio.detail.params
    assert not params.edit_button.isEnabled()
    w.portfolio.select("Rosa")
    assert params.edit_button.isEnabled()


def test_allocation_dialog_moves_in_5_percent_steps_and_shows_the_leverage(qtbot):
    from qt_panel.account_panels import AllocationDialog

    d = AllocationDialog("Taylor", (0.80, 0.50, 0.0))
    qtbot.addWidget(d)
    assert d.values() == (0.80, 0.50, 0.0)
    assert "130% (1.3x)" in d.text() and "Borrow: 30%" in d.text()
    sr = d.sliders[0]
    assert (sr.minimum(), sr.maximum(), sr.singleStep()) == (0, 20, 1)   # 每格 5%
    sr.setValue(sr.value() - 2)
    assert d.values() == (0.70, 0.50, 0.0)
    assert "70%" in d.text() and "120% (1.2x)" in d.text()
    assert d.save_button.isEnabled()


def test_allocation_dialog_blocks_saving_above_250_percent(qtbot):
    from qt_panel.account_panels import AllocationDialog

    d = AllocationDialog("Taylor", (1.0, 1.0, 0.50))
    qtbot.addWidget(d)
    assert d.save_button.isEnabled()                                    # 剛好 250% 可以
    d.sliders[2].setValue(11)
    assert not d.save_button.isEnabled()
    assert "255%" in d.text() and "250%" in d.text()


def test_allocation_writer_only_writes_alloc_fields(monkeypatch):
    import account_utils
    from qt_panel.portfolio_source import write_strategy_allocations

    writes = []
    monkeypatch.setattr(account_utils, "update_account_config",
                        lambda name, updates: writes.append((name, updates)))
    write_strategy_allocations("Rosa", {"alloc_sr": 0.5, "alloc_sp500": 0.3, "alloc_skewls": 0.2})
    assert writes == [("Rosa", {"alloc_sr": 0.5, "alloc_sp500": 0.3, "alloc_skewls": 0.2})]
    for bad in ({"alloc_sr": 0.5, "leverage": 3}, {"port": 4001},
                {"alloc_sr": 1.0, "alloc_sp500": 1.0, "alloc_skewls": 0.6},
                {"alloc_sr": -0.1}, {"alloc_sr": 1.05}):
        with pytest.raises(ValueError):
            write_strategy_allocations("Rosa", bad)
    assert len(writes) == 1


def test_strategy_config_read_failure_is_shown(make_window, qtbot, extras):
    extras.config["Taylor"] = ValueError("Account 'Taylor' not found")
    w = make_window()
    _wait_extras(w, qtbot)
    assert "設定讀取失敗" in w.portfolio.detail.params.text()
    assert "130%" not in w.portfolio.detail.params.text()


def test_recent_trades_show_rows_with_full_date(make_window, qtbot, extras):
    from qt_panel import theme

    w = make_window()
    _wait_extras(w, qtbot)
    trades = w.portfolio.detail.trades
    assert trades.rows() == [
        ("2026-09-29 21:30:05", "XLK", "BUY", "10", "$230.50", "SR", "Filled"),
        ("2026-09-28 21:30:06", "AAPL", "SELL", "3", "—", "Stocks", "DRY RUN [DRY]"),
    ]
    for got, tr in zip(trades.rows(), extras.trades["Taylor"]):
        assert got[1:] == tr.cells()[1:]                                 # 同一套格式
    assert trades.table.item(1, 2).foreground().color().name() == theme.RED
    assert trades.table.item(1, 6).foreground().color().name() == theme.AMBER
    w.portfolio.select("Rosa")
    assert trades.rows() == [] and trades.empty_text() == "近 7 天沒有成交"


def test_refresh_rereads_equity_params_and_trades(make_window, qtbot, extras):
    w = make_window()
    _wait_extras(w, qtbot)
    extras.calls.clear()
    extras.config["Taylor"] = {"alloc_sr": 0.60, "alloc_sp500": 0.20, "alloc_skewls": 0.0}
    qtbot.mouseClick(w.portfolio.refresh_button, Qt.LeftButton)
    _wait_extras(w, qtbot)
    assert sorted(extras.calls) == sorted(
        (kind, name) for kind in ("equity", "config", "trades", "flows")
        for name in ("Taylor", "Rosa", "Yvonne"))
    assert "80% (0.8x)" in w.portfolio.detail.params.text()


def test_account_extras_are_not_read_after_quit(make_window, qtbot, extras):
    w = make_window(tray_factory=None)
    _wait_extras(w, qtbot)
    w.close()
    extras.calls.clear()
    assert w.load_account_extras() is False
    assert extras.calls == []


def test_strategy_allocations_are_read_without_other_config_fields(fake_config):
    from qt_panel.portfolio_source import read_strategy_allocations

    assert read_strategy_allocations("Yvonne") == {
        "alloc_sr": 0.75, "alloc_sp500": 0.45, "alloc_skewls": 0.30}


def test_equity_ranges_never_start_before_the_first_day():
    import pandas as pd
    from qt_panel.equity import range_start, recent

    first, last = pd.Timestamp("2026-03-15"), pd.Timestamp("2026-09-30")
    assert range_start("1M", first, last) == pd.Timestamp("2026-08-30")
    assert range_start("3M", first, last) == pd.Timestamp("2026-06-30")
    assert range_start("YTD", first, last) == first
    assert range_start("1Y", first, last) == first
    assert range_start("全部", first, last) == first
    assert range_start("YTD", pd.Timestamp("2025-01-01"), last) == pd.Timestamp("2026-01-01")
    s = nav_series("2026-01-01", 100)
    assert recent(s).index[0] == s.index[-1] - pd.Timedelta(days=60)


# ── 權益／業績切換（#166）────────────────────────────────────────────

def deposit_case(extras):
    """Rosa：每天 +1%，2026-08-10（美國日）入金 10,000 → 台北 08-11 的快照權益跳上去。"""
    from datetime import date

    import pandas as pd
    from ib_flex import FlowsResult
    from performance import CashFlow

    index = pd.date_range("2026-08-01", periods=20, freq="D")
    values, nav = [], 10_000.0
    for day in index:
        nav *= 1.01
        if day == pd.Timestamp("2026-08-11"):
            nav += 10_000.0
        values.append(nav)
    extras.equity["Rosa"] = pd.Series(values, index=index)
    extras.flows["Rosa"] = FlowsResult([CashFlow(date(2026, 8, 10), 10_000.0)],
                                       datetime(2026, 9, 30, 9, 0))


def _perf(w, qtbot, name="Taylor"):
    _wait_extras(w, qtbot)
    w.nav_button("Portfolio").click()
    w.portfolio.select(name)
    chart = w.portfolio.detail.equity
    qtbot.mouseClick(chart.mode_buttons["業績"], Qt.LeftButton)
    return chart


def test_performance_mode_excludes_deposits(make_window, qtbot, extras):
    deposit_case(extras)
    w = make_window()
    chart = _perf(w, qtbot, "Rosa")
    assert chart.mode() == "業績"
    qtbot.mouseClick(chart.range_buttons["全部"], Qt.LeftButton)
    perf = [v for _, v in chart.points()]
    steps = [(b + 100) / (a + 100) - 1 for a, b in zip(perf, perf[1:])]
    assert steps == pytest.approx([0.01] * 19)                        # 入金那天也只有 +1%
    assert perf[0] == pytest.approx(0.0)
    assert "已排除出入金（IB Flex，09-30 09:00 下載）" in chart.note_text()

    qtbot.mouseClick(chart.mode_buttons["權益"], Qt.LeftButton)
    navs = [v for _, v in chart.points()]
    assert max(b / a - 1 for a, b in zip(navs, navs[1:])) > 0.5       # 權益有入金的跳動
    assert chart.note_text() == ""


def test_performance_starts_each_range_at_zero(make_window, qtbot):
    w = make_window()
    chart = _perf(w, qtbot)
    qtbot.mouseClick(chart.range_buttons["1M"], Qt.LeftButton)
    points = dict(chart.points())
    assert points["2026-07-23"] == pytest.approx(0.0)                # 1M 起點（08-23 往前一個月）
    # 沒有出入金時，業績就是權益的報酬
    assert points["2026-08-23"] == pytest.approx((159_900 / 156_800 - 1) * 100)
    assert points["2026-07-01"] < 0                                   # 起點之前相對起點


def test_hovering_in_performance_mode_shows_the_return(make_window, qtbot):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    w = make_window()
    chart = _perf(w, qtbot)
    qtbot.mouseClick(chart.range_buttons["1M"], Qt.LeftButton)
    view = chart.plot.viewport()
    pos = QPointF(chart.plot.mapFromScene(chart.scene_pos_of("2026-08-10")))
    QApplication.sendEvent(view, QMouseEvent(QMouseEvent.MouseMove, pos, view.mapToGlobal(pos),
                                             Qt.NoButton, Qt.NoButton, Qt.NoModifier))
    expected = (158_600 / 156_800 - 1) * 100
    assert chart.hover_text() == f"2026-08-10　業績 {expected:+.2f}%　距前高 +0.0%"


def test_performance_without_flex_settings_explains_and_equity_still_works(make_window, qtbot):
    w = make_window()
    chart = _perf(w, qtbot, "Yvonne")
    assert "業績需要出入金紀錄" in chart.empty_text() and "flex_token" in chart.empty_text()
    assert chart.points() == []
    assert "Yvonne] 出入金讀取失敗" in w.dashboard.log_text()
    # 沒設定 Flex 是常態：重新讀取不再重複記
    qtbot.mouseClick(w.portfolio.refresh_button, Qt.LeftButton)
    _wait_extras(w, qtbot)
    assert w.dashboard.log_text().count("Yvonne] 出入金讀取失敗") == 1
    w.portfolio.select("Taylor")                                     # 其他帳戶的業績照常
    assert chart.points() and chart.empty_text() == ""
    w.portfolio.select("Yvonne")
    qtbot.mouseClick(chart.mode_buttons["權益"], Qt.LeftButton)
    assert chart.points() == [("2026-09-01", 110_000.0)]


def test_a_stale_flex_cache_is_labelled(make_window, qtbot, extras):
    from ib_flex import FlowsResult

    extras.flows["Taylor"] = FlowsResult([], datetime(2026, 9, 29, 9, 0),
                                         "IB Flex 拒絕請求（1018）：Too many requests.")
    w = make_window()
    chart = _perf(w, qtbot)
    assert chart.points()
    assert "出入金下載失敗，使用 09-29 09:00 的快取" in chart.note_text()
    assert "[!] [Taylor] 出入金下載失敗，改用快取" in w.dashboard.log_text()


def test_overview_change_uses_performance_when_flows_are_known(make_window, qtbot, extras):
    deposit_case(extras)
    w = make_window()
    _wait_extras(w, qtbot)
    rosa = w.portfolio.card_text("Rosa")
    expected = (1.01 ** 19 - 1) * 100                                 # 入金不算賺錢
    assert f"近 60 天業績 +{expected:.1f}%" in rosa


def test_overview_change_without_flows_says_it_is_equity(make_window, qtbot, extras):
    from ib_flex import FlexNotConfigured

    extras.flows["Taylor"] = FlexNotConfigured("未設定 IB Flex")
    w = make_window()
    _wait_extras(w, qtbot)
    assert "近 60 天權益 +" in w.portfolio.card_text("Taylor")


def test_cash_flows_are_read_with_the_accounts_flex_settings(fake_config, monkeypatch, tmp_path):
    import account_utils
    import ib_flex
    from ib_flex import FlexNotConfigured
    from qt_panel.portfolio_source import load_account_cash_flows

    monkeypatch.setattr(ib_flex, "CACHE_FILE", tmp_path / "flows.json")
    with pytest.raises(FlexNotConfigured):
        load_account_cash_flows("Rosa")                               # 範例設定沒有 flex 欄位

    accounts = account_utils.load_accounts()
    for a in accounts:
        a.update(flex_token="T", flex_query_id="Q")
    monkeypatch.setattr(account_utils, "load_accounts", lambda: accounts)
    seen = []
    monkeypatch.setattr(ib_flex, "load_cash_flows",
                        lambda name, cfg, cache, now: seen.append((name, cfg, cache.path)) or "ok")
    assert load_account_cash_flows("Rosa") == "ok"
    name, cfg, path = seen[0]
    assert name == "Rosa" and cfg["account"] == "U12770188" and cfg["flex_query_id"] == "Q"
    assert path == tmp_path / "flows.json"
