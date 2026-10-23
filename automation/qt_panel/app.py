"""Panel 的啟動流程：管理員權限 → 單一實例 → 讀設定 → 開主視窗（#147）。

Panel 負責值班與實盤送單（#154 從 Tk Panel 切換，#155 刪除 Tk Panel）。
單一實例鎖保證同時只有一個 Panel 在值班；第二次開啟只會把已開的視窗叫到前面。
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
    """不是管理員就用 runas 以 pythonw 重開自己。"""
    exe = sys.executable
    if exe.endswith("python.exe"):
        exe = exe.replace("python.exe", "pythonw.exe")
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", exe, f'"{ENTRY_SCRIPT}"', str(AUTOMATION_DIR), 1)


class WindowsCredentialStore:
    """Gateway 帳密存在 Windows 認證管理員。"""

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
        QMessageBox.information(None, "QuantCore", "Panel 已經開著，會把它叫到前面。")
        return 0

    try:
        from account_utils import load_accounts, load_capabilities
        from ledger import daily_nav
        from qt_panel import theme
        from qt_panel.gateway_status import MonitorGatewaySource
        from qt_panel.main_window import MainWindow
        from qt_panel.portfolio_source import (
            fetch_panel_portfolio, load_account_cash_flows, read_strategy_allocations,
            write_strategy_allocations,
        )
        from qt_panel.tray import tray_factory_for
        from trades_display import read_account_trades

        accounts = [{"name": a["name"], "port": a.get("port")} for a in load_accounts()]
        try:
            capabilities = load_capabilities()
        except ValueError as e:              # 例如兩個帳戶都設 remote_gateway（#211）
            QMessageBox.critical(None, "QuantCore", f"accounts.json 設定有誤，Panel 無法開啟：\n{e}")
            return 1

        app.setFont(QFont(theme.UI_FONT, 10))
        app.setStyleSheet(theme.STYLESHEET)
        icon = QIcon(str(ICON_PATH))
        app.setWindowIcon(icon)

        import gateway_start_win

        window = MainWindow(
            accounts=accounts,
            capabilities=capabilities,
            gateway_source=MonitorGatewaySource([a["name"] for a in accounts]),
            gateway_ports_factory=lambda ask: gateway_start_win.WindowsPorts(ask_credentials=ask),
            credential_store=WindowsCredentialStore(),
            portfolio_fetch=fetch_panel_portfolio,
            equity_fetch=daily_nav,
            cash_flows_fetch=load_account_cash_flows,
            strategy_config_fetch=read_strategy_allocations,
            trades_fetch=lambda name, alloc: read_account_trades(name, alloc=alloc),
            allocation_save=write_strategy_allocations,
            tray_factory=tray_factory_for(icon, "QuantCore Panel"),
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
