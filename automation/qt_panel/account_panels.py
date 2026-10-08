"""單一帳戶頁的策略參數與最近成交（#150）。

顯示內容來自和 Tk Panel 共用的 strategy_params_display、trades_display。
策略參數卡的「調整…」開 AllocationDialog（#220）；驗證、確認與寫檔由主視窗處理，
這裡只負責元件。
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QGridLayout, QHeaderView, QPushButton,
    QSlider, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import strategy_params_display

from qt_panel import theme
from qt_panel.widgets import Card, all_text, clear_layout, label

TRADE_HEADERS = ("時間", "代號", "方向", "股數", "成交價", "策略", "狀態")
ALLOCATION_STEP = 0.05          # 拉桿每格 5%，範圍 0–100%（同 Tk 的拉桿）
ALLOCATION_STEPS = 20


def _add_params_rows(grid: QGridLayout, row: int, params) -> list:
    """合計 Leverage 與借款說明加到 grid 的第 row 列起；回傳數值標籤，讓呼叫端更新。"""
    grid.addWidget(label("Leverage", name="Dim"), row, 0)
    leverage = label(params.leverage_text, name="MedNum",
                     color=theme.color(params.leverage_color))
    grid.addWidget(leverage, row, 1, 1, -1, Qt.AlignRight)
    detail = label(params.detail, name="Faint")
    grid.addWidget(detail, row + 1, 0, 1, -1)
    return [leverage, detail]


class StrategyParamsCard(Card):
    """各 Strategy 的 Allocation 與合計 Leverage；「調整…」交給 on_edit。"""

    def __init__(self, on_edit: Callable[[], None] | None = None):
        super().__init__("策略參數")
        self.edit_button = QPushButton("調整…")
        self.edit_button.setObjectName("Small")
        self.edit_button.setToolTip("調整這個帳戶各策略的 Allocation（寫入 accounts.json）")
        if on_edit is not None:
            self.edit_button.clicked.connect(on_edit)
        self.edit_button.setVisible(on_edit is not None)
        self.title_row.addWidget(self.edit_button)
        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(12)
        self._grid.setVerticalSpacing(6)
        self.body.addLayout(self._grid)
        self._message = label(name="Dim")
        self._message.setWordWrap(True)
        self.body.addWidget(self._message)
        self.body.addStretch(1)
        self.set_config(None)

    def set_config(self, cfg: dict | None, error: str = "") -> None:
        clear_layout(self._grid)
        self.edit_button.setEnabled(cfg is not None)
        if cfg is None:
            self._message.setText(f"設定讀取失敗：{error}" if error else "載入中…")
            self._message.setStyleSheet(f"color: {theme.RED if error else theme.TEXT_DIM};")
            return
        self._message.setText("")
        params = strategy_params_display.strategy_params(
            *strategy_params_display.allocations_from_cfg(cfg))
        for r, row in enumerate(params.rows):
            self._grid.addWidget(label(row.label, color=theme.color(row.color)), r, 0)
            value = label(row.text, name="MedNum", color=theme.color(row.color))
            self._grid.addWidget(value, r, 1, Qt.AlignRight)
        _add_params_rows(self._grid, len(params.rows), params)
        self._grid.setColumnStretch(0, 1)

    def text(self) -> str:
        return all_text(self)

    def value_color(self, text: str) -> str:
        """某個數值標籤的顏色（測試用）。"""
        for i in range(self._grid.count()):
            widget = self._grid.itemAt(i).widget()
            if widget is not None and widget.text() == text:
                return widget.styleSheet().removeprefix("color: ").rstrip(";")
        raise KeyError(text)


class AllocationDialog(QDialog):
    """三條拉桿調整一個帳戶的 Allocation；合計超過上限時不能按儲存。"""

    def __init__(self, account: str, current: tuple[float, float, float],
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"調整策略參數 — {account}")
        self.setModal(True)
        self.setMinimumWidth(380)
        lay = QVBoxLayout(self)
        lay.addWidget(label(f"{account} — 策略 Allocation", name="SectionTitle"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self.sliders: list[QSlider] = []
        self._values: list = []
        for r, ((_, name, _, color), value) in enumerate(
                zip(strategy_params_display.STRATEGIES, current)):
            grid.addWidget(label(name, color=theme.color(color)), r, 0)
            slider = QSlider(Qt.Horizontal)
            slider.setRange(0, ALLOCATION_STEPS)
            slider.setSingleStep(1)
            slider.setPageStep(1)
            slider.setValue(round(value / ALLOCATION_STEP))
            slider.valueChanged.connect(self._update)
            grid.addWidget(slider, r, 1)
            self.sliders.append(slider)
            text = label(name="MedNum", color=theme.color(color))
            grid.addWidget(text, r, 2, Qt.AlignRight)
            self._values.append(text)
        self._total = _add_params_rows(grid, len(self.sliders),
                                       strategy_params_display.strategy_params(*current))
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        self._error = label(color=theme.RED)
        self._error.setWordWrap(True)
        lay.addWidget(self._error)
        lay.addWidget(label("只改這個帳戶；儲存前會再確認一次。", name="Faint"))
        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.save_button = self.buttons.button(QDialogButtonBox.Save)
        self.save_button.setText("儲存")
        self.buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self._update()

    def values(self) -> tuple[float, float, float]:
        return tuple(round(s.value() * ALLOCATION_STEP, 2) for s in self.sliders)

    def result_values(self) -> tuple[float, float, float] | None:
        """按儲存才有值；取消／關閉 = None。"""
        return self.values() if self.result() == QDialog.Accepted else None

    def _update(self) -> None:
        values = self.values()
        params = strategy_params_display.strategy_params(*values)
        for text, row in zip(self._values, params.rows):
            text.setText(row.text)
        leverage, detail = self._total
        leverage.setText(params.leverage_text)
        leverage.setStyleSheet(f"color: {theme.color(params.leverage_color)};")
        detail.setText(params.detail)
        error = strategy_params_display.allocation_change("", {}, values).error
        self._error.setText(error)
        self._error.setVisible(bool(error))
        self.save_button.setEnabled(not error)

    def text(self) -> str:
        return all_text(self)


class RecentTradesCard(Card):
    """最近 7 天成交（trade_log.xlsx），新到舊。"""

    def __init__(self):
        super().__init__("最近成交（7 天）")
        self.table = QTableWidget(0, len(TRADE_HEADERS))
        self.table.setHorizontalHeaderLabels(TRADE_HEADERS)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(120)
        self.body.addWidget(self.table, 1)
        self._empty = label(name="Dim")
        self.body.addWidget(self._empty)
        self.set_trades(None)

    def set_trades(self, trades: list | None, error: str = "") -> None:
        """trades: trades_display.TradeLine 的清單（read_account_trades）。"""
        rows = list(trades or [])
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            cells = (row.datetime,) + row.cells()[1:]       # 7 天內會跨日：顯示完整日期時間
            for c, (text, color) in enumerate(zip(cells, row.colors())):
                item = QTableWidgetItem(text)
                item.setForeground(QColor(theme.color(color)))
                if c in (3, 4):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, c, item)
        if error:
            empty = f"成交紀錄讀取失敗：{error}"
        elif trades is None:
            empty = "載入中…"
        else:
            empty = "近 7 天沒有成交"
        self._empty.setText("" if rows else empty)
        self._empty.setVisible(not rows)
        self.table.setVisible(bool(rows))

    def rows(self) -> list[tuple[str, ...]]:
        return [tuple(self.table.item(r, c).text() for c in range(self.table.columnCount()))
                for r in range(self.table.rowCount())]

    def empty_text(self) -> str:
        return self._empty.text() if self._empty.isVisibleTo(self) else ""
