"""單一帳戶頁的策略參數與最近成交（#150）。

顯示內容來自和 Tk Panel 共用的 strategy_params_display、trades_display。
策略參數只顯示、不能調整：新 Panel 沒有任何寫入 accounts.json／config.json 的路徑，
調整 Allocation 仍用 Tk Panel。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QGridLayout, QHeaderView, QTableWidget, QTableWidgetItem,
)

import strategy_params_display
import trades_display

from qt_panel import theme
from qt_panel.widgets import Card, all_text, clear_layout, label

TRADE_HEADERS = ("時間", "代號", "方向", "股數", "成交價", "策略", "狀態")


class StrategyParamsCard(Card):
    """各 Strategy 的 Allocation 與合計 Leverage（唯讀）。"""

    def __init__(self):
        super().__init__("策略參數")
        self.title_row.addWidget(label("唯讀 · 調整請用 Tk Panel", name="Faint"))
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
        n = len(params.rows)
        self._grid.addWidget(label("Leverage", name="Dim"), n, 0)
        self._grid.addWidget(label(params.leverage_text, name="MedNum",
                                   color=theme.color(params.leverage_color)), n, 1, Qt.AlignRight)
        self._grid.addWidget(label(params.detail, name="Faint"), n + 1, 0, 1, 2)
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
        rows = [trades_display.trade_row(tr) for tr in trades or []]
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
