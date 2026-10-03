"""新 Panel 的 Trading 分頁：手動單（#151）、Sync Pos（#152）、交給策略接管（#153）。
送去 paper 或實盤由主視窗建的送單目標決定（`order_target`，#183）。

流程和 Tk 的 `manual_order_view.ManualOrderWindow` 一樣：填欄位 → 預覽 → 再次核對
（重新向 IB 檢查）→ 確認視窗 → 送出 → 狀態追蹤（部分成交、撤單、過期、修改）。
欄位解析、預覽與狀態列文字共用 `manual_order_form`（#146），帳戶、合約、送單的
判斷全在 `manual_orders.ManualOrderService`；這裡只做 Qt 那一側。

手動單 service、Sync Pos、接管操作都由送單目標提供，送單連線一律經過送單閘門
（`broker_gate.SendGate`）。IB 在背景執行緒跑，結果排回主執行緒更新畫面。
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLineEdit, QListWidget, QPushButton, QScrollArea, QSizePolicy,
    QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import manual_order_form as order_form
from ime_guard import to_ascii_input
from manual_order_form import ManualOrderForm

from qt_panel import theme
from qt_panel.order_target import SyncOperations, TakeoverOperations
from qt_panel.sync_pos import SyncPosPanel
from qt_panel.takeover import TakeoverPanel
from qt_panel.trading_common import MainThread, background
from qt_panel.widgets import Card, label

POLL_INTERVAL_MS = 10_000
SYMBOL_SUGGESTION_LIMIT = 8
ORDER_COLUMNS = ("Account", "標的", "方向", "數量", "價格條件", "時段", "IB 狀態", "成交")
STATUS_COLUMN = 6
# 表單欄位固定寬度、靠左排：寬螢幕不會把輸入框拉滿整行（#169）
FIELD_WIDTH = 240
SYMBOL_WIDTH = 320      # 代碼欄下方要放公司名稱提示


def ascii_only(edit: QLineEdit) -> None:
    """英數欄位（#138 的 Qt 版）：關掉這個欄位的輸入法，萬一仍收到全形字元就轉半形。

    Qt 在 Windows 上對不接受輸入法的焦點元件會自己解除 IME context，
    不必像 Tk 版用 Win32 ImmAssociateContext。注音實測列在切換檢查清單。
    """
    edit.setAttribute(Qt.WA_InputMethodEnabled, False)
    edit.setInputMethodHints(Qt.ImhPreferLatin | Qt.ImhNoPredictiveText)

    def normalize(text: str) -> None:
        fixed = to_ascii_input(text)
        if fixed != text:
            cursor = edit.cursorPosition()
            edit.setText(fixed)
            edit.setCursorPosition(min(cursor, len(fixed)))

    edit.textChanged.connect(normalize)


class ManualOrderPanel(QWidget):
    """手動單：表單、確認內容、手動委託狀態。"""

    def __init__(self, account_names: list[str], service, *, dialogs,
                 log: Callable[[str], None], symbol_loader: Callable | None = None,
                 target_text: str = ""):
        super().__init__()
        self.service = service
        self._dialogs = dialogs
        self._log = log
        self._target_text = target_text
        self._main = MainThread()
        self._preview = None
        self._preview_request = None
        self._busy = False
        self._refreshing = False
        self._closed = False
        self._order_rows: dict[str, dict] = {}
        self._replacement_order_ref = None
        self._replacement_request = None
        # None = 載入中；False = 取不到清單（只少提示，不影響下單）
        self._symbols = None
        self._suggested: list[str] = []
        self._build(account_names)
        self._poll = QTimer(self, interval=POLL_INTERVAL_MS, timeout=self._poll_status)
        self._load_symbols(symbol_loader)

    # ── 版面 ─────────────────────────────────────────────────────────

    def _build(self, account_names: list[str]) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        form_card = Card("手動下單 · 一般時段 LMT / MKT · 盤前／盤後／隔夜 LMT · 整股")
        grid = QGridLayout()
        grid.setVerticalSpacing(6)
        grid.setHorizontalSpacing(12)
        grid.setColumnMinimumWidth(1, FIELD_WIDTH + 24)     # 左右兩組欄位之間留空
        grid.setColumnStretch(4, 1)                         # 多出的寬度都放最右邊

        def field(text, row, column, widget, width=FIELD_WIDTH):
            grid.addWidget(label(text), row, column)
            widget.setFixedWidth(width)
            grid.addWidget(widget, row, column + 1, Qt.AlignLeft)

        self.account_box = QComboBox()
        self.account_box.addItems(account_names)
        self.account_box.setCurrentIndex(-1)
        field("Account", 0, 0, self.account_box)

        symbol_cell = QWidget()
        symbol_lay = QVBoxLayout(symbol_cell)
        symbol_lay.setContentsMargins(0, 0, 0, 0)
        symbol_lay.setSpacing(2)
        self.symbol_edit = QLineEdit()
        self.symbol_edit.setFixedWidth(FIELD_WIDTH)
        self.symbol_hint = label(name="Faint")
        # 提示長短不影響欄寬，太長就截掉（完整名稱在滑鼠提示）
        self.symbol_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        symbol_lay.addWidget(self.symbol_edit)
        symbol_lay.addWidget(self.symbol_hint)
        field("美股代碼", 0, 2, symbol_cell, SYMBOL_WIDTH)

        self.quantity_edit = QLineEdit()
        field(order_form.SHARES_MODE, 1, 0, self.quantity_edit)
        self.price_edit = QLineEdit()
        field("自填限價（USD）", 1, 2, self.price_edit)

        self.input_mode_box = QComboBox()
        self.input_mode_box.addItems(
            [order_form.SHARES_MODE, order_form.CASH_MODE, order_form.PERCENT_MODE])
        field("輸入方式", 2, 0, self.input_mode_box)
        self.amount_edit = QLineEdit()
        self.amount_edit.setEnabled(False)
        field("美元金額（預估交易目標）", 2, 2, self.amount_edit)

        # 可自行輸入其他百分比
        self.percent_box = QComboBox()
        self.percent_box.setEditable(True)
        self.percent_box.addItems(order_form.PERCENT_CHOICES)
        self.percent_box.setCurrentIndex(-1)
        self.percent_box.setEnabled(False)
        field("權益百分比（IB 淨清算值 × %）", 3, 2, self.percent_box)

        self.action_box = QComboBox()
        self.action_box.addItems(list(order_form.ACTION_LABELS))
        field("方向", 4, 0, self.action_box)
        self.order_type_box = QComboBox()
        self.order_type_box.addItems(["LMT", "MKT"])
        field("委託類型", 4, 2, self.order_type_box)

        self.session_box = QComboBox()
        self.session_box.addItems(list(order_form.SESSION_CHOICES))
        field("交易時段", 5, 0, self.session_box)
        self.allow_short_check = QCheckBox("允許放空")
        self.allow_short_check.setEnabled(False)
        grid.addWidget(self.allow_short_check, 5, 2, 1, 2)
        form_card.body.addLayout(grid)

        helper = label("只接受美國市場的美元個股或 ETF；策略 Universe 標的會被阻擋。"
                       "盤前／盤後／隔夜只收 LMT，且只在該時段開放時送出；"
                       "隔夜單送往 IBKR Overnight 市場。", name="Faint")
        helper.setWordWrap(True)
        form_card.body.addWidget(helper)

        # 中文輸入法會把 "." 吃進組字，184.4 變 1844（#138）
        for edit in (self.symbol_edit, self.quantity_edit, self.price_edit,
                     self.amount_edit, self.percent_box.lineEdit()):
            ascii_only(edit)
        # 可編輯的 QComboBox 焦點在 combo 本身，輸入法要在它身上關
        self.percent_box.setAttribute(Qt.WA_InputMethodEnabled, False)

        actions = QHBoxLayout()
        self.preview_button = QPushButton("預覽委託")
        self.preview_button.clicked.connect(self.preview_order)
        self.confirm_button = QPushButton("再次核對並送出")
        self.confirm_button.setObjectName("Primary")
        self.confirm_button.setEnabled(False)
        self.confirm_button.clicked.connect(self.confirm_order)
        self.form_status = label(name="Dim")
        actions.addWidget(self.preview_button)
        actions.addWidget(self.confirm_button)
        actions.addWidget(self.form_status, 1)
        form_card.body.addLayout(actions)
        lay.addWidget(form_card)

        preview_card = Card("確認內容")
        self.preview_text = label("填妥欄位後按「預覽委託」。")
        self.preview_text.setWordWrap(True)
        self.preview_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        preview_card.body.addWidget(self.preview_text)
        lay.addWidget(preview_card)

        orders_card = Card("手動委託狀態")
        self.cancel_button = QPushButton("取消選定委託")
        self.cancel_button.setObjectName("Small")
        self.cancel_button.clicked.connect(self.cancel_selected)
        self.modify_button = QPushButton("修改條件")
        self.modify_button.setObjectName("Small")
        self.modify_button.clicked.connect(self.modify_selected)
        self.refresh_button = QPushButton("刷新 IB 狀態")
        self.refresh_button.setObjectName("Small")
        self.refresh_button.clicked.connect(self.refresh_status)
        for button in (self.cancel_button, self.modify_button, self.refresh_button):
            orders_card.title_row.addWidget(button)
        self.orders = QTableWidget(0, len(ORDER_COLUMNS))
        self.orders.setHorizontalHeaderLabels(ORDER_COLUMNS)
        self.orders.verticalHeader().setVisible(False)
        self.orders.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.orders.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.orders.setSelectionMode(QAbstractItemView.SingleSelection)
        self.orders.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.orders.setMinimumHeight(180)
        self.orders.itemSelectionChanged.connect(self._update_selection_actions)
        orders_card.body.addWidget(self.orders)
        self.order_status = label(name="Dim")
        orders_card.body.addWidget(self.order_status)
        lay.addWidget(orders_card, 1)

        # 代號建議浮在代碼欄下方，不另開視窗、不搶焦點
        self.symbol_suggestions = QListWidget(self)
        self.symbol_suggestions.setFocusPolicy(Qt.NoFocus)
        self.symbol_suggestions.setStyleSheet(
            f"background: {theme.SURFACE}; border: 1px solid {theme.BORDER_STRONG};"
            f"font-family: '{theme.MONO_FONT}';")
        self.symbol_suggestions.hide()
        self.symbol_suggestions.itemClicked.connect(lambda _item: self.accept_suggestion())
        self.symbol_edit.installEventFilter(self)

        for signal in (self.account_box.currentTextChanged, self.symbol_edit.textChanged,
                       self.quantity_edit.textChanged, self.amount_edit.textChanged,
                       self.percent_box.currentTextChanged, self.input_mode_box.currentTextChanged,
                       self.price_edit.textChanged, self.order_type_box.currentTextChanged,
                       self.session_box.currentTextChanged, self.action_box.currentTextChanged,
                       self.allow_short_check.toggled):
            signal.connect(self._form_changed)
        self.action_box.currentTextChanged.connect(self._action_changed)
        self.input_mode_box.currentTextChanged.connect(self._input_mode_changed)
        self.order_type_box.currentTextChanged.connect(self._order_type_changed)
        self.symbol_edit.textChanged.connect(self._update_symbol_hint)
        self.symbol_edit.textEdited.connect(self._symbol_edited)
        # 預設用帳戶權益 %（使用者 09-30 決定；Tk 版仍預設整股股數）
        self.input_mode_box.setCurrentText(order_form.PERCENT_MODE)

    # ── 生命週期 ─────────────────────────────────────────────────────

    def start(self) -> None:
        self._closed = False
        self._render_orders(self._active_orders())
        if self._has_work():
            self.refresh_status()
        self._poll.start()

    def stop(self) -> None:
        self._closed = True
        self._poll.stop()

    def _has_work(self) -> bool:
        return self.service.has_active_orders() or self.service.has_pending_notifications()

    def _poll_status(self) -> None:
        if not self._closed and not self._refreshing and self._has_work():
            self.refresh_status()

    def _background(self, work: Callable[[], object],
                    done: Callable[[object, str | None], None]) -> None:
        background(self._main, work,
                   lambda result, error: None if self._closed else done(result, error),
                   name="qt-panel-manual-order")

    # ── 代號建議 ─────────────────────────────────────────────────────

    def _load_symbols(self, loader) -> None:
        if loader is None:
            from us_symbols import load_directory as loader
        self._update_symbol_hint()

        def done(result, _error):
            self._symbols = result if result else False
            self._update_symbol_hint()

        self._background(loader, done)

    def symbols_loaded(self) -> bool:
        return self._symbols is not None

    def _update_symbol_hint(self, *_args) -> None:
        hint, key = order_form.symbol_hint(self._symbols, self.symbol_edit.text())
        self.symbol_hint.setText(hint)
        self.symbol_hint.setToolTip(hint)
        self.symbol_hint.setStyleSheet(f"color: {theme.color(key)};")

    def _symbol_edited(self, *_args) -> None:
        if not self._symbols:
            return
        text = self.symbol_edit.text()
        self._show_suggestions(self._symbols.search(text, SYMBOL_SUGGESTION_LIMIT), text)

    def _show_suggestions(self, matches, typed: str) -> None:
        lines = order_form.suggestion_lines(matches, typed)
        if not lines:
            self.hide_suggestions()
            return
        self._suggested = [symbol for symbol, _name in matches]
        box = self.symbol_suggestions
        box.clear()
        box.addItems(lines)
        box.setCurrentRow(0)
        width = 520
        bottom_right = self.symbol_edit.mapTo(
            self, self.symbol_edit.rect().bottomRight())
        row_height = box.sizeHintForRow(0) or 20
        box.setGeometry(max(0, bottom_right.x() - width), bottom_right.y() + 2,
                        width, row_height * len(lines) + 6)
        box.show()
        box.raise_()

    def suggestion_lines_shown(self) -> list[str]:
        if not self.symbol_suggestions.isVisible():
            return []
        box = self.symbol_suggestions
        return [box.item(i).text() for i in range(box.count())]

    def hide_suggestions(self) -> None:
        self._suggested = []
        self.symbol_suggestions.hide()

    def _move_suggestion(self, step: int) -> None:
        if not self._suggested:
            return
        box = self.symbol_suggestions
        box.setCurrentRow(max(0, min(box.currentRow() + step, len(self._suggested) - 1)))

    def accept_suggestion(self) -> bool:
        if not self._suggested:
            return False
        row = self.symbol_suggestions.currentRow()
        symbol = self._suggested[row if row >= 0 else 0]
        self.hide_suggestions()
        self.symbol_edit.setText(symbol)
        self.symbol_edit.setFocus()
        return True

    def eventFilter(self, obj, event) -> bool:
        if obj is self.symbol_edit:
            if event.type() == QEvent.KeyPress and self._suggested:
                key = event.key()
                if key == Qt.Key_Down:
                    self._move_suggestion(1)
                    return True
                if key == Qt.Key_Up:
                    self._move_suggestion(-1)
                    return True
                if key in (Qt.Key_Return, Qt.Key_Enter):
                    return self.accept_suggestion()
                if key == Qt.Key_Escape:
                    self.hide_suggestions()
                    return True
            elif event.type() == QEvent.FocusOut:
                # 讓點清單的動作先完成
                QTimer.singleShot(150, self._hide_if_unfocused)
        return super().eventFilter(obj, event)

    def _hide_if_unfocused(self) -> None:
        if not self._closed and not self.symbol_edit.hasFocus():
            self.hide_suggestions()

    # ── 表單 ─────────────────────────────────────────────────────────

    def form(self) -> ManualOrderForm:
        return ManualOrderForm(
            account=self.account_box.currentText(),
            symbol=self.symbol_edit.text(),
            input_mode=self.input_mode_box.currentText(),
            quantity=self.quantity_edit.text(),
            amount=self.amount_edit.text(),
            percent=self.percent_box.currentText(),
            order_type=self.order_type_box.currentText(),
            price=self.price_edit.text(),
            session=self.session_box.currentText(),
            action=self.action_box.currentText(),
            allow_short=self.allow_short_check.isChecked(),
        )

    def load_form(self, loaded: ManualOrderForm) -> None:
        self.account_box.setCurrentText(loaded.account)
        self.symbol_edit.setText(loaded.symbol)
        self.action_box.setCurrentText(loaded.action)
        self.allow_short_check.setChecked(loaded.allow_short)
        self.input_mode_box.setCurrentText(loaded.input_mode)
        self.order_type_box.setCurrentText(loaded.order_type)
        self.session_box.setCurrentText(loaded.session)
        self.quantity_edit.setText(loaded.quantity)
        self.price_edit.setText(loaded.price)

    def _set_preview_text(self, text: str, key: str = "text") -> None:
        self._set_status(self.preview_text, text, key)

    def _set_status(self, target, text: str, key: str = "text_dim") -> None:
        target.setText(text)
        target.setStyleSheet(f"color: {theme.color(key)};")

    def _form_changed(self, *_args) -> None:
        if self._preview is not None:
            self._clear_preview()
            self._set_preview_text("欄位已變更，請重新預覽委託。")

    def _clear_preview(self) -> None:
        self._preview = None
        self._preview_request = None
        self.confirm_button.setEnabled(False)

    def _action_changed(self, *_args) -> None:
        is_sell = self.action_box.currentText() == "賣出"
        if not is_sell:
            self.allow_short_check.setChecked(False)
        self.allow_short_check.setEnabled(is_sell and not self._busy)

    def _input_mode_changed(self, *_args) -> None:
        mode = self.input_mode_box.currentText()
        if mode != order_form.SHARES_MODE:
            self.quantity_edit.clear()
        if mode != order_form.CASH_MODE:
            self.amount_edit.clear()
        if mode != order_form.PERCENT_MODE:
            self.percent_box.setCurrentIndex(-1)
            self.percent_box.setEditText("")
        self._apply_input_mode_state()

    def _apply_input_mode_state(self) -> None:
        mode = self.input_mode_box.currentText()
        for widget, value in ((self.quantity_edit, order_form.SHARES_MODE),
                              (self.amount_edit, order_form.CASH_MODE),
                              (self.percent_box, order_form.PERCENT_MODE)):
            widget.setEnabled(mode == value and not self._busy)

    def _order_type_changed(self, *_args) -> None:
        if self.order_type_box.currentText() == "MKT":
            self.price_edit.clear()
        self.price_edit.setEnabled(self.order_type_box.currentText() == "LMT" and not self._busy)

    def _set_busy(self, value: bool) -> None:
        self._busy = value
        for widget in (self.preview_button, self.refresh_button, self.account_box,
                       self.symbol_edit, self.input_mode_box, self.order_type_box,
                       self.session_box, self.action_box):
            widget.setEnabled(not value)
        if value:
            self.hide_suggestions()
        self._apply_input_mode_state()
        self.price_edit.setEnabled(not value and self.order_type_box.currentText() == "LMT")
        self.allow_short_check.setEnabled(not value and self.action_box.currentText() == "賣出")
        self.confirm_button.setEnabled(not value and self._preview is not None)
        self._update_selection_actions()

    def busy(self) -> bool:
        return self._busy

    # ── 預覽 → 再次核對 → 確認 → 送出 ─────────────────────────────────

    def preview_order(self) -> None:
        if self._busy:
            return
        try:
            request = order_form.parse_request(self.form())
        except ValueError as exc:
            self._dialogs.show_message("手動下單", str(exc), error=True)
            return
        self._set_busy(True)
        self._set_status(self.form_status, "正在檢查 Account、Universe 與 IB 合約…")
        service = self.service

        def work():
            return service.preview(request.account_name, request.symbol,
                                   request.quantity, request.limit_price,
                                   **order_form.preview_kwargs(request))

        self._background(work, lambda result, error: self._preview_done(request, result, error))

    def _preview_done(self, request, preview, error) -> None:
        self._set_busy(False)
        if error:
            self._clear_preview()
            self._set_status(self.form_status, "無法預覽")
            self._set_preview_text(error, "red")
            return
        self._preview = preview
        self._preview_request = request
        self._set_status(self.form_status, f"IB 合約已確認：{preview.primary_exchange}")
        self._set_preview_text(preview.confirmation_text,
                               "yellow" if preview.price_warning else "text")
        self.confirm_button.setEnabled(True)

    def confirm_order(self) -> None:
        if self._busy or self._preview is None:
            return
        try:
            current = order_form.parse_request(self.form())
        except ValueError as exc:
            self._dialogs.show_message("手動下單", str(exc), error=True)
            return
        if current != self._preview_request:
            self._form_changed()
            return
        preview = self._preview
        self._set_busy(True)
        self._set_status(self.form_status, "正在重新檢查持倉與委託…")
        self._background(lambda: self.service.validate_preview(preview),
                         lambda fresh, error: self._validated(preview, current, fresh, error))

    def _validated(self, preview, request, fresh, error) -> None:
        self._set_busy(False)
        if error or fresh != preview:
            self._clear_preview()
            self._set_status(self.form_status, "預覽已失效，請重新預覽")
            self._set_preview_text(error or "資料在預覽後變更，請重新預覽", "red")
            return
        try:
            unchanged = (self._preview == preview
                         and order_form.parse_request(self.form()) == request)
        except ValueError:
            unchanged = False
        if not unchanged:
            self._form_changed()
            return
        prompt = fresh.confirmation_text
        if self._target_text:
            prompt += f"\n\n送往：{self._target_text}"
        if not self._dialogs.confirm("再次核對委託", prompt + "\n\n確認送出這筆委託？",
                                     warning=bool(fresh.price_warning)):
            return
        self._set_busy(True)
        self._set_status(self.form_status, "送單中…")
        self._background(lambda: self.service.submit(preview), self._submitted)

    def _submitted(self, result, error) -> None:
        self._set_busy(False)
        self._clear_preview()
        if error:
            self._set_status(self.form_status, "送單前重新檢查未通過")
            self._set_preview_text(error, "red")
            return
        self._set_status(self.form_status, order_form.submit_status_text(result),
                         "red" if order_form.needs_attention(result) else "green")
        self._log(order_form.submit_log_line(result))
        self._render_orders(self._active_orders())
        self.refresh_status()

    # ── 委託狀態 ─────────────────────────────────────────────────────

    def _active_orders(self) -> list[dict]:
        return self.service.list_orders(active_only=True)

    def selected_order(self) -> dict | None:
        rows = self.orders.selectionModel().selectedRows() if self.orders.selectionModel() else []
        if not rows:
            return None
        item = self.orders.item(rows[0].row(), 0)
        return self._order_rows.get(item.data(Qt.UserRole)) if item else None

    def select_order(self, order_ref: str) -> None:
        for row in range(self.orders.rowCount()):
            if self.orders.item(row, 0).data(Qt.UserRole) == order_ref:
                self.orders.selectRow(row)
                return

    def order_table(self) -> list[tuple[str, ...]]:
        return [tuple(self.orders.item(r, c).text() for c in range(self.orders.columnCount()))
                for r in range(self.orders.rowCount())]

    def _update_selection_actions(self, *_args) -> None:
        row = self.selected_order()
        self.cancel_button.setEnabled(not self._busy and order_form.can_cancel(row))
        self.modify_button.setEnabled(not self._busy and order_form.can_modify(row))

    def cancel_selected(self) -> None:
        row = self.selected_order()
        if row is None or self._busy:
            return
        if not self._dialogs.confirm("取消手動委託", order_form.cancel_prompt(row)):
            return
        self._request_cancel(row["order_ref"], None)

    def modify_selected(self) -> None:
        row = self.selected_order()
        if row is None or self._busy or row.get("order_type", "LMT") == "MKT":
            return
        if not order_form.form_matches_row(self.form(), row):
            self.load_form(order_form.form_from_row(row))
            self._set_status(self.form_status, "已載入選定委託；調整價格或股數後再次按修改。")
            self._set_preview_text("修改流程會先等待 IB 確認取消，再提供新委託預覽。")
            return
        try:
            request = order_form.parse_request(self.form())
            quantity = order_form.modify_quantity(request)
            order_form.check_modify(row, request, quantity)
        except ValueError as exc:
            self._dialogs.show_message("修改手動委託", str(exc), error=True)
            return
        if not self._dialogs.confirm("取消後修改手動委託",
                                     order_form.modify_prompt(row, request, quantity)):
            return
        self._request_cancel(row["order_ref"], request)

    def _request_cancel(self, order_ref: str, replacement_request) -> None:
        row = self._order_rows.get(order_ref)
        if row is not None:
            pending = order_form.cancel_pending_row(row)
            self._order_rows[order_ref] = pending
            for r in range(self.orders.rowCount()):
                if self.orders.item(r, 0).data(Qt.UserRole) == order_ref:
                    self.orders.item(r, STATUS_COLUMN).setText(pending["status_label"])
        self._set_busy(True)
        self._set_status(self.order_status, "已記錄取消要求，等待 IB 確認…")
        self._background(
            lambda: self.service.cancel(order_ref),
            lambda result, error: self._cancelled(order_ref, replacement_request, result, error))

    def _cancelled(self, order_ref, replacement_request, result, error) -> None:
        self._set_busy(False)
        if error:
            self._set_status(self.order_status, f"取消結果待確認：{error}", "red")
            self._render_orders(self._active_orders())
            return
        status = result["status"]
        if replacement_request is not None:
            self._replacement_order_ref = order_ref
            self._replacement_request = replacement_request
        if status in {"CANCEL_PENDING", "CANCEL_UNKNOWN", "SUBMISSION_UNKNOWN"}:
            self._set_status(self.order_status,
                             result.get("status_message") or "取消結果待確認；委託仍保持未結案",
                             "red")
            self.refresh_status()
        else:
            self._set_status(self.order_status, result.get("status_label", status))
            self._finish_replacement_if_ready(result)
            self._render_orders(self._active_orders())

    def _finish_replacement_if_ready(self, row: dict) -> None:
        if (self._replacement_order_ref is None
                or row.get("order_ref") != self._replacement_order_ref):
            return
        request = self._replacement_request
        self._replacement_order_ref = None
        self._replacement_request = None
        replacement = order_form.replacement_after_cancel(row, request)
        if replacement.form is None:
            self._set_preview_text(replacement.message)
            return
        self.load_form(replacement.form)
        self._clear_preview()
        self._set_status(self.form_status, "IB 已確認取消；請預覽並再次確認新委託。")
        self._set_preview_text(replacement.message)

    def refresh_status(self) -> None:
        if self._closed or self._refreshing:
            return
        self._refreshing = True
        self.refresh_button.setEnabled(False)
        self._set_status(self.order_status, "正在向 IB 查詢手動委託狀態…")
        self._background(self.service.refresh_status, self._refreshed)

    def refreshing(self) -> bool:
        return self._refreshing

    def _refreshed(self, result, error) -> None:
        self._refreshing = False
        self.refresh_button.setEnabled(not self._busy)
        if error:
            self._set_status(self.order_status, f"IB 狀態查詢失敗：{error}", "red")
            self._render_orders(self._active_orders())
            return
        self._render_orders(result["orders"])
        if self._replacement_order_ref is not None:
            previous = next((row for row in self.service.list_orders()
                             if row.get("order_ref") == self._replacement_order_ref), None)
            if previous and not order_form.replacement_waiting(previous):
                self._finish_replacement_if_ready(previous)
        errors = result.get("errors", [])
        self._set_status(self.order_status, "；".join(errors) if errors else "狀態已刷新",
                         "red" if errors else "text_dim")

    def _render_orders(self, rows) -> None:
        selected = self.selected_order()
        self.orders.setRowCount(0)
        self._order_rows = {}
        for row in order_form.open_orders(rows):
            ref = row["order_ref"]
            self._order_rows[ref] = row
            r = self.orders.rowCount()
            self.orders.insertRow(r)
            for c, value in enumerate(order_form.order_row_values(row)):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(Qt.AlignCenter)
                if c == 0:
                    item.setData(Qt.UserRole, ref)
                self.orders.setItem(r, c, item)
        if selected is not None:
            self.select_order(selected["order_ref"])
        self._update_selection_actions()


class TradingPage(QWidget):
    """Trading 分頁：送單目標說明＋手動單／Sync Pos／交給策略接管切換。
    沒有可用的送單服務時只顯示原因。live = 正式模式（送實盤）。"""

    def __init__(self, account_names: list[str], service, *, unavailable: str = "",
                 target_text: str = "", dialogs, log: Callable[[str], None],
                 symbol_loader: Callable | None = None, sync: SyncOperations | None = None,
                 takeover: TakeoverOperations | None = None, live: bool = False):
        super().__init__()
        # 內容放進捲動區：確認內容很多行時可以捲，不會撐大主視窗被螢幕切掉（#171）
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        content = QWidget()
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setWidget(content)
        outer.addWidget(self.scroll_area)
        lay = QVBoxLayout(content)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(12)
        banner = Card()
        if service is None:
            title = ("正式模式：Trading 目前不能送單" if live
                     else "並行模式：Trading 只接 paper Gateway，目前不能送單")
            detail = unavailable
        elif live:
            title = "正式模式：手動單、Sync Pos、接管送到實盤帳戶"
            detail = ("每個帳戶送到自己的 Gateway，送出前一律跳確認視窗。帳戶、連線身分、"
                      "紀錄（手動單、手動歸屬、state、trade_log.xlsx）與 Telegram 通知都和 "
                      "Tk Panel 相同；正式模式下 Tk Panel 不能送單。")
        else:
            title = f"並行模式：手動單、Sync Pos、接管一律送往 {target_text}，不會送到實盤"
            detail = ("帳戶選單照列三個帳戶，但全部導向這個 paper 帳號。"
                      "每條送單連線都會先確認 Gateway 管理的帳號全部是 paper（DU/DF 開頭），"
                      "否則斷線拒絕。paper 不寫實盤的 state、歸屬與成交紀錄，也不發 Telegram。"
                      "實盤下單請用 Tk Panel。")
        self.banner_title = label(title, color=theme.WARN_TEXT)
        self.banner_title.setStyleSheet(f"color: {theme.WARN_TEXT}; font-weight: 600;")
        self.banner_title.setWordWrap(True)
        self.banner_detail = label(detail, name="Dim")
        self.banner_detail.setWordWrap(True)
        banner.body.addWidget(self.banner_title)
        banner.body.addWidget(self.banner_detail)
        lay.addWidget(banner)
        self.manual: ManualOrderPanel | None = None
        self.sync = None
        self.takeover = None
        if service is None:
            lay.addStretch(1)
            return
        tabs = QHBoxLayout()
        tabs.setSpacing(6)
        self.manual_tab = QPushButton("手動單")
        self.sync_tab = QPushButton("Sync Pos")
        self.takeover_tab = QPushButton("交給策略接管")
        stack = QStackedWidget()
        self._stack = stack
        self.manual = ManualOrderPanel(account_names, service, dialogs=dialogs, log=log,
                                       symbol_loader=symbol_loader, target_text=target_text)
        stack.addWidget(self.manual)
        pages = [(self.manual_tab, self.manual)]
        if takeover is not None:
            self.takeover = TakeoverPanel(account_names, takeover, dialogs=dialogs, log=log)
        if sync is not None:
            self.sync = SyncPosPanel(
                account_names, sync, dialogs=dialogs, log=log,
                on_takeover=self.open_takeover if self.takeover is not None else None)
            stack.addWidget(self.sync)
            pages.append((self.sync_tab, self.sync))
        if self.takeover is not None:
            stack.addWidget(self.takeover)
            pages.append((self.takeover_tab, self.takeover))
        group = QButtonGroup(self)
        for button, page in pages:
            button.setObjectName("Chip")
            button.setCheckable(True)
            button.clicked.connect(lambda _=False, p=page: stack.setCurrentWidget(p))
            group.addButton(button)
            tabs.addWidget(button)
        self.manual_tab.setChecked(True)
        tabs.addStretch(1)
        lay.addLayout(tabs)
        lay.addWidget(stack, 1)

    def open_takeover(self, account: str, symbol: str) -> str:
        """Sync Pos 暫停列的「交給策略接管」：切到接管畫面並開始算該標的的預覽。
        回傳不能開始的原因（空字串 = 已開始），不能開始就留在原畫面。"""
        problem = self.takeover.start_for(account, symbol)
        if not problem:
            self.takeover_tab.setChecked(True)
            self._stack.setCurrentWidget(self.takeover)
        return problem

    def start(self) -> None:
        if self.manual is not None:
            self.manual.start()

    def stop(self) -> None:
        if self.manual is not None:
            self.manual.stop()
        if self.sync is not None:
            self.sync.stop()
        if self.takeover is not None:
            self.takeover.stop()
