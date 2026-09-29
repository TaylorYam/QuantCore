"""系統匣（Qt 內建 QSystemTrayIcon）：點圖示叫回視窗，選單可離開。"""
from __future__ import annotations

from typing import Callable

from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


class QtTray:
    def __init__(self, *, on_show: Callable[[], None], on_quit: Callable[[], None],
                 icon: QIcon, tooltip: str):
        self._icon = QSystemTrayIcon(icon)
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


def tray_factory_for(icon: QIcon, tooltip: str):
    """系統匣可用就回傳建立 QtTray 的工廠；不可用回傳 None（關視窗 = 離開）。"""
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None
    return lambda *, on_show, on_quit: QtTray(
        on_show=on_show, on_quit=on_quit, icon=icon, tooltip=tooltip)
