"""Panel 的 Gateway 啟動與帳密更新（#148）。

判斷全部沿用 gateway_start（CONTEXT.md「Gateway 啟動」：三個實盤 Gateway 有任何
一個在執行就整批拒絕、依序登入、失敗不重試）；這裡只負責 Qt 那一側：背景
執行、在主執行緒跳帳密輸入框、登入後自動重新檢查。碰外部的 Ports 與認證管理員
由 caller 注入（真的在 gateway_start_win），測試換成假的。不發 Telegram。
"""
from __future__ import annotations

import threading
from typing import Callable, Protocol, Sequence

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QMessageBox, QVBoxLayout, QWidget,
)

import gateway_start as gs
from gateway_start import Credentials, Started

from qt_panel.widgets import label


class CredentialsDialog(QDialog):
    """帳號＋遮住的密碼；兩個都填了 OK 才有效。Esc／Cancel／關閉 = 不存。"""

    def __init__(self, account: str, username: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Gateway 帳密 — {account}")
        self.setModal(True)
        lay = QVBoxLayout(self)
        lay.addWidget(label(f"{account} — IB Gateway 帳密", name="SectionTitle"))
        form = QFormLayout()
        self.user_edit = QLineEdit(username)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        form.addRow("帳號", self.user_edit)
        form.addRow("密碼", self.password_edit)
        lay.addLayout(form)
        lay.addWidget(label("存在 Windows 認證管理員，不會寫進任何檔案。", name="Faint"))
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._ok)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        (self.password_edit if username else self.user_edit).setFocus()

    def _ok(self) -> None:
        if not self.user_edit.text().strip():
            self.user_edit.setFocus()
            return
        if not self.password_edit.text():
            self.password_edit.setFocus()
            return
        self.accept()

    def credentials(self) -> Credentials | None:
        if self.result() != QDialog.Accepted:
            return None
        return Credentials(self.user_edit.text().strip(), self.password_edit.text())


class Dialogs(Protocol):
    def ask_credentials(self, account: str, username: str = "") -> Credentials | None: ...
    def show_message(self, title: str, text: str, *, error: bool = False) -> None: ...
    def confirm(self, title: str, text: str, *, warning: bool = False) -> bool: ...
    def ask_allocation(self, account: str, current: tuple[float, float, float]
                       ) -> tuple[float, float, float] | None: ...


class QtDialogs:
    """真的跳窗。跳窗暫設最上層：被 Gateway 視窗蓋住時 Panel 才不會整個點不動（Tk #124）。"""

    def __init__(self, parent: QWidget):
        self._parent = parent

    def ask_credentials(self, account: str, username: str = "") -> Credentials | None:
        dialog = CredentialsDialog(account, username, self._parent)
        dialog.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        dialog.exec()
        return dialog.credentials()

    def show_message(self, title: str, text: str, *, error: bool = False) -> None:
        box = QMessageBox(QMessageBox.Critical if error else QMessageBox.Information,
                          title, text, QMessageBox.Ok, self._parent)
        box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        box.exec()

    def confirm(self, title: str, text: str, *, warning: bool = False) -> bool:
        """送單前的確認視窗；預設按鈕是「否」，按 Enter 不會誤送。"""
        box = QMessageBox(QMessageBox.Warning if warning else QMessageBox.Question,
                          title, text, QMessageBox.Yes | QMessageBox.No, self._parent)
        box.setDefaultButton(QMessageBox.No)
        box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        return box.exec() == QMessageBox.Yes

    def ask_allocation(self, account: str, current: tuple[float, float, float]
                       ) -> tuple[float, float, float] | None:
        """調整 Allocation 的拉桿對話框（#220）；取消 = None。"""
        from qt_panel.account_panels import AllocationDialog
        dialog = AllocationDialog(account, current, self._parent)
        dialog.exec()
        return dialog.result_values()


class CredentialStore(Protocol):
    def read(self, account: str) -> Credentials | None: ...
    def write(self, account: str, creds: Credentials) -> None: ...


PortsFactory = Callable[[Callable[[str], Credentials | None]], gs.Ports]


