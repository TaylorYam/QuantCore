"""新 Panel 的啟動流程：管理員權限 → 單一實例 → 讀設定 → 開主視窗（#147）。

和 Tk Panel 並存：單一實例鎖、叫回視窗的訊號檔、Windows AppUserModelID 都用
自己的名字，兩個 Panel 可以同時開；IB 連線用唯讀身分（見 gateway_status）。
"""
from __future__ import annotations

import ctypes
import sys
import tempfile
from pathlib import Path

APP_USER_MODEL_ID = "Taylor.QuantCore.QtPanel.1"
AUTOMATION_DIR = Path(__file__).resolve().parent.parent
ENTRY_SCRIPT = AUTOMATION_DIR / "qt_panel_app.py"
ICON_PATH = AUTOMATION_DIR / "quantcore.ico"


def lockfile_path() -> Path:
    return Path(tempfile.gettempdir()) / "quantcore_qt_panel.lock"


def show_signal_path() -> Path:
    return Path(tempfile.gettempdir()) / "quantcore_qt_panel.show"


def _is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def _relaunch_as_admin() -> None:
    """和 Tk Panel 一樣：不是管理員就用 runas 以 pythonw 重開自己。"""
    exe = sys.executable
    if exe.endswith("python.exe"):
        exe = exe.replace("python.exe", "pythonw.exe")
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", exe, f'"{ENTRY_SCRIPT}"', str(AUTOMATION_DIR), 1)


class WindowsCredentialStore:
    """Gateway 帳密存在 Windows 認證管理員（同 Tk Panel 的「更新 Gateway 帳密」）。"""

    def read(self, account: str):
        from gateway_start_win import read_credentials
        return read_credentials(account)

    def write(self, account: str, creds) -> None:
        from gateway_start_win import write_credentials
        write_credentials(account, creds)


def main() -> int:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass
    if not _is_admin():
        _relaunch_as_admin()
        return 0

    from single_instance import acquire_lock, release_lock
    from PySide6.QtGui import QFont, QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication(sys.argv)
    lock = lockfile_path()
    if not acquire_lock(lock):
        try:
            show_signal_path().write_text("show", encoding="utf-8")
        except OSError:
            pass
        QMessageBox.information(None, "QuantCore", "新 Panel 已經開著，會把它叫到前面。")
        return 0

    try:
        from account_utils import load_accounts, load_base_config
        from qt_panel import theme
        from qt_panel.gateway_status import gateway_source_for
        from qt_panel.main_window import MainWindow
        from qt_panel.mode import resolve_mode
        from qt_panel.portfolio_source import (
            fetch_readonly_portfolio, load_account_cash_flows, load_equity, read_recent_trades,
            read_strategy_allocations,
        )
        from qt_panel.tray import tray_factory_for

        mode, note = resolve_mode(load_base_config())
        accounts = [{"name": a["name"], "port": a.get("port")} for a in load_accounts()]

        app.setFont(QFont(theme.UI_FONT, 10))
        app.setStyleSheet(theme.STYLESHEET)
        icon = QIcon(str(ICON_PATH))
        app.setWindowIcon(icon)

        import gateway_start_win

        window = MainWindow(
            accounts=accounts,
            mode=mode,
            mode_note=note,
            gateway_source=gateway_source_for(mode, [a["name"] for a in accounts]),
            gateway_ports_factory=lambda ask: gateway_start_win.WindowsPorts(ask_credentials=ask),
            credential_store=WindowsCredentialStore(),
            portfolio_fetch=fetch_readonly_portfolio,
            equity_fetch=load_equity,
            cash_flows_fetch=load_account_cash_flows,
            strategy_config_fetch=read_strategy_allocations,
            trades_fetch=read_recent_trades,
            tray_factory=tray_factory_for(icon, "QuantCore Panel（PySide6・並行模式）"),
            show_signal=show_signal_path(),
        )
        window.quit_requested.connect(app.quit)
        window.start()
        app.setQuitOnLastWindowClosed(not window.has_tray())
        window.show_window()
        try:
            return app.exec()
        finally:
            window.shutdown()
    finally:
        release_lock(lock)
