"""新 Panel 主視窗：頂端列＋左側導覽＋三個畫面（#147、#148、#149、#150、#151）。

外部依賴全部從建構子注入（Gateway 狀態來源、Gateway 啟動的 Ports、認證管理員、
查權益／持倉的函式、權益序列／策略設定／最近成交的讀取、資料日期／今日排程／
Tk Panel 是否值班的讀取、時鐘、系統匣工廠、叫回視窗的訊號檔），測試用假的驅動，
不連 IB、不讀真實檔案。

並行模式不值班（ADR-0012）：值班只顯示下次時間與 Tk Panel 有沒有開，這裡
不建任何值班 worker、不發 Telegram。

送單（#151）：Trading 的所有送單連線都由這裡建的送單閘門（broker_gate.SendGate）
產生；並行模式下只接 config.json `qt_panel_paper` 指定的 paper Gateway，三個帳戶
名稱都導向那個 paper 帳號，手動單通知只寫 Output Log。
"""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton, QStackedWidget,
    QVBoxLayout, QWidget,
)

from gateway_start import LIVE_ACCOUNTS
from morning_check import next_morning_check_time
from portfolio_queries import PortfolioQueries, PortfolioRefreshLoop

from qt_panel import dashboard_data, theme
from qt_panel.dashboard import Dashboard
from qt_panel.dashboard_data import JobRow
from qt_panel.gateway_actions import (
    CredentialStore, Dialogs, GatewayStartFlow, PortsFactory, QtDialogs,
)
from qt_panel.broker_gate import PaperTarget, SendGate
from qt_panel.gateway_status import GatewaySource
from qt_panel.mode import PanelMode
from qt_panel.portfolio import EQUITY, FLOWS, PARAMS, TRADES, PortfolioPage
from qt_panel.trading import TradingPage, paper_manual_order_service
from qt_panel.widgets import label, pill, visible_text

PAGES = ["Dashboard", "Portfolio", "Trading"]
SHOW_SIGNAL_POLL_MS = 2000
AUTO_RECHECK_POLL_MS = 60_000


def _taipei_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Taipei"))


class _Bridge(QObject):
    """背景 thread 的回呼 → Qt 主執行緒（跨執行緒的 signal 會自動排隊）。"""
    gateway_changed = Signal(str, str, str)
    log = Signal(str)
    account_extra = Signal(str, str, object, str)     # 帳戶, kind, 資料, 錯誤
    extras_done = Signal()


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


def _default_broker():
    from ib_insync import IB
    return IB()


