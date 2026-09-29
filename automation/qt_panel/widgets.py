"""新 Panel 共用的小元件。"""
from __future__ import annotations

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLayout, QVBoxLayout


class Card(QFrame):
    """圓角白色卡片，可帶標題列。"""

    def __init__(self, title: str | None = None):
        super().__init__()
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 14, 16, 14)
        self.body.setSpacing(8)
        self.title_row = QHBoxLayout()
        if title:
            self.title_row.addWidget(label(title, name="SectionTitle"))
        self.title_row.addStretch(1)
        self.body.addLayout(self.title_row)


def label(text: str = "", *, name: str | None = None, color: str | None = None) -> QLabel:
    lbl = QLabel(text)
    if name:
        lbl.setObjectName(name)
    if color:
        lbl.setStyleSheet(f"color: {color};")
    return lbl


def pill(text: str, fg: str, bg: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("Pill")
    lbl.setFixedHeight(22)
    lbl.setStyleSheet(f"color: {fg}; background: {bg};")
    return lbl


def dot(color: str, size: int = 10) -> QLabel:
    lbl = QLabel()
    lbl.setFixedSize(size, size)
    set_dot_color(lbl, color)
    return lbl


def set_dot_color(lbl: QLabel, color: str) -> None:
    lbl.setStyleSheet(f"background: {color}; border-radius: {lbl.width() // 2}px;")


def visible_text(widget) -> str:
    """widget 底下目前看得到的文字（測試與無障礙讀取用）。"""
    return " ".join(l.text() for l in widget.findChildren(QLabel)
                    if l.isVisibleTo(widget) and l.text())


def all_text(widget) -> str:
    """widget 底下所有標籤的文字，不管顯示了沒（剛加進版面的元件要等事件迴圈才顯示）。"""
    return " ".join(l.text() for l in widget.findChildren(QLabel) if l.text())


def clear_layout(layout: QLayout) -> None:
    """拿掉 layout 裡的所有元件（重畫整張清單用）。"""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)   # 立刻離開畫面；deleteLater 要等事件迴圈
            widget.deleteLater()
