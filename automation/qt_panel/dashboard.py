"""Dashboard 畫面：Gateway、值班、今日排程、資料日期、Output Log（#147、#148）。

只負責顯示與把按鈕接到 caller 給的動作；資料從哪來、按下去做什麼由主視窗決定。
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Sequence

from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QGridLayout, QHBoxLayout, QMenu, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from qt_panel import theme
from qt_panel.dashboard_data import JOB_STATE_TEXT, JobRow, WatchItem, format_next_run
from qt_panel.widgets import Card, all_text, clear_layout, dot, label, set_dot_color, visible_text

WATCH_NOTE = "並行模式：值班由 Tk Panel 執行，新 Panel 只顯示、不值班"
WATCH_OWNER_TEXT = {True: "Tk Panel 值班中", False: "Tk Panel 沒開 — 目前沒有人值班"}
LOG_MAX_LINES = 5000


class GatewayCard(Card):
    def __init__(self, name: str, port):
        super().__init__()
        self.name = name
        self.state = "init"
        self._port = port

        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(dot(theme.ACCOUNT_COLOR.get(name, theme.NAVY), 8))
        top.addWidget(label(name, name="MedNum"))
        top.addStretch(1)
        self._dot = dot(theme.GATEWAY_COLOR["init"])
        self._state = label()
        top.addWidget(self._dot)
        top.addWidget(self._state)
        self.body.addLayout(top)
        self._detail = label(name="Faint")
        self.body.addWidget(self._detail)
        self.set_state("init", "")

    def set_state(self, state: str, detail: str) -> None:
        self.state = state
        color = theme.GATEWAY_COLOR.get(state, theme.TEXT_FAINT)
        set_dot_color(self._dot, color)
        self._state.setText(theme.GATEWAY_TEXT.get(state, state))
        self._state.setStyleSheet(f"color: {color};")
        parts = [f"port {self._port}"] if self._port else []
        if detail:
            parts.append(detail)
        self._detail.setText(" · ".join(parts))


class Dashboard(QWidget):
    def __init__(self, accounts: list[dict], *, on_recheck: Callable[[], None],
                 on_start_gateways: Callable[[], None],
                 on_edit_credentials: Callable[[str], None],
                 credential_accounts: Sequence[str],
                 on_refresh: Callable[[], None]):
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(16)

        # ── Gateway ──
        head = QHBoxLayout()
        head.addWidget(label("GATEWAY", name="SectionTitle"))
        head.addStretch(1)
        self._auto_recheck = label(name="Faint")
        head.addWidget(self._auto_recheck)
        self.start_gateways_button = QPushButton("一鍵啟動 Gateway")
        self.start_gateways_button.setObjectName("Primary")
        self.start_gateways_button.setToolTip(
            "開三個實盤 Gateway、填帳密、依序登入；手機只要確認 IB Key。"
            "三個都要先關閉，否則整批拒絕")
        self.start_gateways_button.clicked.connect(on_start_gateways)
        head.addWidget(self.start_gateways_button)
        self.credentials_button = QPushButton("Gateway 帳密")
        self.credentials_button.setToolTip("改密碼後更新存在 Windows 認證管理員的帳密")
        self.credentials_menu = QMenu(self.credentials_button)
        for account in credential_accounts:
            self.credentials_menu.addAction(QAction(
                account, self.credentials_menu,
                triggered=lambda _=False, a=account: on_edit_credentials(a)))
        self.credentials_button.setMenu(self.credentials_menu)
        head.addWidget(self.credentials_button)
        self.recheck_button = QPushButton("重新檢查")
        self.recheck_button.setToolTip("斷線的 Gateway 各重連一次；連著的不動（同 Tk 的 Check Now）")
        self.recheck_button.clicked.connect(on_recheck)
        head.addWidget(self.recheck_button)
        root.addLayout(head)
        row = QHBoxLayout()
        row.setSpacing(12)
        self._cards: dict[str, GatewayCard] = {}
        for acct in accounts:
            card = GatewayCard(acct["name"], acct.get("port"))
            self._cards[acct["name"]] = card
            row.addWidget(card, 1)
        root.addLayout(row)

        # ── 值班｜今日排程｜資料 ──
        mid = QHBoxLayout()
        mid.setSpacing(12)

        self._watch_card = Card("值班")
        self._watch_note = label(WATCH_NOTE, name="Faint")
        self._watch_note.setWordWrap(True)
        self._watch_card.body.addWidget(self._watch_note)
        owner_row = QHBoxLayout()
        self._owner_dot = dot(theme.TEXT_FAINT, 8)
        self._owner = label()
        owner_row.addWidget(self._owner_dot)
        owner_row.addWidget(self._owner, 1)
        self._watch_card.body.addLayout(owner_row)
        self._watch_grid = QGridLayout()
        self._watch_grid.setHorizontalSpacing(16)
        self._watch_grid.setVerticalSpacing(2)
        self._watch_card.body.addLayout(self._watch_grid)
        self._watch_card.body.addStretch(1)
        mid.addWidget(self._watch_card, 5)

        self._jobs_card = Card("今日排程")
        self._jobs_grid = QGridLayout()
        self._jobs_grid.setHorizontalSpacing(10)
        self._jobs_grid.setVerticalSpacing(6)
        self._jobs_card.body.addLayout(self._jobs_grid)
        self._jobs_card.body.addStretch(1)
        mid.addWidget(self._jobs_card, 4)

        data = Card("資料")
        data.body.addWidget(label("資料日期", name="Dim"))
        self._data_date = label("—", name="BigNum")
        data.body.addWidget(self._data_date)
        data.body.addSpacing(6)
        data.body.addWidget(label("最後更新（本機時間）", name="Dim"))
        self._last_refresh = label("—", name="MedNum")
        data.body.addWidget(self._last_refresh)
        data.body.addStretch(1)
        self.refresh_button = QPushButton("重新整理")
        self.refresh_button.setToolTip("重讀資料日期、今日排程與值班狀態，並重新檢查斷線的 Gateway")
        self.refresh_button.clicked.connect(on_refresh)
        data.body.addWidget(self.refresh_button)
        mid.addWidget(data, 3)
        root.addLayout(mid)

        # ── Output Log ──
        log_card = Card("OUTPUT LOG")
        self.clear_log_button = QPushButton("清除")
        self.clear_log_button.setObjectName("Small")
        log_card.title_row.addWidget(self.clear_log_button)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("Log")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_MAX_LINES)
        self.clear_log_button.clicked.connect(self.log_view.clear)
        log_card.body.addWidget(self.log_view)
        root.addWidget(log_card, 1)

    # ── Gateway ──

    def set_gateway_state(self, name: str, state: str, detail: str) -> None:
        card = self._cards.get(name)
        if card is not None:
            card.set_state(state, detail)

    def set_next_auto_recheck(self, when: datetime) -> None:
        self._auto_recheck.setText(f"交易日 07:00 自動重新檢查 · 下次 {when:%m-%d %H:%M}")

    def set_gateway_starting(self, starting: bool) -> None:
        self.start_gateways_button.setEnabled(not starting)
        self.start_gateways_button.setText("Gateway 啟動中…" if starting else "一鍵啟動 Gateway")

    # ── 值班 ──

    def set_watch(self, items: list[WatchItem], now: datetime, tk_running: bool) -> None:
        color = theme.GREEN if tk_running else theme.RED
        set_dot_color(self._owner_dot, color)
        self._owner.setText(WATCH_OWNER_TEXT[tk_running])
        self._owner.setStyleSheet(f"color: {color};")
        clear_layout(self._watch_grid)
        for i, item in enumerate(items):
            self._watch_grid.addWidget(label(item.name), i * 2, 0)
            self._watch_grid.addWidget(label(item.schedule, name="Faint"), i * 2 + 1, 0)
            self._watch_grid.addWidget(
                label(f"下次 {format_next_run(now, item.next_run)}", name="MedNum"), i * 2, 1)
        self._watch_grid.setColumnStretch(0, 1)

    # ── 今日排程 ──

    def set_jobs(self, rows: list[JobRow]) -> None:
        clear_layout(self._jobs_grid)
        row = 0
        for job in rows:
            color = theme.JOB_COLOR.get(job.state, theme.TEXT_FAINT)
            self._jobs_grid.addWidget(label(job.at, name="MedNum"), row, 0)
            name = label(job.name)
            if job.detail:
                name.setToolTip(job.detail)
            self._jobs_grid.addWidget(name, row, 1)
            self._jobs_grid.addWidget(dot(color, 8), row, 2)
            self._jobs_grid.addWidget(
                label(JOB_STATE_TEXT.get(job.state, job.state), color=color), row, 3)
            row += 1
            if job.state == "problem" and job.detail:
                reason = label(job.detail, name="Faint")
                reason.setWordWrap(True)
                self._jobs_grid.addWidget(reason, row, 1, 1, 3)
                row += 1
        self._jobs_grid.setColumnStretch(1, 1)

    # ── 資料 ──

    def set_data_date(self, text: str) -> None:
        self._data_date.setText(text)

    def set_last_refresh(self, when: datetime) -> None:
        self._last_refresh.setText(f"{when:%Y-%m-%d %H:%M:%S}")

    # ── Log ──

    def append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    # ── 讀取（測試與狀態檢查用）──

    def auto_recheck_text(self) -> str:
        return self._auto_recheck.text()

    def gateway_names(self) -> list[str]:
        return list(self._cards)

    def gateway_state(self, name: str) -> str:
        return self._cards[name].state

    def gateway_states(self) -> dict[str, str]:
        return {name: card.state for name, card in self._cards.items()}

    def gateway_text(self, name: str) -> str:
        return visible_text(self._cards[name])

    def watch_note(self) -> str:
        return self._watch_note.text()

    def watch_text(self) -> str:
        return all_text(self._watch_card)

    def jobs_text(self) -> str:
        return all_text(self._jobs_card)

    def data_date_text(self) -> str:
        return self._data_date.text()

    def last_refresh_text(self) -> str:
        return self._last_refresh.text()

    def log_text(self) -> str:
        return self.log_view.toPlainText()
