"""Portfolio 畫面（#149、#150）：上方三帳戶總覽，點選後下方顯示該帳戶的細節。

細節：指標卡、權益／業績曲線（pyqtgraph，#150、#166）、策略參數（可調整 Allocation，#220）、配置圓環、
持倉、最近成交。權益序列、出入金、策略參數、最近成交由主視窗在背景讀好再交給這裡
（set_extra）。

讀數沿用 portfolio_queries 的 QueryState；指標卡、報表日期、持倉列、圓環圖資料
都來自不含 GUI 的 portfolio_display，這裡只負責 Qt 元件。
某帳戶讀取失敗只影響它自己的卡片；有上次讀數時照樣顯示並標「快取」。
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QPushButton, QScrollArea, QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import portfolio_display as display
from portfolio_queries import QueryState
from portfolio_snapshot import PortfolioSnapshot

from qt_panel import theme
from qt_panel.account_panels import RecentTradesCard, StrategyParamsCard
from qt_panel.equity import EquityChart, Sparkline, recent_change
from qt_panel.portfolio_page import EQUITY, FLOWS, PARAMS, TRADES
from qt_panel.widgets import Card, all_text, clear_layout, dot, label
from strategy_owner import includes

OWNER_FILTERS = ("全部", "SR", "SkewLS", "SP500", display.MANUAL)
TABLE_COLUMNS = ("歸屬", "代號", "股數", "權重", "成本", "現價", "損益", "損益%", "名稱")
# 主視窗在背景讀的帳戶資料（set_extra 的 kind）
# 版面（#177）：卡片間距、右側兩欄寬度、表格列高
GAP = 10
SIDE_WIDTH = 240
DONUT_WIDTH = 420
ROW_HEIGHT = 26


def _compact(card: Card) -> None:
    card.body.setContentsMargins(14, 10, 14, 10)
    card.body.setSpacing(6)


def _table_rows(table: QTableWidget) -> None:
    table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)


class _FitLayout(QVBoxLayout):
    """不回報 height-for-width：裡面有自動換行的文字時，QScrollArea 會改用「理想高度」
    排版，視窗明明放得下也長出捲軸（#177）。關掉後只在小於最小高度時才捲動。"""

    def hasHeightForWidth(self) -> bool:
        return False


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
    source = state.portfolio.source
    return f"快取 · {source}" if is_cached(state) else source


# ── 總覽卡片 ─────────────────────────────────────────────────────────

class OverviewCard(Card):
    def __init__(self, name: str, on_click: Callable[[str], None], color: str | None = None):
        super().__init__()
        self.name = name
        self._on_click = on_click
        self.setProperty("clickable", True)
        self.setCursor(Qt.PointingHandCursor)
        self.body.setContentsMargins(14, 10, 14, 10)
        self.body.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(dot(color or theme.NAVY, 8))
        head.addWidget(label(name, name="MedNum"))
        head.addStretch(1)
        self._source = label(name="Faint")
        head.addWidget(self._source)
        self.body.addLayout(head)

        # 權益｜近 60 天小圖｜未平倉損益 排同一列，卡片才不會太高（#177）
        grid = QGridLayout()
        grid.setVerticalSpacing(0)
        grid.setHorizontalSpacing(16)
        grid.addWidget(label("權益", name="Dim"), 0, 0)
        grid.addWidget(label("未平倉損益", name="Dim"), 0, 2, Qt.AlignRight)
        self._nav = label("$—", name="BigNum")
        self._upnl = label("$—", name="MedNum")
        grid.addWidget(self._nav, 1, 0)
        grid.addWidget(self._upnl, 1, 2, Qt.AlignRight | Qt.AlignBottom)
        self.sparkline = Sparkline()
        grid.addWidget(self.sparkline, 0, 1, 2, 1)
        grid.setColumnStretch(1, 1)
        self.body.addLayout(grid)
        meta_row = QHBoxLayout()
        meta_row.setSpacing(8)
        self._meta = label(name="Faint")
        meta_row.addWidget(self._meta)
        meta_row.addStretch(1)
        self._spark_change = label(name="Faint")
        meta_row.addWidget(self._spark_change)
        self.body.addLayout(meta_row)
        self._status = label(name="Faint")
        self._status.setWordWrap(True)
        self.body.addWidget(self._status)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_state(self, state) -> None:
        pf = state.portfolio or PortfolioSnapshot.unavailable("No data yet")
        values = {m.key: m for m in display.metrics(pf)}
        self._nav.setText(values["nav"].text)
        self._upnl.setText(values["pnl"].text)
        self._upnl.setStyleSheet(f"color: {theme.color(values['pnl'].color)};")
        if pf.available:
            self._meta.setText(f"Account Exposure {values['total_exp'].text} · "
                               f"Cushion {pf.cushion:.0f}%")
        else:
            self._meta.setText("")
        self._source.setText(source_tag(state))
        text, color = card_status(state)
        self._status.setText(text)
        self._status.setStyleSheet(f"color: {theme.color(color)};")

    def set_equity(self, series, flows=None) -> None:
        """小圖畫權益；旁邊的近 60 天報酬有出入金紀錄時改算業績（入金不算賺錢）。"""
        self.sparkline.set_series(series)
        text, color = recent_change(series, flows)
        self._spark_change.setText(text)
        self._spark_change.setStyleSheet(f"color: {color};")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._on_click(self.name)
        super().mousePressEvent(event)


