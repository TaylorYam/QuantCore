"""新 PySide6 Panel 骨架（#147，ADR-0012）。

從主視窗操作，外部依賴（Gateway 狀態來源、時鐘、系統匣）一律注入假的；
不連 IB、不讀真實 config。沒裝 PySide6／pytest-qt 的環境（例如 live 機器
直接跑 pytest）整檔略過。
"""
from __future__ import annotations

import ast
import os
import threading
from datetime import datetime
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


@pytest.fixture
def source():
    return FakeGatewaySource()


@pytest.fixture
def make_window(qtbot, source):
    from qt_panel.main_window import MainWindow
    from qt_panel.mode import PanelMode

    def make(**kw):
        kw.setdefault("accounts", ACCOUNTS)
        kw.setdefault("mode", PanelMode.PARALLEL)
        kw.setdefault("gateway_source", source)
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


def test_portfolio_and_trading_are_placeholders_that_point_to_tk(make_window):
    w = make_window()
    for page in ("Portfolio", "Trading"):
        assert "Tk Panel" in w.page_text(page)


def test_recheck_button_asks_the_source_to_probe_again(make_window, source, qtbot):
    w = make_window()
    qtbot.mouseClick(w.dashboard.recheck_button, Qt.LeftButton)
    assert source.rechecks == 1


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
