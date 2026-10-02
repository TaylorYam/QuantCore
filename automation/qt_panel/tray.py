"""系統匣（Qt 內建 QSystemTrayIcon）：點圖示叫回視窗，選單可離開。

圖示跟著 Rosa Gateway 的心跳換顏色（灰＝連線中／綠＝連上／紅＝斷線），同 Tk 的
TrayController，圖檔共用 tray_icons 產生的 assets/tray。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


AUTOMATION_DIR = Path(__file__).resolve().parent.parent
STATE_TO_COLOR = {"init": "gray", "connecting": "gray", "online": "green", "offline": "red"}


def _state_icon(color: str) -> QIcon | None:
    try:
        from tray_icons import _icon_filename, ensure_tray_icons
        assets = AUTOMATION_DIR / "assets" / "tray"
        ensure_tray_icons(assets, AUTOMATION_DIR / "quantcore.ico")
        return QIcon(str(assets / _icon_filename(color, 32)))
    except Exception:
        return None                      # 圖檔有問題就維持原本的圖示


class QtTray:
    def __init__(self, *, on_show: Callable[[], None], on_quit: Callable[[], None],
                 icon: QIcon, tooltip: str):
        self._icon = QSystemTrayIcon(icon)
        self._tooltip = tooltip
        self._icon.setToolTip(tooltip)
        menu = QMenu()
        menu.addAction(QAction("顯示 Panel", menu, triggered=on_show))
        menu.addSeparator()
        menu.addAction(QAction("離開", menu, triggered=on_quit))
        self._menu = menu  # QSystemTrayIcon 不接管選單的生命週期，自己留著
        self._icon.setContextMenu(menu)
        self._icon.activated.connect(
            lambda reason: on_show() if reason in (
                QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)

    def show(self) -> None:
        self._icon.show()

    def hide(self) -> None:
        self._icon.hide()

    def update_state(self, state: str, when: datetime) -> None:
        """Rosa 心跳狀態 → 圖示顏色與提示文字（同 Tk 的 TrayController.update_state）。"""
        icon = _state_icon(STATE_TO_COLOR.get(state, "gray"))
        if icon is not None:
            self._icon.setIcon(icon)
        self._icon.setToolTip(f"{self._tooltip}\nRosa: {state.upper()} (last {when:%H:%M})")


def tray_factory_for(icon: QIcon, tooltip: str):
    """系統匣可用就回傳建立 QtTray 的工廠；不可用回傳 None（關視窗 = 離開）。"""
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None
    return lambda *, on_show, on_quit: QtTray(
        on_show=on_show, on_quit=on_quit, icon=icon, tooltip=tooltip)