class GatewayStartFlow(QObject):
    """一鍵啟動：背景跑 start_live_gateways，完成後回主執行緒報結果、登入後自動重新檢查。

    running_changed(bool)：按鈕要跟著停用／恢復。
    """
    running_changed = Signal(bool)
    _ask = Signal(str)
    _done = Signal(object, str, bool)   # (結果或 None, 訊息, ok)

    def __init__(self, *, ports_factory: PortsFactory, dialogs: Dialogs,
                 log: Callable[[str], None], recheck: Callable[[], None],
                 states: Callable[[], dict[str, str]], after_checks: Callable[[], None],
                 step_aside: Callable[[], None], accounts: Sequence[str]):
        """accounts：一鍵啟動的實盤帳戶（有 gateway_dir 的，#211）。"""
        super().__init__()
        self._accounts = tuple(accounts)
        self._ports_factory = ports_factory
        self._dialogs = dialogs
        self._log = log
        self._recheck = recheck
        self._states = states
        self._after_checks = after_checks
        self._step_aside = step_aside
        self._running = False
        self._stopped = False
        self._answer: dict = {}
        self._answered = threading.Event()
        self._checks = 0
        self._polls = 0
        self._ask.connect(self._on_ask)
        self._done.connect(self._on_done)

    def is_running(self) -> bool:
        return self._running

    def stop(self) -> None:
        """Panel 關閉：還排著的登入後檢查不再執行。"""
        self._stopped = True

    def start(self) -> bool:
        if self._running:
            return False
        self._running = True
        self.running_changed.emit(True)
        self._log("[>] 啟動 Gateway — 檢查實盤 Gateway 是否都已關閉...")
        threading.Thread(target=self._run, daemon=True, name="QtGatewayStart").start()
        return True

    # ── 背景執行緒 ───────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            result = gs.start_live_gateways(self._ports_factory(self._ask_credentials),
                                            self._accounts)
            self._done.emit(result, gs.describe(result), True)
        except Exception as e:
            self._done.emit(None, f"啟動 Gateway 失敗：{type(e).__name__}: {e}", False)

    def _ask_credentials(self, account: str) -> Credentials | None:
        """背景執行緒呼叫：請主執行緒跳帳密輸入框，等使用者按 OK／Cancel。"""
        self._answered.clear()
        self._ask.emit(account)
        self._answered.wait()
        return self._answer.pop("creds", None)

    # ── 主執行緒 ─────────────────────────────────────────────────────

    @Slot(str)
    def _on_ask(self, account: str) -> None:
        try:
            self._answer["creds"] = self._dialogs.ask_credentials(account)
        finally:
            self._answered.set()

    @Slot(object, str, bool)
    def _on_done(self, result, msg: str, ok: bool) -> None:
        self._running = False
        self.running_changed.emit(False)
        for line in msg.splitlines():
            if line:
                self._log(f"[GW] {line}")
        started = isinstance(result, Started)
        # 三個都登入就沒有要使用者處理的事：不跳窗（Tk #129）
        if not (started and result.all_logged_in):
            self._dialogs.show_message("啟動 Gateway", msg, error=not ok)
            if started:
                self._step_aside()   # 讓沒登入成功的 Gateway 登入視窗露出來
        if started:
            self._begin_post_login_checks()

    # ── 登入後：等 Gateway 能接 API 再重新檢查（同 Tk #131 的節奏）─────

    def _begin_post_login_checks(self) -> None:
        self._checks = 0
        self._log(f"[GW] {gs.POST_LOGIN_FIRST_CHECK_SEC} 秒後重新檢查 Gateway 連線")
        QTimer.singleShot(int(gs.POST_LOGIN_FIRST_CHECK_SEC * 1000), self._check_now)

    def _check_now(self) -> None:
        if self._stopped:
            return
        self._checks += 1
        self._polls = 0
        self._recheck()
        QTimer.singleShot(int(gs.POST_LOGIN_POLL_SEC * 1000), self._poll)

    def _poll(self) -> None:
        if self._stopped:
            return
        states = self._states()
        self._polls += 1
        if self._polls > 60 / max(gs.POST_LOGIN_POLL_SEC, 0.001):
            step = "done"            # 一分鐘都沒結果：不再等
        else:
            step = gs.next_step_after_login(states, self._checks)
        if step == "wait":
            QTimer.singleShot(int(gs.POST_LOGIN_POLL_SEC * 1000), self._poll)
            return
        pending = [n for n, s in states.items() if s != "online"]
        if step == "retry":
            self._log(f"[GW] {'、'.join(pending)} 還沒連上，"
                      f"{gs.POST_LOGIN_RETRY_SEC} 秒後再重新檢查")
            QTimer.singleShot(int(gs.POST_LOGIN_RETRY_SEC * 1000), self._check_now)
            return
        if pending:
            self._log(f"[GW] {'、'.join(pending)} 仍未連上，請到該 Gateway 確認")
        self._after_checks()