# ── 圓環圖 ───────────────────────────────────────────────────────────

class DonutWidget(QWidget):
    """portfolio_display.donut 的資料畫成圓環（QPainter）。"""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(140, 140)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
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
            span = -round(s.value / total * 360 * 16)     # 順時針
            pen = QPen(QColor(s.color), ring)
            pen.setCapStyle(Qt.FlatCap)
            painter.setPen(pen)
            painter.drawArc(inner, start, span)
            start += span
        text, color = self._donut.center
        painter.setPen(QColor(theme.color(color)))
        font = QFont(painter.font())
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignCenter, text)


# ── 持倉表 ───────────────────────────────────────────────────────────

class _SortItem(QTableWidgetItem):
    """依數值排序（顯示文字是格式化過的金額、百分比）。"""

    def __init__(self, text: str, key):
        super().__init__(text)
        # 數字一律轉 float：IB 回的股數、損益% 可能 int 和 float 混在一起
        self._key = float(key) if isinstance(key, (int, float)) else key

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
    if row.manual_reason:
        items[0].setToolTip(f"手動持倉進入策略 Universe（碰撞）：{row.manual_reason}")
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
        _table_rows(self.table)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        # 預設順序：ETF → 個股 → 手動，各組市值大的在前；使用者點欄位標題才排序
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
            shown = self._filter == "全部" or includes(owner, self._filter)
            self.table.setRowHidden(r, not shown)

    def visible_rows(self) -> list[tuple[str, ...]]:
        return [tuple(self.table.item(r, c).text() for c in range(self.table.columnCount()))
                for r in range(self.table.rowCount()) if not self.table.isRowHidden(r)]


# ── 單一帳戶細節 ─────────────────────────────────────────────────────