class MainWindow(QMainWindow):
    quit_requested = Signal()

    def __init__(self, *, accounts: list[dict], mode: PanelMode,
                 gateway_source: GatewaySource,
                 gateway_ports_factory: PortsFactory,
                 credential_store: CredentialStore,
                 portfolio_fetch: Callable[[str], dict],
                 equity_fetch: Callable[[str], object],
                 cash_flows_fetch: Callable[[str], object],
                 strategy_config_fetch: Callable[[str], dict],
                 trades_fetch: Callable[[str], list],
                 portfolio_next_refresh: Callable[[datetime], datetime] | None = None,
                 dialogs: Dialogs | None = None,
                 read_data_date: Callable[[], str] = dashboard_data.read_data_date,
                 today_jobs: Callable[[datetime], list[JobRow]] | None = None,
                 tk_panel_running: Callable[[], bool] = dashboard_data.tk_panel_running,
                 clock: Callable[[], datetime] = datetime.now,
                 taipei_now: Callable[[], datetime] = _taipei_now,
                 tray_factory=None, show_signal: Path | None = None,
                 mode_note: str = "",
                 paper_target: PaperTarget | None = None,
                 paper_problem: str = "尚未設定 paper 連線",
                 broker_factory: Callable[[], object] = _default_broker,
                 manual_order_service: Callable[..., object] | None = None,
                 symbol_loader: Callable | None = None):
        super().__init__()
        self._source = gateway_source
        self._credential_store = credential_store
        self._dialogs = dialogs if dialogs is not None else QtDialogs(self)
        self._read_data_date = read_data_date
        names = [a["name"] for a in accounts]
        self._names = names
        # 單一帳戶頁的權益序列、策略參數（唯讀）、最近成交：讀本機檔案，背景讀；
        # 出入金要向 IB Flex 下載（可能要等幾秒），排在所有帳戶的本機資料之後
        self._extra_fetchers = ((PARAMS, strategy_config_fetch), (TRADES, trades_fetch),
                                (EQUITY, equity_fetch), (FLOWS, cash_flows_fetch))
        self._extras_loading = False
        self._flow_problems: dict[str, str] = {}      # 出入金問題每帳戶同樣的只記一次
        self._today_jobs = today_jobs or (lambda now: dashboard_data.today_jobs(now, names))
        self._tk_panel_running = tk_panel_running
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
        self.dashboard = Dashboard(
            accounts, on_recheck=self.recheck_gateways,
            on_start_gateways=self.start_gateways,
            on_edit_credentials=self.edit_gateway_credentials,
            credential_accounts=LIVE_ACCOUNTS, on_refresh=self.refresh)
        self.portfolio = PortfolioPage(names, on_refresh=self.refresh_portfolio)
        # 送單閘門：Trading 每一條 IB 連線都從這裡建，並行模式只放行 paper
        self.send_gate = SendGate(mode, broker_factory)
        self.trading = self._build_trading(
            mode, paper_target, paper_problem, manual_order_service, symbol_loader)
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
        self._bridge.account_extra.connect(self._on_account_extra)
        self._bridge.extras_done.connect(self._on_extras_done)
        self._gateway_flow = GatewayStartFlow(
            ports_factory=gateway_ports_factory, dialogs=self._dialogs, log=self.log,
            recheck=self._source.recheck, states=self.dashboard.gateway_states,
            after_checks=self._after_gateway_login, step_aside=self.showMinimized)
        self._gateway_flow.running_changed.connect(self.dashboard.set_gateway_starting)
        # 權益／持倉：和 Tk 同一套查詢與排程（開啟時一次、美股盤中固定時間），
        # 每個帳戶查完各自更新，一個失敗不影響其他帳戶
        self._portfolio_queries = PortfolioQueries(names, portfolio_fetch)
        self._portfolio_loop = PortfolioRefreshLoop(
            self._portfolio_queries, self._schedule, self._cancel,
            self._on_portfolio_change, next_refresh_time=portfolio_next_refresh,
            log=self.log)
        self._clock_timer = QTimer(self, interval=1000, timeout=self._update_clock)
        self._signal_timer = QTimer(self, interval=SHOW_SIGNAL_POLL_MS,
                                    timeout=self.check_show_signal)
        # 並行期間不值班，但比照 Morning check 每個交易日 07:00 重新檢查一次 Gateway
        # （每天 06:00 自動重啟後心跳已斷、不會自己重連）。每分鐘比對一次時間，
        # 電腦睡眠醒來也不會漏掉；同一個計時器順便更新值班下次時間與今日排程。
        self._minute_timer = QTimer(self, interval=AUTO_RECHECK_POLL_MS,
                                    timeout=self.tick_minute)
        self._next_recheck = next_morning_check_time(self._taipei_now())
        self.dashboard.set_next_auto_recheck(self._next_recheck)
        self._update_clock()

    def _build_trading(self, mode, paper_target, paper_problem, service_factory,
                       symbol_loader) -> TradingPage:
        from manual_orders import SUPPORTED_MANUAL_ACCOUNTS
        supported = {n.casefold() for n in SUPPORTED_MANUAL_ACCOUNTS}
        names = [n for n in self._names if n.casefold() in supported]
        service, problem, target_text = None, paper_problem, ""
        # 正式模式（#154）才會接實盤帳戶；在那之前一律要有 paper 設定
        if mode is PanelMode.PARALLEL and paper_target is not None:
            factory = service_factory or paper_manual_order_service(names)
            try:
                service = factory(ib_factory=self.send_gate.broker,
                                  account_ctx_factory=paper_target.account_ctx,
                                  notifier=self._manual_order_notice)
                target_text = paper_target.describe()
            except Exception as e:
                problem = f"手動單服務無法啟動：{e}"
        return TradingPage(names, service, unavailable=problem, target_text=target_text,
                           dialogs=self._dialogs, log=self.log, symbol_loader=symbol_loader)

    def _manual_order_notice(self, account: str, message: str) -> bool:
        """手動單結案通知：paper 單只寫 Output Log，不發 Telegram。"""
        lines = [line.strip() for line in message.splitlines() if line.strip()]
        self.log(f"[手動單·paper] {account}：" + " ／ ".join(lines))
        return True

    # ── 生命週期 ─────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self._source.start(self._bridge.gateway_changed.emit)
        self._reload()
        self._portfolio_loop.start()
        self.load_account_extras()
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
        self._portfolio_loop.stop()
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
        self.tick_auto_recheck()
        self._update_watch()
        self._update_jobs()

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

    # ── Gateway ──────────────────────────────────────────────────────

    def _on_gateway_state(self, name: str, state: str, detail: str) -> None:
        previous = self.dashboard.gateway_states().get(name)
        self.dashboard.set_gateway_state(name, state, detail)
        if previous is None or state == previous:
            return
        if state == "offline":
            self.log(f"[GW] {name} 離線" + (f" — {detail}" if detail else ""))
        elif state == "online" and previous != "init":
            self.log(f"[GW] {name} 已連上")

    def recheck_gateways(self) -> None:
        self.log("[>] 重新檢查 Gateway 連線")
        self._source.recheck()

    def start_gateways(self) -> bool:
        """一鍵啟動 Gateway；已經在跑就不重複開。"""
        return self._gateway_flow.start()

    def gateway_start_running(self) -> bool:
        return self._gateway_flow.is_running()

    def _after_gateway_login(self) -> None:
        # 同 Tk：登入後的檢查結束就讀一次權益與持倉（Refresh IB）。
        # 不再重新檢查 Gateway：每帳戶連線次數已由 POST_LOGIN_MAX_CHECKS 限住。
        self._reload()
        self.log("[GW] 登入後檢查完成")
        self.refresh_portfolio()

    # ── 權益／持倉 ───────────────────────────────────────────────────

    def refresh_portfolio(self) -> tuple:
        """三個帳戶各查一次（唯讀）；還在查的帳戶不重複送。"""
        started = self._portfolio_loop.refresh()
        if started:
            self.log(f"[>] 讀取帳戶權益與持倉：{'、'.join(started)}")
        else:
            self.log("[>] 帳戶查詢還在進行中")
        self.load_account_extras()
        return started

    def load_account_extras(self) -> bool:
        """背景讀各帳戶的策略設定、最近成交、權益序列、出入金；上一輪還沒讀完就不重複讀。"""
        if not self._running or self._extras_loading:
            return False
        self._extras_loading = True
        names, fetchers, emit = list(self._names), self._extra_fetchers, self._bridge

        def work():
            try:
                for kind, fetch in fetchers:
                    for name in names:
                        try:
                            data, error = fetch(name), ""
                        except Exception as e:
                            data, error = None, str(e) or type(e).__name__
                        emit.account_extra.emit(name, kind, data, error)
            finally:
                emit.extras_done.emit()

        threading.Thread(target=work, name="qt-panel-account-extras", daemon=True).start()
        return True

    def _on_account_extra(self, name: str, kind: str, data, error: str) -> None:
        if not self._running:
            return
        self.portfolio.set_extra(name, kind, data, error)
        if kind == FLOWS:
            self._log_flow_problem(name, error, data)
        elif error:
            what = {EQUITY: "權益紀錄", PARAMS: "策略設定", TRADES: "成交紀錄"}[kind]
            self.log(f"[X] [{name}] {what}讀取失敗：{error}")

    def _log_flow_problem(self, name: str, error: str, result) -> None:
        # 沒設定 Flex 是常態：每次重新讀取都記一筆會洗版，同樣的問題只記一次
        if error:
            problem = f"[X] [{name}] 出入金讀取失敗：{error}"
        elif result.error:
            problem = f"[!] [{name}] 出入金下載失敗，改用快取：{result.error}"
        else:
            problem = ""
        if problem and problem != self._flow_problems.get(name):
            self.log(problem)
        self._flow_problems[name] = problem

    def _on_extras_done(self) -> None:
        self._extras_loading = False

    def account_extras_loading(self) -> bool:
        return self._extras_loading

    def _on_portfolio_change(self, name: str, state) -> None:
        self.portfolio.set_state(name, state)
        if not state.running:
            if state.error:
                self.log(f"[X] [{name}] 權益／持倉讀取失敗：{state.error}")
            else:
                self.log(f"[OK] [{name}] 權益／持倉已更新")

    def _schedule(self, ms: int, fn) -> QTimer:
        """PortfolioRefreshLoop 的計時器（同 Tk 的 after）：單次、觸發後自己釋放。"""
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
        """手動重新整理：重讀 Dashboard 資料，並重新檢查斷線的 Gateway（同 Tk refresh_all）。"""
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
            items = dashboard_data.watch_items(now)
        except Exception as e:
            self.log(f"[X] 值班時間計算失敗：{e}")
            return
        self.dashboard.set_watch(items, now, self._tk_panel_running())

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
