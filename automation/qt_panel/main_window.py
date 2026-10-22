"""Panel 主視窗：頂端列＋左側導覽＋三個畫面（#147、#148、#149、#150、#151）。

外部依賴全部從建構子注入（Gateway 狀態來源、Gateway 啟動的 Ports、認證管理員、
查權益／持倉的函式、權益序列／策略設定／最近成交的讀取與 Allocation 寫入、資料日期／
今日排程的讀取、時鐘、系統匣工廠、叫回視窗的訊號檔、送單的 Broker），測試用假的
驅動，不連 IB、不讀真實檔案。

Portfolio 頁的讀數全部來自 `qt_panel.portfolio_page`（#231）；Allocation 的編輯流程在
`qt_panel.allocation_flow`，Gateway 狀態轉換的規則在 `gateway_status.gateway_transition`。
這裡只接線。

值班（#154）由 `qt_panel.watch` 跑（Rosa poll／TY check／Morning check，既有的
MonitorEngine）。送單（#151、#183）：這裡建一次送單目標（order_target，各帳戶自己的
實盤 Gateway）交給 Trading；帳戶身分、紀錄、state、通知都在送單目標裡。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton, QStackedWidget,
    QVBoxLayout, QWidget,
)

from account_utils import AccountCapabilities, capabilities_for, remote_gateway_account

from qt_panel import allocation_flow, dashboard_data, theme
from qt_panel.dashboard import Dashboard
from qt_panel.dashboard_data import JobRow
from qt_panel.gateway_actions import (
    CredentialStore, Dialogs, GatewayStartFlow, PortsFactory, QtDialogs,
)
from qt_panel.gateway_status import GatewaySource, gateway_transition
from qt_panel.order_target import OrderFiles, OrderTarget
from qt_panel.portfolio import PortfolioPage
from qt_panel.portfolio_page import SNAPSHOT, PageChange, PageFetchers, PortfolioPageSource
from qt_panel.trading import TradingPage
from qt_panel.trading_common import MainThread
from qt_panel.watch import QtWatch, WatchPorts
from qt_panel.widgets import label, visible_text

PAGES = ["Dashboard", "Portfolio", "Trading"]
SHOW_SIGNAL_POLL_MS = 2000
MINUTE_POLL_MS = 60_000


def _taipei_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Taipei"))


class _Bridge(QObject):
    """背景 thread 的回呼 → Qt 主執行緒（跨執行緒的 signal 會自動排隊）。"""
    gateway_changed = Signal(str, str, str)
    log = Signal(str)


class _Header(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("Header")
        self.setFixedHeight(52)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 0, 20, 0)
        lay.setSpacing(10)
        lay.addWidget(label(f"Quant<span style='color:{theme.ACCENT}'>Core</span>", name="Brand"))
        lay.addStretch(1)
        self.clock = label(name="Clock")
        lay.addWidget(self.clock)


def _load_account_ctx(name: str, role: int):
    from account_ctx import AccountCtx
    return AccountCtx.from_name(name, role)


class MainWindow(QMainWindow):
    quit_requested = Signal()

    def __init__(self, *, accounts: list[dict],
                 gateway_source: GatewaySource,
                 gateway_ports_factory: PortsFactory,
                 credential_store: CredentialStore,
                 portfolio_fetch: Callable[[str], dict],
                 equity_fetch: Callable[[str], object],
                 cash_flows_fetch: Callable[[str], object],
                 strategy_config_fetch: Callable[[str], dict],
                 trades_fetch: Callable[[str, object], list],
                 allocation_save: Callable[[str, dict], None] | None = None,
                 portfolio_next_refresh: Callable[[datetime], datetime] | None = None,
                 dialogs: Dialogs | None = None,
                 read_data_date: Callable[[], str] = dashboard_data.read_data_date,
                 today_jobs: Callable[[datetime], list[JobRow]] | None = None,
                 clock: Callable[[], datetime] = datetime.now,
                 taipei_now: Callable[[], datetime] = _taipei_now,
                 tray_factory=None, show_signal: Path | None = None,
                 broker=None,
                 manual_order_service: Callable[..., object] | None = None,
                 symbol_loader: Callable | None = None,
                 live_files: OrderFiles | None = None,
                 account_ctx_loader: Callable | None = None,
                 watch_ports: WatchPorts | None = None,
                 capabilities: Sequence[AccountCapabilities] | None = None):
        """capabilities：各帳戶的能力（accounts.json，#211）；None = 依 accounts 的名字
        套現狀預設（不讀檔）。trades_fetch(name, alloc)：alloc 是同一輪讀到的
        (alloc_sr, alloc_skewls)，見 portfolio_page。"""
        super().__init__()
        caps = list(capabilities) if capabilities is not None else capabilities_for(accounts)
        self._remote_account = remote_gateway_account(caps)
        self._ty_check_accounts = tuple(c.name for c in caps if c.ty_check)
        self._manual_accounts = [c.name for c in caps if c.manual_orders]
        gateway_accounts = [c.name for c in caps if c.can_start_gateway]
        colors = {c.name: c.color for c in caps if c.color}
        self._source = gateway_source
        self._watch_ports = watch_ports or WatchPorts()
        self._watch: QtWatch | None = None
        self._manual_service = None          # 手動單對帳在背景持續跑
        self._credential_store = credential_store
        self._dialogs = dialogs if dialogs is not None else QtDialogs(self)
        self._read_data_date = read_data_date
        names = [a["name"] for a in accounts]
        self._names = names
        self._allocation_save = allocation_save
        self._today_jobs = today_jobs or (lambda now: dashboard_data.today_jobs(now, names))
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
        self._header = _Header()
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
        self.dashboard = Dashboard(
            [{**a, "color": colors.get(a["name"])} for a in accounts],
            on_recheck=self.recheck_gateways,
            on_start_gateways=self.start_gateways,
            on_edit_credentials=self.edit_gateway_credentials,
            credential_accounts=gateway_accounts, on_refresh=self.refresh)
        self.portfolio = PortfolioPage(
            names, on_refresh=self.refresh_portfolio,
            on_edit_allocation=self.edit_allocation if allocation_save is not None else None,
            colors=colors)
        # broker 只給測試換成 in-memory fake（tests/fake_broker.py）
        target = OrderTarget(files=live_files,
                             account_ctx_loader=account_ctx_loader or _load_account_ctx,
                             broker=broker)
        self.trading = self._build_trading(target, manual_order_service, symbol_loader)
        self._pages: dict[str, QWidget] = {"Dashboard": self.dashboard,
                                           "Portfolio": self.portfolio,
                                           "Trading": self.trading}
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

        self._bridge = _Bridge()
        self._bridge.gateway_changed.connect(self._on_gateway_state)
        self._bridge.log.connect(self._append_log)
        self._main_thread = MainThread()          # Portfolio 頁讀數從背景轉回主執行緒
        self._gateway_flow = GatewayStartFlow(
            ports_factory=gateway_ports_factory, dialogs=self._dialogs, log=self.log,
            recheck=self._source.recheck, states=self.dashboard.gateway_states,
            after_checks=self._after_gateway_login, step_aside=self.showMinimized,
            accounts=gateway_accounts)
        self._gateway_flow.running_changed.connect(self.dashboard.set_gateway_starting)
        self._page_source = PortfolioPageSource(
            names, PageFetchers(snapshot=portfolio_fetch, params=strategy_config_fetch,
                                trades=trades_fetch, equity=equity_fetch,
                                flows=cash_flows_fetch),
            schedule=self._schedule, cancel=self._cancel, post=self._main_thread.call.emit,
            on_change=self._on_page_change, log=self.log,
            next_refresh_time=portfolio_next_refresh)
        self._clock_timer = QTimer(self, interval=1000, timeout=self._update_clock)
        self._signal_timer = QTimer(self, interval=SHOW_SIGNAL_POLL_MS,
                                    timeout=self.check_show_signal)
        # 每分鐘更新值班下次時間與今日排程（Gateway 的 07:00 重新檢查由 Morning check 做）
        self._minute_timer = QTimer(self, interval=MINUTE_POLL_MS, timeout=self.tick_minute)
        self._update_clock()

    def _build_trading(self, target: OrderTarget, service_factory,
                       symbol_loader) -> TradingPage:
        names = list(self._manual_accounts)
        service, sync, takeover, problem = None, None, None, ""
        try:
            service = target.manual_order_service(names, service_factory)
        except Exception as e:
            problem = f"手動單服務無法啟動：{e}"
        else:
            sync, takeover = target.sync(), target.takeover()
        self._manual_service = service       # 手動單對帳在背景持續跑
        return TradingPage(names, service, unavailable=problem, target_text=target.text,
                           dialogs=self._dialogs, log=self.log, symbol_loader=symbol_loader,
                           sync=sync, takeover=takeover)

    # ── 生命週期 ─────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self._source.start(self._bridge.gateway_changed.emit)
        self._watch = QtWatch(source=self._source, account_names=self._names,
                              ports=self._watch_ports, log=self.log,
                              refresh=self.recheck_gateways, now=self._taipei_now,
                              remote_account=self._remote_account,
                              ty_check_accounts=self._ty_check_accounts, parent=self)
        self._watch.start()
        self._start_manual_order_tracking()
        self._reload()
        self._page_source.start()
        self.trading.start()
        self._clock_timer.start()
        self._minute_timer.start()
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
        self._minute_timer.stop()
        self._gateway_flow.stop()
        if self._watch is not None:
            self._watch.stop()
        stop_tracking = getattr(self._manual_service, "stop_monitor", None)
        if callable(stop_tracking):
            stop_tracking()
        self._page_source.stop()
        self.trading.stop()
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

    def tick_minute(self) -> None:
        if not self._running:
            return
        self._update_watch()
        self._update_jobs()

    def _start_manual_order_tracking(self) -> None:
        """手動單對帳與結案通知在背景持續跑。"""
        start = getattr(self._manual_service, "start_monitor", None)
        if not callable(start):
            return
        try:
            start()
        except Exception as e:
            self.log(f"[!] 手動單追蹤無法啟動：{e}")

    def _update_clock(self) -> None:
        self._header.clock.setText(self._clock().strftime("%Y-%m-%d  %H:%M:%S"))

    # ── Gateway ──────────────────────────────────────────────────────

    def _on_gateway_state(self, name: str, state: str, detail: str) -> None:
        previous = self.dashboard.gateway_states().get(name)
        self.dashboard.set_gateway_state(name, state, detail)
        todo = gateway_transition(name, previous, state, detail, self._remote_account)
        if todo.update_tray:
            update_tray = getattr(self._tray, "update_state", None)
            if callable(update_tray):
                update_tray(state, self._clock(), name)
        if todo.remote_offline and self._watch is not None:
            self._watch.remote_went_offline()
        if todo.log:
            self.log(todo.log)

    def recheck_gateways(self) -> None:
        self.log("[>] 重新檢查 Gateway 連線")
        self._source.recheck()

    def start_gateways(self) -> bool:
        """一鍵啟動 Gateway；已經在跑就不重複開。"""
        return self._gateway_flow.start()

    def gateway_start_running(self) -> bool:
        return self._gateway_flow.is_running()

    def _after_gateway_login(self) -> None:
        # 登入後的檢查結束就讀一次權益與持倉。
        # 不再重新檢查 Gateway：每帳戶連線次數已由 POST_LOGIN_MAX_CHECKS 限住。
        self._reload()
        self.log("[GW] 登入後檢查完成")
        self.refresh_portfolio()

    # ── 權益／持倉 ───────────────────────────────────────────────────

    def refresh_portfolio(self) -> tuple:
        """三個帳戶各查一次（唯讀）；還在查的帳戶不重複送。"""
        return self._page_source.refresh()

    def load_account_extras(self) -> bool:
        """背景讀各帳戶的策略設定、最近成交、權益序列、出入金；上一輪還沒讀完就不重複讀。"""
        return self._page_source.refresh_extras()

    def account_extras_loading(self) -> bool:
        return self._page_source.extras_loading()

    def edit_allocation(self, name: str) -> None:
        """調整帳戶的 Allocation（#220）：拉桿 → 檢查 → 確認 → 只寫這個帳戶。"""
        if self._allocation_save is None:
            return
        allocation_flow.edit_allocation(name, read=self._page_source.read_params,
                                        save=self._allocation_save, dialogs=self._dialogs,
                                        log=self.log)

    def _on_page_change(self, change: PageChange) -> None:
        if change.kind == SNAPSHOT:
            self.portfolio.set_state(change.name, change.data)
        else:
            self.portfolio.set_extra(change.name, change.kind, change.data, change.error)

    def _schedule(self, ms: int, fn) -> QTimer:
        """Portfolio 頁讀數排程的計時器：單次、觸發後自己釋放。"""
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(fn)
        timer.timeout.connect(timer.deleteLater)
        timer.start(ms)
        return timer

    @staticmethod
    def _cancel(timer: QTimer) -> None:
        try:
            timer.stop()
            timer.deleteLater()
        except RuntimeError:        # 已觸發並釋放
            pass

    def edit_gateway_credentials(self, account: str) -> None:
        """更新某帳戶存在認證管理員的 Gateway 帳密（改密碼後用）。"""
        try:
            current = self._credential_store.read(account)
        except OSError as e:
            self._dialogs.show_message("Gateway 帳密", f"讀取 {account} 帳密失敗：{e}", error=True)
            return
        creds = self._dialogs.ask_credentials(account, current.username if current else "")
        if creds is None:
            return
        try:
            self._credential_store.write(account, creds)
        except OSError as e:
            self._dialogs.show_message("Gateway 帳密", f"儲存 {account} 帳密失敗：{e}", error=True)
            return
        self.log(f"[GW] {account} Gateway 帳密已更新")
        self._dialogs.show_message("Gateway 帳密", f"{account} 帳密已存進 Windows 認證管理員。")

    # ── 重新整理 ─────────────────────────────────────────────────────

    def refresh(self) -> None:
        """手動重新整理：重讀 Dashboard 資料，並重新檢查斷線的 Gateway。"""
        self.log("重新整理...")
        self._reload()
        self._source.recheck()
        self.log("[OK] 重新整理完成")

    def _reload(self) -> None:
        try:
            self.dashboard.set_data_date(self._read_data_date())
        except Exception as e:
            self.log(f"[X] 資料日期讀取失敗：{e}")
        self._update_jobs()
        self._update_watch()
        self.dashboard.set_last_refresh(self._clock())

    def _update_watch(self) -> None:
        now = self._taipei_now()
        try:
            items = dashboard_data.watch_items(now, self._remote_account,
                                               self._ty_check_accounts)
        except Exception as e:
            self.log(f"[X] 值班時間計算失敗：{e}")
            return
        self.dashboard.set_watch(items, now)

    def _update_jobs(self) -> None:
        try:
            self.dashboard.set_jobs(self._today_jobs(self._clock()))
        except Exception as e:
            self.log(f"[X] 今日排程讀取失敗：{e}")

    # ── Log ──────────────────────────────────────────────────────────

    def log(self, msg: str) -> None:
        """寫一行到 Output Log；任何執行緒都可以呼叫。"""
        self._bridge.log.emit(msg)

    def _append_log(self, msg: str) -> None:
        self.dashboard.append_log(f"{self._clock():%H:%M:%S}  {msg}")

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