class AccountDetail(QWidget):
    """單一帳戶細節。版面要在 1920×1080 最大化時一頁放得下（#177）：

        標題列
        指標列（標題和數值同一行）
        權益／業績圖      │ 策略參數 │ 配置（圓環＋圖例並排）
        持倉              │ 最近成交
    圖和兩張表跟著視窗伸縮，表格列數多時在卡片內部捲動。
    """

    def __init__(self, on_edit_params: Callable[[], None] | None = None):
        super().__init__()
        lay = _FitLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(GAP)
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
        self._metrics_row.setSpacing(GAP)
        lay.addLayout(self._metrics_row)

        grid = QGridLayout()
        grid.setSpacing(GAP)
        equity = Card("權益／業績")
        _compact(equity)
        self.equity = EquityChart()
        equity.body.addWidget(self.equity, 1)
        grid.addWidget(equity, 0, 0)
        self.params = StrategyParamsCard(on_edit=on_edit_params)
        _compact(self.params)
        self.params.setFixedWidth(SIDE_WIDTH)
        grid.addWidget(self.params, 0, 1)
        chart = Card("配置")
        _compact(chart)
        donut_row = QHBoxLayout()
        donut_row.setSpacing(12)
        self.donut = DonutWidget()
        donut_row.addWidget(self.donut, 1)
        self._legend = QGridLayout()
        self._legend.setHorizontalSpacing(10)
        self._legend.setVerticalSpacing(2)
        donut_row.addLayout(self._legend)
        chart.body.addLayout(donut_row, 1)
        chart.setFixedWidth(DONUT_WIDTH)
        grid.addWidget(chart, 0, 2)
        self._chart_card = chart

        holdings = Card()
        _compact(holdings)
        self.holdings = HoldingsTable()
        holdings.body.addWidget(self.holdings, 1)
        grid.addWidget(holdings, 1, 0)
        self.trades = RecentTradesCard()
        _compact(self.trades)
        grid.addWidget(self.trades, 1, 1, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)
        lay.addLayout(grid, 1)

    def show_extras(self, extras: dict) -> None:
        """權益序列、策略參數、最近成交：{kind: (資料 或 None, 錯誤訊息)}；沒有的 kind 顯示載入中。"""
        series, error = extras.get(EQUITY, (None, ""))
        self.equity.set_series(series, f"權益紀錄讀取失敗：{error}" if error
                               else "沒有權益紀錄" if EQUITY in extras else "載入中…")
        self.equity.set_cash_flows(*extras.get(FLOWS, (None, "")))
        self.params.set_config(*extras.get(PARAMS, (None, "")))
        self.trades.set_trades(*extras.get(TRADES, (None, "")))

    def show_account(self, name: str, state) -> None:
        pf = state.portfolio or PortfolioSnapshot.unavailable(state.error or "No data yet")
        self._title.setText(name)
        self._report.setText(display.report_line(pf))
        text, color = display.status_line(state)
        if is_cached(state):
            text = f"快取 · {text}"
        if pf.manual_error:
            text = f"手動歸屬讀取失敗（歸屬欄只看分類）· {text}"
            color = "yellow"
        self._status.setText(text)
        self._status.setStyleSheet(f"color: {theme.color(color)};")

        clear_layout(self._metrics_row)
        for metric in display.metrics(pf):
            card = Card(metric.title)
            card.body.setContentsMargins(14, 8, 14, 8)
            value = label(metric.text, name="MedNum", color=theme.color(metric.color))
            card.title_row.addWidget(value)
            self._metrics_row.addWidget(card, 1)

        donut = display.donut(pf)
        self.donut.set_donut(donut)
        clear_layout(self._legend)
        slices = donut.slices if donut else []
        per_column = max(1, (len(slices) + 1) // 2)      # 圖例分兩欄，最多 15 項才不會撐高整排
        for i, s in enumerate(slices):
            row, col = i % per_column, (i // per_column) * 4
            self._legend.addWidget(dot(s.color, 8), row, col)
            self._legend.addWidget(label(s.label), row, col + 1)
            self._legend.addWidget(label(f"{s.pct_of_nav:.0f}%", name="Dim"), row, col + 2,
                                   Qt.AlignRight)
        self._legend.setColumnMinimumWidth(3, 8)

        rows = display.all_holding_rows(pf)
        self.holdings.set_rows(rows, "沒有持倉" if pf.available else "尚無讀數")

    def metrics_text(self) -> str:
        widgets = (self._metrics_row.itemAt(i).widget() for i in range(self._metrics_row.count()))
        return " ".join(all_text(w) for w in widgets if w is not None)

    def header_text(self) -> str:
        return " ".join(l.text() for l in (self._title, self._report, self._status))

    def legend_text(self) -> str:
        return all_text(self._chart_card)


# ── 畫面 ─────────────────────────────────────────────────────────────

class PortfolioPage(QWidget):
    def __init__(self, accounts: list[str], *, on_refresh: Callable[[], None],
                 on_edit_allocation: Callable[[str], None] | None = None,
                 colors: dict[str, str] | None = None):
        """on_edit_allocation(帳戶)：策略參數卡按「調整…」（#220）；None = 不能調整。"""
        super().__init__()
        self._states: dict = {}
        self._extras: dict[str, dict] = {name: {} for name in accounts}
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 12, 20, 12)
        root.setSpacing(GAP)

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
        row.setSpacing(GAP)
        self._cards: dict[str, OverviewCard] = {}
        for name in accounts:
            card = OverviewCard(name, self.select, (colors or {}).get(name))
            self._cards[name] = card
            row.addWidget(card, 1)
        root.addLayout(row)

        self.detail = AccountDetail(
            None if on_edit_allocation is None
            else lambda: self._selected and on_edit_allocation(self._selected))
        # 視窗夠大時一頁放得下（#177）；比細節的最小尺寸還小才整頁捲動
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidget(self.detail)
        root.addWidget(self.scroll, 1)
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

    def set_extra(self, name: str, kind: str, data, error: str = "") -> None:
        """主視窗背景讀好的權益序列（EQUITY）、出入金（FLOWS，ib_flex.FlowsResult）、
        策略設定（PARAMS）、最近成交（TRADES）。"""
        if name not in self._cards:
            return
        extras = self._extras[name]
        extras[kind] = (None if error else data, error)
        if kind in (EQUITY, FLOWS):
            series = extras.get(EQUITY, (None, ""))[0]
            flows = extras.get(FLOWS, (None, ""))[0]
            self._cards[name].set_equity(series, flows.flows if flows else None)
        if name == self._selected:
            self.detail.show_extras(self._extras[name])

    def extra(self, name: str, kind: str):
        """背景讀好的某項資料；還沒讀到或讀取失敗回 None。"""
        return self._extras.get(name, {}).get(kind, (None, ""))[0]

    def select(self, name: str) -> None:
        if name not in self._cards:
            return
        self._selected = name
        self._refresh_selection()

    def _refresh_selection(self) -> None:
        for n, card in self._cards.items():
            card.set_selected(n == self._selected)
        if not self._selected:
            return
        state = self._states.get(self._selected)
        self.detail.show_account(self._selected, state if state is not None else QueryState())
        self.detail.show_extras(self._extras[self._selected])

    # ── 讀取（測試與狀態檢查用）──

    def selected_account(self) -> str:
        return self._selected

    def card(self, name: str) -> OverviewCard:
        return self._cards[name]

    def card_text(self, name: str) -> str:
        return all_text(self._cards[name])
