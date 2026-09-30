"""Portfolio 畫面（#149）：上方三帳戶總覽，點選後下方顯示該帳戶的權益讀數與持倉。

讀數沿用 portfolio_queries 的 QueryState；指標卡、報表日期、持倉列、圓環圖資料
都來自 portfolio_display（和 Tk PortfolioView 共用），這裡只負責 Qt 元件。
某帳戶讀取失敗只影響它自己的卡片；有上次讀數時照樣顯示並標「快取」。
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QGridLayout, QHBoxLayout, QHeaderView, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import portfolio_display as display

from qt_panel import theme
from qt_panel.widgets import Card, all_text, clear_layout, dot, label

OWNER_FILTERS = ("全部", "SR", "SkewLS", "SP500", display.MANUAL)
TABLE_COLUMNS = ("歸屬", "代號", "股數", "權重", "成本", "現價", "損益", "損益%", "名稱")


def _color(key: str) -> str:
    return theme.THEME_KEY_COLOR.get(key, theme.TEXT)


def _time(state) -> str:
    return state.updated_at.astimezone().strftime("%m-%d %H:%M:%S") if state.updated_at else ""


def is_cached(state) -> bool:
    """畫面上的讀數不是最近一次查詢的結果（最近一次失敗，顯示的是上次成功的）。"""
    return state.portfolio is not None and bool(state.error)


def card_status(state) -> tuple[str, str]:
    """總覽卡片底下的一行狀態 → (文字, theme key)。"""
    if state.running:
        suffix = f"（顯示 {_time(state)} 的讀數）" if state.portfolio else ""
        return f"更新中…{suffix}", "text_dim"
    if is_cached(state):
        return f"讀取失敗：{state.error} — 快取，顯示 {_time(state)} 的讀數", "yellow"
    if state.error:
        return f"讀取失敗：{state.error}（其他帳戶不受影響）", "red"
    if state.portfolio:
        return f"更新於 {_time(state)}", "text_dim"
    return "尚無資料", "text_dim"


def source_tag(state) -> str:
    """讀數來源（例如 IB Live [Taylor]）；快取時加註。"""
    if not state.portfolio:
        return ""
    source = state.portfolio.get("source", "Excel")
    return f"快取 · {source}" if is_cached(state) else source


# ── 總覽卡片 ─────────────────────────────────────────────────────────

class OverviewCard(Card):
    def __init__(self, name: str, on_click: Callable[[str], None]):
        super().__init__()
        self.name = name
        self._on_click = on_click
        self.setProperty("clickable", True)
        self.setCursor(Qt.PointingHandCursor)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(dot(theme.ACCOUNT_COLOR.get(name, theme.NAVY), 8))
        head.addWidget(label(name, name="MedNum"))
        head.addStretch(1)
        self._source = label(name="Faint")
        head.addWidget(self._source)
        self.body.addLayout(head)

        grid = QGridLayout()
        grid.setVerticalSpacing(2)
        grid.addWidget(label("權益", name="Dim"), 0, 0)
        grid.addWidget(label("未平倉損益", name="Dim"), 0, 1, Qt.AlignRight)
        self._nav = label("$—", name="BigNum")
        self._upnl = label("$—", name="MedNum")
        grid.addWidget(self._nav, 1, 0)
        grid.addWidget(self._upnl, 1, 1, Qt.AlignRight | Qt.AlignBottom)
        self.body.addLayout(grid)
        self._meta = label(name="Faint")
        self.body.addWidget(self._meta)
        self._status = label(name="Faint")
        self._status.setWordWrap(True)
        self.body.addWidget(self._status)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_state(self, state) -> None:
        pf = state.portfolio or {"available": False}
        values = {m.key: m for m in display.metrics(pf)}
        self._nav.setText(values["nav"].text)
        self._upnl.setText(values["pnl"].text)
        self._upnl.setStyleSheet(f"color: {_color(values['pnl'].color)};")
        if pf.get("available"):
            self._meta.setText(f"Leverage {display.leverage(pf):.2f}x · "
                               f"Cushion {pf['cushion']:.0f}%")
        else:
            self._meta.setText("")
        self._source.setText(source_tag(state))
        text, color = card_status(state)
        self._status.setText(text)
        self._status.setStyleSheet(f"color: {_color(color)};")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._on_click(self.name)
        super().mousePressEvent(event)


# ── 圓環圖 ───────────────────────────────────────────────────────────

class DonutWidget(QWidget):
    """portfolio_display.donut 的資料畫成圓環（Tk 版是 matplotlib，這裡用 QPainter）。"""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(180, 180)
        self._donut: display.Donut | None = None

    def set_donut(self, donut: display.Donut | None) -> None:
        self._donut = donut
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        side = min(self.width(), self.height()) - 16
        rect = QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)
        if self._donut is None:
            painter.setPen(QColor(theme.TEXT_FAINT))
            painter.drawText(rect, Qt.AlignCenter, "沒有持倉資料")
            return
        total = sum(s.value for s in self._donut.slices) or 1
        ring = side * 0.19
        inner = rect.adjusted(ring / 2, ring / 2, -ring / 2, -ring / 2)
        start = 90 * 16
        for s in self._donut.slices:
            span = -round(s.value / total * 360 * 16)     # 順時針，同 Tk 的 counterclock=False
            pen = QPen(QColor(s.color), ring)
            pen.setCapStyle(Qt.FlatCap)
            painter.setPen(pen)
            painter.drawArc(inner, start, span)
            start += span
        text, color = self._donut.center
        painter.setPen(QColor(_color(color)))
        font = QFont(painter.font())
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, text)


# ── 持倉表 ───────────────────────────────────────────────────────────

class _SortItem(QTableWidgetItem):
    """依數值排序（顯示文字是格式化過的金額、百分比）。"""

    def __init__(self, text: str, key):
        super().__init__(text)
        self._key = key

    def __lt__(self, other) -> bool:
        if isinstance(other, _SortItem) and type(self._key) is type(other._key):
            return self._key < other._key
        return super().__lt__(other)


def _row_items(row: display.HoldingRow) -> list[QTableWidgetItem]:
    cells = row.cells()
    keys = (row.owner, row.symbol, row.shares, row.weight_pct, row.avg_cost, row.mkt_px,
            row.upnl, row.upnl_pct, row.name)
    items = [_SortItem(row.owner, row.owner)] + [
        _SortItem(text, key) for text, key in zip(cells, keys[1:])]
    items[0].setForeground(QColor(theme.OWNER_COLOR.get(row.owner, theme.TEXT)))
    for i in (6, 7):
        pnl = row.upnl if i == 6 else row.upnl_pct
        if pnl:
            items[i].setForeground(QColor(theme.GREEN if pnl > 0 else theme.RED))
    for i in range(2, 8):
        items[i].setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return items


class HoldingsTable(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        bar = QHBoxLayout()
        bar.addWidget(label("持倉", name="SectionTitle"))
        bar.addStretch(1)
        self._filter = "全部"
        self._filters = QButtonGroup(self, exclusive=True)
        self.filter_buttons: dict[str, QPushButton] = {}
        for key in OWNER_FILTERS:
            button = QPushButton(key)
            button.setObjectName("Chip")
            button.setCheckable(True)
            button.setChecked(key == self._filter)
            button.clicked.connect(lambda _=False, k=key: self.set_filter(k))
            self._filters.addButton(button)
            self.filter_buttons[key] = button
            bar.addWidget(button)
        lay.addLayout(bar)
        self.table = QTableWidget(0, len(TABLE_COLUMNS))
        self.table.setHorizontalHeaderLabels(TABLE_COLUMNS)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        # 預設照 Tk 的順序（ETF → 個股 → 手動，各組市值大的在前）；使用者點欄位標題才排序
        self.table.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)
        lay.addWidget(self.table, 1)
        self._empty = label("", name="Dim")
        lay.addWidget(self._empty)

    def set_rows(self, rows: list[display.HoldingRow], empty_text: str) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, item in enumerate(_row_items(row)):
                self.table.setItem(r, c, item)
        self.table.setSortingEnabled(True)
        self._empty.setText("" if rows else empty_text)
        self._empty.setVisible(not rows)
        self._apply_filter()

    def set_filter(self, key: str) -> None:
        self._filter = key
        self.filter_buttons[key].setChecked(True)
        self._apply_filter()

    def _apply_filter(self) -> None:
        for r in range(self.table.rowCount()):
            owner = self.table.item(r, 0).text()
            self.table.setRowHidden(r, self._filter not in ("全部", owner))

    def visible_rows(self) -> list[tuple[str, ...]]:
        return [tuple(self.table.item(r, c).text() for c in range(self.table.columnCount()))
                for r in range(self.table.rowCount()) if not self.table.isRowHidden(r)]


# ── 單一帳戶細節 ─────────────────────────────────────────────────────

class AccountDetail(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        head = QHBoxLayout()
        self._title = label(name="MedNum")
        head.addWidget(self._title)
        head.addSpacing(12)
        self._report = label(name="Dim")
        head.addWidget(self._report)
        head.addStretch(1)
        self._status = label(name="Faint")
        head.addWidget(self._status)
        lay.addLayout(head)

        self._metrics_row = QHBoxLayout()
        self._metrics_row.setSpacing(12)
        lay.addLayout(self._metrics_row)

        body = QHBoxLayout()
        body.setSpacing(12)
        chart = Card("配置")
        self.donut = DonutWidget()
        chart.body.addWidget(self.donut, 1)
        self._legend = QGridLayout()
        self._legend.setHorizontalSpacing(10)
        self._legend.setVerticalSpacing(2)
        chart.body.addLayout(self._legend)
        chart.setFixedWidth(300)
        body.addWidget(chart)
        holdings = Card()
        self.holdings = HoldingsTable()
        holdings.body.addWidget(self.holdings, 1)
        body.addWidget(holdings, 1)
        lay.addLayout(body, 1)
        self._chart_card = chart

    def show_account(self, name: str, state) -> None:
        pf = state.portfolio or {"available": False, "error": state.error or "No data yet"}
        self._title.setText(name)
        self._report.setText(display.report_line(pf))
        text, color = display.status_line(state)
        if is_cached(state):
            text = f"快取 · {text}"
        self._status.setText(text)
        self._status.setStyleSheet(f"color: {_color(color)};")

        clear_layout(self._metrics_row)
        for metric in display.metrics(pf):
            card = Card(metric.title)
            value = label(metric.text, name="MedNum", color=_color(metric.color))
            card.body.addWidget(value, 0, Qt.AlignRight)
            self._metrics_row.addWidget(card, 1)

        donut = display.donut(pf)
        self.donut.set_donut(donut)
        clear_layout(self._legend)
        for i, s in enumerate(donut.slices if donut else []):
            self._legend.addWidget(dot(s.color, 8), i, 0)
            self._legend.addWidget(label(s.label), i, 1)
            self._legend.addWidget(label(f"{s.pct_of_nav:.0f}%", name="Dim"), i, 2, Qt.AlignRight)
        self._legend.setColumnStretch(1, 1)

        rows = display.all_holding_rows(pf)
        self.holdings.set_rows(rows, "沒有持倉" if pf.get("available") else "尚無讀數")

    def metrics_text(self) -> str:
        widgets = (self._metrics_row.itemAt(i).widget() for i in range(self._metrics_row.count()))
        return " ".join(all_text(w) for w in widgets if w is not None)

    def header_text(self) -> str:
        return " ".join(l.text() for l in (self._title, self._report, self._status))

    def legend_text(self) -> str:
        return all_text(self._chart_card)


# ── 畫面 ─────────────────────────────────────────────────────────────

class PortfolioPage(QWidget):
    account_selected = Signal(str)

    def __init__(self, accounts: list[str], *, on_refresh: Callable[[], None]):
        super().__init__()
        self._states: dict = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(16)

        head = QHBoxLayout()
        head.addWidget(label("帳戶總覽", name="SectionTitle"))
        head.addStretch(1)
        head.addWidget(label("美股盤中整點自動讀取", name="Faint"))
        self.refresh_button = QPushButton("重新讀取 IB")
        self.refresh_button.setToolTip("三個帳戶各查一次權益與持倉（唯讀，不下單）")
        self.refresh_button.clicked.connect(on_refresh)
        head.addWidget(self.refresh_button)
        root.addLayout(head)

        row = QHBoxLayout()
        row.setSpacing(12)
        self._cards: dict[str, OverviewCard] = {}
        for name in accounts:
            card = OverviewCard(name, self.select)
            self._cards[name] = card
            row.addWidget(card, 1)
        root.addLayout(row)

        self.detail = AccountDetail()
        root.addWidget(self.detail, 1)
        self._selected = accounts[0] if accounts else ""
        for name in accounts:
            self._states[name] = None
        self._refresh_selection()

    def set_state(self, name: str, state) -> None:
        card = self._cards.get(name)
        if card is None:
            return
        self._states[name] = state
        card.set_state(state)
        if name == self._selected:
            self.detail.show_account(name, state)

    def select(self, name: str) -> None:
        if name not in self._cards:
            return
        self._selected = name
        self._refresh_selection()
        self.account_selected.emit(name)

    def _refresh_selection(self) -> None:
        for n, card in self._cards.items():
            card.set_selected(n == self._selected)
        state = self._states.get(self._selected)
        if state is not None:
            self.detail.show_account(self._selected, state)
        elif self._selected:
            from portfolio_queries import QueryState
            self.detail.show_account(self._selected, QueryState())

    # ── 讀取（測試與狀態檢查用）──

    def selected_account(self) -> str:
        return self._selected

    def card(self, name: str) -> OverviewCard:
        return self._cards[name]

    def card_text(self, name: str) -> str:
        return all_text(self._cards[name])
