"""權益曲線（#150）與業績曲線（#166）：單一帳戶頁的互動圖、總覽卡片的近 60 天小圖。

資料是 `equity_chart.load_equity_series` 的每日 NAV（portfolio_tracker.xlsx 的 Daily，
和 Telegram 報表同一份）；這裡只畫圖，Telegram 報表仍用 matplotlib（ADR-0012）。
業績 = 排除出入金的累積報酬（`performance`，出入金來自 IB Flex 報表 `ib_flex`）。
互動圖只縮放、平移時間軸，縱軸跟著看得到的區間自動調整；滑鼠移上去顯示最近一天的
日期與數值。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6.QtCore import QPointF, Qt
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from performance import growth_index, rebase
from qt_panel import theme
from qt_panel.widgets import label

RANGES = ("1M", "3M", "YTD", "1Y", "全部")
DEFAULT_RANGE = "1Y"
SPARKLINE_DAYS = 60
CHART_HEIGHT = 280
EQUITY_MODE, PERFORMANCE_MODE = "權益", "業績"
MODES = (EQUITY_MODE, PERFORMANCE_MODE)
MODE_TIPS = {EQUITY_MODE: "每日帳戶權益（NAV），含出入金",
             PERFORMANCE_MODE: "排除出入金的累積報酬（時間加權），區間起點為 0%"}

pg.setConfigOptions(antialias=True)


def range_start(key: str, first: pd.Timestamp, last: pd.Timestamp) -> pd.Timestamp:
    """區間按鈕的起點（不早於第一筆資料）；終點一律是最後一筆資料那天。"""
    if key == "1M":
        start = last - pd.DateOffset(months=1)
    elif key == "3M":
        start = last - pd.DateOffset(months=3)
    elif key == "YTD":
        start = pd.Timestamp(year=last.year, month=1, day=1)
    elif key == "1Y":
        start = last - pd.DateOffset(years=1)
    else:
        start = first
    return max(start, first)


def recent(series: pd.Series, days: int = SPARKLINE_DAYS) -> pd.Series:
    """最後一筆資料往前 days 個日曆天（含）。"""
    if series.empty:
        return series
    return series[series.index >= series.index[-1] - pd.Timedelta(days=days)]


def change_text(series: pd.Series) -> tuple[str, str]:
    """區間報酬 → (文字, 顏色)。"""
    if len(series) < 2 or not series.iloc[0]:
        return "", theme.TEXT_DIM
    pct = (series.iloc[-1] / series.iloc[0] - 1) * 100
    return f"{pct:+.1f}%", theme.GREEN if pct >= 0 else theme.RED


def recent_change(series, flows=None) -> tuple[str, str]:
    """總覽卡片的近 60 天報酬 → (文字, 顏色)。有出入金紀錄就算業績，否則是權益變化。"""
    data = _series(series)
    if len(recent(data)) < 2:
        return "", theme.TEXT_DIM
    if flows is None:
        text, color = change_text(recent(data))
        return f"近 {SPARKLINE_DAYS} 天權益 {text}", color
    text, color = change_text(recent(growth_index(data, flows)))
    return f"近 {SPARKLINE_DAYS} 天業績 {text}", color


def _seconds(index: pd.DatetimeIndex) -> np.ndarray:
    """日期 → pyqtgraph 時間軸用的秒數（當 UTC 的午夜，軸上顯示日期不會跨日）。"""
    return (index.values.astype("datetime64[s]").astype(np.int64)).astype(float)


def _day(seconds: float) -> str:
    return f"{pd.Timestamp(int(seconds), unit='s'):%Y-%m-%d}"


def _curve_points(curve) -> list[tuple[str, float]]:
    x, y = curve.getData()
    if x is None:
        return []
    return [(_day(s), float(v)) for s, v in zip(x, y)]


def _series(series) -> pd.Series:
    if series is None:
        return pd.Series(dtype=float)
    return series.dropna().sort_index()


class _UtcDateAxis(pg.DateAxisItem):
    def __init__(self, **kw):
        super().__init__(utcOffset=0, **kw)


def _chips(keys, on_click, tips=None) -> tuple[QButtonGroup, dict[str, QPushButton]]:
    group = QButtonGroup()
    buttons = {}
    for key in keys:
        button = QPushButton(key)
        button.setObjectName("Chip")
        button.setCheckable(True)
        if tips:
            button.setToolTip(tips[key])
        button.clicked.connect(lambda _=False, k=key: on_click(k))
        group.addButton(button)
        buttons[key] = button
    return group, buttons


class EquityChart(QWidget):
    """可縮放、平移、滑鼠看值的權益／業績曲線，附 1M／3M／YTD／1Y／全部 區間按鈕。

    業績每次選區間都從區間起點 0% 算起（同 IB）；平移到起點之前顯示相對起點的報酬。
    沒有出入金紀錄時業績畫不出來，權益照常。
    """

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        bar = QHBoxLayout()
        self._mode_group, self.mode_buttons = _chips(MODES, self.set_mode, MODE_TIPS)
        self._mode_group.setParent(self)
        for button in self.mode_buttons.values():
            bar.addWidget(button)
        bar.addSpacing(12)
        self._hover = label(name="Dim")
        bar.addWidget(self._hover)
        bar.addStretch(1)
        self._range_group, self.range_buttons = _chips(RANGES, self.set_range)
        self._range_group.setParent(self)
        for button in self.range_buttons.values():
            bar.addWidget(button)
        lay.addLayout(bar)

        self.plot = pg.PlotWidget(axisItems={"bottom": _UtcDateAxis(orientation="bottom")},
                                  background=theme.SURFACE)
        self.plot.setFixedHeight(CHART_HEIGHT)
        item = self.plot.getPlotItem()
        item.hideButtons()
        item.setMenuEnabled(False)
        item.showGrid(x=True, y=True, alpha=0.15)
        for name in ("left", "bottom"):
            axis = item.getAxis(name)
            axis.setPen(pg.mkPen(theme.BORDER_STRONG))
            axis.setTextPen(pg.mkPen(theme.TEXT_DIM))
        item.getAxis("left").setWidth(64)
        item.getAxis("left").tickStrings = self._ticks
        self.viewbox = item.getViewBox()
        self.viewbox.setMouseEnabled(x=True, y=False)       # 滾輪／拖曳只動時間軸
        self.viewbox.setAutoVisible(y=True)                 # 縱軸跟著看得到的區間
        self.viewbox.sigRangeChangedManually.connect(self._on_manual_range)
        self._zero = pg.InfiniteLine(pos=0, angle=0, movable=False,
                                     pen=pg.mkPen(theme.BORDER_STRONG))
        self._curve = self.plot.plot([], [], pen=pg.mkPen(theme.NAVY, width=2))
        self._marker = pg.ScatterPlotItem(size=8, brush=pg.mkBrush(theme.ACCENT), pen=None)
        self._vline = pg.InfiniteLine(angle=90, movable=False,
                                      pen=pg.mkPen(theme.BORDER_STRONG, style=Qt.DashLine))
        for extra in (self._zero, self._vline, self._marker):
            extra.setVisible(False)
            self.plot.addItem(extra, ignoreBounds=True)
        self.plot.scene().sigMouseMoved.connect(self._on_mouse_moved)
        lay.addWidget(self.plot)
        self._empty = label(name="Dim")
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setWordWrap(True)
        lay.addWidget(self._empty)
        self._note = label(name="Faint")
        self._note.setWordWrap(True)
        lay.addWidget(self._note)

        self._mode = EQUITY_MODE
        self._range = DEFAULT_RANGE
        self._data = pd.Series(dtype=float)
        self._data_empty_text = "載入中…"
        self._flows = None                    # ib_flex.FlowsResult
        self._flows_error = ""
        self._growth = pd.Series(dtype=float)
        self._y = pd.Series(dtype=float)
        self._x = np.array([])
        self.mode_buttons[self._mode].setChecked(True)
        self._redraw()

    def _ticks(self, values, scale, spacing):
        if self._mode == PERFORMANCE_MODE:
            return [f"{v:+.1f}%" if spacing < 1 else f"{v:+.0f}%" for v in values]
        return [f"${v / 1000:,.0f}k" if abs(v) >= 10_000 else f"${v:,.0f}" for v in values]

    # ── 資料 ─────────────────────────────────────────────────────────

    def set_series(self, series, empty_text: str = "沒有權益紀錄") -> None:
        self._data = _series(series)
        self._data_empty_text = empty_text
        self._update_growth()
        self._redraw()

    def set_cash_flows(self, result, error: str = "") -> None:
        """出入金（ib_flex.FlowsResult）；None 且有 error = 讀取失敗，都沒有 = 載入中。"""
        self._flows = result
        self._flows_error = error
        self._update_growth()
        self._redraw()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.mode_buttons[mode].setChecked(True)
        self._redraw()

    def _update_growth(self) -> None:
        if self._flows is None or self._data.empty:
            self._growth = pd.Series(dtype=float)
        else:
            self._growth = growth_index(self._data, self._flows.flows)

    def _unavailable(self) -> str:
        """目前模式畫不出來的原因；畫得出來回傳空字串。"""
        if self._data.empty:
            return self._data_empty_text
        if self._mode == PERFORMANCE_MODE and self._growth.empty:
            if self._flows_error:
                return f"業績需要出入金紀錄：{self._flows_error}"
            return "出入金紀錄載入中…"
        return ""

    def _redraw(self) -> None:
        self._hide_hover()
        reason = self._unavailable()
        self.plot.setVisible(not reason)
        self._empty.setText(reason)
        self._empty.setVisible(bool(reason))
        for button in self.range_buttons.values():
            button.setEnabled(not reason)
        self._zero.setVisible(self._mode == PERFORMANCE_MODE)
        self._update_note()
        if reason:
            self._set_curve(pd.Series(dtype=float))
            return
        self.viewbox.setLimits(xMin=float(_seconds(self._data.index[:1])[0]) - 86400,
                               xMax=float(_seconds(self._data.index[-1:])[0]) + 86400)
        self.set_range(self._range)

    def _update_note(self) -> None:
        text, color = "", theme.TEXT_FAINT
        if self._mode == PERFORMANCE_MODE and self._flows is not None:
            fetched = self._flows.fetched_at
            when = f"{fetched:%m-%d %H:%M}" if fetched else "—"
            if self._flows.error:
                text = f"出入金下載失敗，使用 {when} 的快取：{self._flows.error}"
                color = theme.AMBER
            else:
                text = f"已排除出入金（IB Flex，{when} 下載）"
        self._note.setText(text)
        self._note.setStyleSheet(f"color: {color};")
        self._note.setVisible(bool(text))

    def _set_curve(self, values: pd.Series) -> None:
        self._y = values
        self._x = _seconds(values.index) if not values.empty else np.array([])
        self._curve.setData(self._x, values.to_numpy(dtype=float))

    def set_range(self, key: str) -> None:
        self._range = key
        self._check_range(key)
        if self._unavailable():
            return
        first, last = self._data.index[0], self._data.index[-1]
        start = range_start(key, first, last)
        if self._mode == PERFORMANCE_MODE:
            self._set_curve(rebase(self._growth, start))    # 區間起點 = 0%
        else:
            self._set_curve(self._data)
        x0 = float(_seconds(pd.DatetimeIndex([start]))[0])
        x1 = float(self._x[-1])
        if x1 <= x0:                                         # 只有一天的資料
            x0, x1 = x0 - 86400, x1 + 86400
        self.viewbox.setXRange(x0, x1, padding=0.02)
        self.viewbox.enableAutoRange(axis="y")

    def _check_range(self, key: str | None) -> None:
        self._range_group.setExclusive(False)
        for k, button in self.range_buttons.items():
            button.setChecked(k == key)
        self._range_group.setExclusive(True)

    def _on_manual_range(self, *_):
        # 使用者自己縮放／平移後，區間按鈕都不再代表畫面上的範圍
        self._check_range(None)
        self.viewbox.enableAutoRange(axis="y")

    # ── 滑鼠看值 ─────────────────────────────────────────────────────

    def _on_mouse_moved(self, scene_pos) -> None:
        if self._y.empty or not self.viewbox.sceneBoundingRect().contains(scene_pos):
            self._hide_hover()
            return
        x = self.viewbox.mapSceneToView(scene_pos).x()
        i = int(np.clip(np.searchsorted(self._x, x), 0, len(self._x) - 1))
        if i > 0 and abs(self._x[i - 1] - x) <= abs(self._x[i] - x):
            i -= 1
        value = float(self._y.iloc[i])
        self._vline.setPos(self._x[i])
        self._marker.setData([self._x[i]], [value])
        self._vline.setVisible(True)
        self._marker.setVisible(True)
        day = f"{self._y.index[i]:%Y-%m-%d}"
        if self._mode == PERFORMANCE_MODE:
            growth = self._growth.iloc[: i + 1]
            drawdown = (growth.iloc[-1] / growth.max() - 1) * 100
            self._hover.setText(f"{day}　業績 {value:+.2f}%　距前高 {drawdown:+.1f}%")
        else:
            peak = float(self._data.iloc[: i + 1].max())
            drawdown = (value / peak - 1) * 100 if peak else 0.0
            self._hover.setText(f"{day}　權益 ${value:,.0f}　距前高 {drawdown:+.1f}%")

    def _hide_hover(self) -> None:
        self._vline.setVisible(False)
        self._marker.setVisible(False)
        self._hover.setText("滑鼠移到圖上看數值；滾輪縮放、拖曳平移")

    # ── 讀取（測試與狀態檢查用）──

    def mode(self) -> str:
        return self._mode

    def points(self) -> list[tuple[str, float]]:
        return _curve_points(self._curve)

    def x_range_dates(self) -> tuple[str, str]:
        x0, x1 = self.viewbox.viewRange()[0]
        return _day(x0), _day(x1)

    def checked_range(self) -> str | None:
        return next((k for k, b in self.range_buttons.items() if b.isChecked()), None)

    def hover_text(self) -> str:
        return self._hover.text()

    def empty_text(self) -> str:
        return self._empty.text() if self._empty.isVisibleTo(self) else ""

    def note_text(self) -> str:
        return self._note.text() if self._note.isVisibleTo(self) else ""

    def scene_pos_of(self, day: str) -> QPointF:
        """某一天資料點在圖上的場景座標（測試用滑鼠移動）。"""
        i = self._y.index.get_loc(pd.Timestamp(day))
        return self.viewbox.mapViewToScene(QPointF(self._x[i], float(self._y.iloc[i])))


class Sparkline(pg.PlotWidget):
    """總覽卡片的近 60 天權益小圖：不能互動，點擊穿透給卡片。"""

    def __init__(self):
        super().__init__(background=None)
        self.setFixedHeight(44)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setStyleSheet("background: transparent; border: none;")
        item = self.getPlotItem()
        item.hideButtons()
        item.setMenuEnabled(False)
        item.hideAxis("left")
        item.hideAxis("bottom")
        item.setMouseEnabled(x=False, y=False)
        item.getViewBox().setDefaultPadding(0.04)
        self._curve = self.plot([], [], pen=pg.mkPen(theme.TEXT_FAINT, width=1.5))

    def set_series(self, series) -> pd.Series:
        """畫近 60 天；回傳實際畫的那段。"""
        data = recent(_series(series))
        color = change_text(data)[1] if len(data) >= 2 else theme.TEXT_FAINT
        self._curve.setPen(pg.mkPen(color, width=1.5))
        x = _seconds(data.index) if not data.empty else np.array([])
        self._curve.setData(x, data.to_numpy(dtype=float))
        return data

    def points(self) -> list[tuple[str, float]]:
        return _curve_points(self._curve)
