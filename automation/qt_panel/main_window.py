"""新 Panel 主視窗：頂端列＋左側導覽＋三個畫面（#147）。

外部依賴全部從建構子注入（Gateway 狀態來源、時鐘、系統匣工廠、叫回視窗的
訊號檔），測試用假的驅動，不連 IB。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton, QStackedWidget,
    QVBoxLayout, QWidget,
)

from morning_check import next_morning_check_time

from qt_panel import theme
from qt_panel.dashboard import Dashboard
from qt_panel.gateway_status import GatewaySource
from qt_panel.mode import PanelMode
from qt_panel.widgets import label, pill, visible_text

PAGES = ["Dashboard", "Portfolio", "Trading"]
PLACEHOLDER = {
    "Portfolio": "Portfolio 畫面製作中。並行期間請在 Tk Panel 查看帳戶權益與持倉。",
    "Trading": "Trading 畫面製作中。並行期間下單請用 Tk Panel。",
}
SHOW_SIGNAL_POLL_MS = 2000
AUTO_RECHECK_POLL_MS = 60_000


def _taipei_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Taipei"))


class _GatewayBridge(QObject):
    """背景 thread 的 Gateway 回呼 → Qt 主執行緒（跨執行緒的 signal 會自動排隊）。"""
    changed = Signal(str, str, str)


class _Header(QFrame):
    def __init__(self, mode: PanelMode, mode_note: str):
        super().__init__()
        self.setObjectName("Header")
        self.setFixedHeight(52)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 0, 20, 0)
        lay.setSpacing(10)
        lay.addWidget(label(f"Quant<span style='color:{theme.ACCENT}'>Core</span>", name="Brand"))
        lay.addSpacing(8)
        if mode is PanelMode.PARALLEL:
            lay.addWidget(pill("並行模式 · 只看不下單", theme.WARN_TEXT, theme.WARN_BG))
        if mode_note:
            lay.addWidget(label(mode_note, name="Faint"))
        lay.addStretch(1)
        self.clock = label(name="Clock")
        lay.addWidget(self.clock)


def _placeholder(text: str) -> QWidget:
    page = QWidget()
    lay = QVBoxLayout(page)
    msg = label(text, name="Placeholder")
    msg.setAlignment(Qt.AlignCenter)
    msg.setWordWrap(True)
    lay.addWidget(msg)
    return page


class MainWindow(QMainWindow):
    quit_requested = Signal()

    def __init__(self, *, accounts: list[dict], mode: PanelMode,
                 gateway_source: GatewaySource,
                 clock: Callable[[], datetime] = datetime.now,
                 taipei_now: Callable[[], datetime] = _taipei_now,
                 tray_factory=None, show_signal: Path | None = None,
                 mode_note: str = ""):
        super().__init__()
        self._source = gateway_source
        self._clock = clock
        self._taipei_now = taipei_now
        self._tray_factory = tray_factory
        self._tray = None
        self._show_signal = show_signal
        self._running = False
        self.setWindowTitle("QuantCore Panel")
        self.resize(1400, 900)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._header = _Header(mode, mode_note)
        root.addWidget(self._header)

        body = QHBoxLayout()
        body.setSpacing(0)
        nav = QFrame()
        nav.setObjectName("Nav")
        nav.setFixedWidth(184)
        nav_lay = QVBoxLayout(nav)
        nav_lay.setContentsMargins(0, 16, 0, 16)
        nav_lay.setSpacing(2)
        self._stack = QStackedWidget()
        self._nav_group = QButtonGroup(self, exclusive=True)
        self._nav: dict[str, QPushButton] = {}
        self.dashboard = Dashboard(accounts, on_recheck=gateway_source.recheck)
        self._pages: dict[str, QWidget] = {"Dashboard": self.dashboard}
        for name in PAGES[1:]:
            self._pages[name] = _placeholder(PLACEHOLDER[name])
        for name in PAGES:
            button = QPushButton(name)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            page = self._pages[name]
            button.clicked.connect(lambda _=False, p=page: self._stack.setCurrentWidget(p))
            self._nav_group.addButton(button)
            self._nav[name] = button
            nav_lay.addWidget(button)
            self._stack.addWidget(page)
        nav_lay.addStretch(1)
        self._nav["Dashboard"].setChecked(True)
        body.addWidget(nav)
        body.addWidget(self._stack, 1)
        root.addLayout(body, 1)

        self._bridge = _GatewayBridge()
        self._bridge.changed.connect(self.dashboard.set_gateway_state)
        self._clock_timer = QTimer(self, interval=1000, timeout=self._update_clock)
        self._signal_timer = QTimer(self, interval=SHOW_SIGNAL_POLL_MS,
                                    timeout=self.check_show_signal)
        # 並行期間不值班，但比照 Morning check 每個交易日 07:00 重新檢查一次 Gateway
        # （每天 06:00 自動重啟後心跳已斷、不會自己重連）。每分鐘比對一次時間，
        # 電腦睡眠醒來也不會漏掉。
        self._recheck_timer = QTimer(self, interval=AUTO_RECHECK_POLL_MS,
                                     timeout=self.tick_auto_recheck)
        self._next_recheck = next_morning_check_time(self._taipei_now())
        self.dashboard.set_next_auto_recheck(self._next_recheck)
        self._update_clock()

    # ── 生命週期 ─────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self._source.start(self._bridge.changed.emit)
        self._clock_timer.start()
        self._recheck_timer.start()
        if self._show_signal is not None:
            self._signal_timer.start()
        if self._tray_factory is not None:
            self._tray = self._tray_factory(on_show=self.show_window, on_quit=self.quit_app)
            self._tray.show()

    def has_tray(self) -> bool:
        return self._tray is not None

    def shutdown(self) -> None:
        """停心跳、停計時器、收系統匣。可重複呼叫。"""
        if not self._running:
            return
        self._running = False
        self._clock_timer.stop()
        self._signal_timer.stop()
        self._recheck_timer.stop()
        self._source.stop()
        if self._tray is not None:
            self._tray.hide()

    def quit_app(self) -> None:
        self.shutdown()
        self.hide()
        self.quit_requested.emit()

    def closeEvent(self, event) -> None:
        # 有系統匣：按 X 只是藏起來，Gateway 狀態繼續更新；真正離開走系統匣選單
        if self._tray is not None and self._running:
            event.ignore()
            self.hide()
            return
        self.shutdown()
        event.accept()
        self.quit_requested.emit()

    def show_window(self) -> None:
        self.showMaximized()
        self.raise_()
        self.activateWindow()

    def check_show_signal(self) -> None:
        """第二次啟動會留下訊號檔：把這個視窗叫回來。"""
        if self._show_signal is None or not self._show_signal.exists():
            return
        try:
            self._show_signal.unlink()
        except OSError:
            pass
        self.show_window()

    def tick_auto_recheck(self) -> None:
        if not self._running:
            return
        now = self._taipei_now()
        if now < self._next_recheck:
            return
        self._source.recheck()
        self._next_recheck = next_morning_check_time(now)
        self.dashboard.set_next_auto_recheck(self._next_recheck)

    def _update_clock(self) -> None:
        self._header.clock.setText(self._clock().strftime("%Y-%m-%d  %H:%M:%S"))

    # ── 讀取（測試與狀態檢查用）─────────────────────────────────────

    def header_text(self) -> str:
        return visible_text(self._header)

    def nav_labels(self) -> list[str]:
        return list(self._nav)

    def nav_button(self, name: str) -> QPushButton:
        return self._nav[name]

    def current_page(self) -> str:
        current = self._stack.currentWidget()
        return next(n for n, p in self._pages.items() if p is current)

    def page_text(self, name: str) -> str:
        return " ".join(l.text() for l in self._pages[name].findChildren(QLabel))
