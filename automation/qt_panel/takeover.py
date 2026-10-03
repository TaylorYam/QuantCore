"""新 Panel 的交給策略接管（#153）：選帳戶與標的 → 計算進度 → 預覽 → 確認才送。

流程和 Tk 的 `takeover_window` 一樣；預覽內容是 `TakeoverPreview.lines()`，按鈕、
確認視窗與結果標題共用 `takeover_preview_text`，預覽與送單沿用 `strategy_takeover`。
也可以從 Sync Pos 預覽的暫停列直接進來（同 Tk）。

暫停清單、預覽、送單與通知是送單目標給的 `order_target.TakeoverOperations`
（#183）：連線、clientId、歸屬紀錄與通知對象都由送單目標決定，這裡只有畫面。
拒絕（TakeoverRefused）在狀態列說明原因；預覽過期（TakeoverStale）換上新預覽並標出
原因，要再確認一次。計算中按「放棄」或關掉 Panel，結果直接丟掉。
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

import takeover_preview_text as preview_text

from qt_panel import theme
from qt_panel.order_target import TakeoverOperations
from qt_panel.trading_common import MainThread, background
from qt_panel.widgets import Card, clear_layout, label, mono

FIELD_WIDTH = 240
SYMBOL_WIDTH = 140


class TakeoverPanel(QWidget):
    def __init__(self, account_names: list[str], takeover: TakeoverOperations, *,
                 dialogs, log: Callable[[str], None]):
        super().__init__()
        self._takeover = takeover
        self._dialogs = dialogs
        self._log = log
        self._main = MainThread()
        self._generation = 0          # 計算的代號：放棄或重算後，舊的結果不採用
        self._computing = False
        # 背景計算還連著 IB（同一個 clientId）：放棄後也要等它結束才能重算
        self._worker_running = False
        self._sending = False
        self._closed = False
        self._preview = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        card = Card("交給策略接管 · 把手動持倉交還策略（只送這一檔）")
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(label("Account"))
        self.account_box = QComboBox()
        self.account_box.addItems(account_names)
        self.account_box.setFixedWidth(FIELD_WIDTH)
        row.addWidget(self.account_box)
        row.addWidget(label("標的"))
        self.symbol_box = QComboBox()
        self.symbol_box.setFixedWidth(SYMBOL_WIDTH)
        row.addWidget(self.symbol_box)
        self.compute_button = QPushButton("計算預覽")
        self.compute_button.setObjectName("Primary")
        self.compute_button.clicked.connect(self.compute)
        self.discard_button = QPushButton("放棄")
        self.discard_button.setEnabled(False)
        self.discard_button.clicked.connect(self.discard)
        row.addWidget(self.compute_button)
        row.addWidget(self.discard_button)
        row.addStretch(1)
        card.body.addLayout(row)
        helper = label("標的只列這個帳戶被手動持倉暫停的（Sync Pos 計算時發現）。預覽該檔的"
                       "策略整數目標與差額，確認後只送這一檔的 LMT；成交並與 IB 持倉對上後"
                       "才解除暫停。", name="Faint")
        helper.setWordWrap(True)
        card.body.addWidget(helper)
        self.status = label(name="Dim")
        self.status.setWordWrap(True)
        card.body.addWidget(self.status)
        self.progress = QPlainTextEdit()
        self.progress.setObjectName("Log")
        self.progress.setReadOnly(True)
        self.progress.setFixedHeight(100)
        self.progress.hide()
        card.body.addWidget(self.progress)
        lay.addWidget(card)

        self.preview = Card("預覽")
        self.title = label(name="SectionTitle")
        self.preview.body.addWidget(self.title)
        self.note = label()
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"color: {theme.color('red')}; font-weight: 600;")
        self.note.hide()
        self.preview.body.addWidget(self.note)
        self._lines = QVBoxLayout()
        self._lines.setSpacing(4)
        self.preview.body.addLayout(self._lines)
        buttons = QHBoxLayout()
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.cancel)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        self.confirm_button = QPushButton()
        self.confirm_button.setObjectName("Primary")
        self.confirm_button.clicked.connect(self.confirm)
        buttons.addWidget(self.confirm_button)
        self.preview.body.addLayout(buttons)
        self.preview.hide()
        lay.addWidget(self.preview)
        lay.addStretch(1)

        self.account_box.currentTextChanged.connect(self._account_changed)
        self.symbol_box.currentTextChanged.connect(self._symbol_changed)
        self._reload_symbols(announce=True)

    # ── 生命週期 ─────────────────────────────────────────────────────

    def stop(self) -> None:
        self._closed = True
        self._generation += 1

    def computing(self) -> bool:
        return self._computing

    def sending(self) -> bool:
        return self._sending

    def showEvent(self, event) -> None:
        # Sync Pos 剛發現新的暫停時，切過來就看得到
        super().showEvent(event)
        if not self._computing and not self._sending and self._preview is None:
            self._reload_symbols(announce=False)

    def _set_status(self, text: str, key: str = "text_dim") -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {theme.color(key)};")

    def _update_buttons(self) -> None:
        idle = not self._computing and not self._sending
        self.compute_button.setEnabled(idle and not self._worker_running
                                       and self.symbol_box.count() > 0)
        self.account_box.setEnabled(idle)
        self.symbol_box.setEnabled(idle)
        self.discard_button.setEnabled(self._computing)
        ready = idle and self._preview is not None
        self.confirm_button.setEnabled(ready)
        self.cancel_button.setEnabled(ready)

    # ── 帳戶與標的 ───────────────────────────────────────────────────

    def _reload_symbols(self, *, announce: bool) -> None:
        account = self.account_box.currentText()
        current = self.symbol_box.currentText()
        try:
            paused = sorted(self._takeover.paused(account))
        except Exception as e:
            paused = []
            self._set_status(f"讀不到 [{account}] 的手動持倉暫停紀錄：{e}", "red")
            announce = False
        self.symbol_box.blockSignals(True)
        self.symbol_box.clear()
        self.symbol_box.addItems(paused)
        if current in paused:
            self.symbol_box.setCurrentText(current)
        self.symbol_box.blockSignals(False)
        if announce:
            self._set_status("" if paused else
                             f"[{account}] 目前沒有手動持倉暫停的標的，不需要接管"
                             "（暫停是在 Sync Pos 計算時發現的）。")
        self._update_buttons()

    def symbols(self) -> list[str]:
        return [self.symbol_box.itemText(i) for i in range(self.symbol_box.count())]

    def _clear_preview(self) -> None:
        self._preview = None
        self.preview.hide()

    def _account_changed(self, *_args) -> None:
        if self._sending:
            return
        self._clear_preview()
        self._reload_symbols(announce=True)

    def _symbol_changed(self, *_args) -> None:
        if self._preview is not None and not self._sending:
            self._clear_preview()
            self._set_status("標的已變更，請重新計算預覽。")
            self._update_buttons()

    def start_for(self, account: str, symbol: str) -> str:
        """從 Sync Pos 的暫停列進來：選好帳戶與標的，直接開始算預覽。
        回傳不能開始的原因（空字串 = 已開始計算）。"""
        index = self.account_box.findText(account, Qt.MatchFixedString)  # 不分大小寫
        if self._computing or self._sending:
            problem = "接管預覽還在計算或送單中，請等它結束再從 Sync Pos 進來。"
        elif self._worker_running:
            problem = "上一次接管計算的連線還沒結束，請稍後再從 Sync Pos 進來。"
        elif index < 0:
            problem = f"接管畫面沒有 {account} 這個帳戶。"
        else:
            problem = ""
        if problem:
            self._set_status(problem, "red")
            return problem
        self.account_box.setCurrentIndex(index)
        if symbol not in self.symbols():
            self.symbol_box.addItem(symbol)      # 讓 strategy_takeover 說明為何不能接管
        self.symbol_box.setCurrentText(symbol)
        self.compute()
        return ""

    # ── 計算 ─────────────────────────────────────────────────────────

    def compute(self) -> bool:
        if (self._computing or self._sending or self._worker_running or self._closed
                or not self.symbol_box.currentText()):
            return False
        account = self.account_box.currentText()
        symbol = self.symbol_box.currentText()
        self._generation += 1
        generation = self._generation
        self._computing = True
        self._worker_running = True
        self._clear_preview()
        self.progress.clear()
        self.progress.show()
        self._set_status(f"[{account}] {symbol} 計算中…（{self._takeover.target_text}）")
        self._update_buttons()
        self._log(f"[>] 交給策略接管（{self._takeover.venue}）— [{account}] {symbol}，計算中...")

        def progress(msg: str) -> None:
            self._main.call.emit(lambda: self._progress(generation, msg))

        background(self._main, lambda: self._takeover.preview(account, symbol, progress),
                   lambda preview, error: self._computed(generation, preview, error),
                   name="qt-panel-takeover-compute")
        return True

    def _progress(self, generation: int, msg: str) -> None:
        if generation == self._generation and not self._closed:
            self.progress.appendPlainText(msg.strip())
            self._log(msg)

    def _computed(self, generation: int, preview, error) -> None:
        self._worker_running = False
        if generation != self._generation or self._closed:
            self._update_buttons()
            return          # 已放棄或已重算：沒人在等這份結果，不顯示、不送
        self._computing = False
        if error:
            self._set_status(f"不能接管：{error}", "red")
            self._log(f"[X] 接管預覽失敗: {error}")
            self._update_buttons()
            return
        self._show_preview(preview)
        self._set_status(f"[{preview.account_name}] {preview.symbol} 預覽完成；"
                         "確認後只送這一檔。")
        self._update_buttons()

    def discard(self) -> None:
        if not self._computing:
            return
        self._generation += 1
        self._computing = False
        self._set_status("已放棄這次計算；結果不會顯示、不會送出（背景連線結束後可重新計算）。")
        self._log("[i] 交給策略接管的計算已放棄")
        self._update_buttons()

    # ── 預覽 ─────────────────────────────────────────────────────────

    def _show_preview(self, preview, note: str = "") -> None:
        self._preview = preview
        self.title.setText(preview_text.window_title(preview.account_name, preview.symbol))
        self.note.setText(f"⚠️ {note}" if note else "")
        self.note.setVisible(bool(note))
        clear_layout(self._lines)
        for line in preview.lines():
            self._lines.addWidget(mono(line, wrap=True))
        self.confirm_button.setText(preview_text.confirm_label(preview))
        self.confirm_button.show()
        self.cancel_button.show()
        self.preview.show()

    def cancel(self) -> None:
        if self._preview is None or self._sending:
            return
        self._clear_preview()
        self._set_status("已取消；沒有送出任何單，這檔維持暫停。")
        self._update_buttons()

    # ── 送出 ─────────────────────────────────────────────────────────

    def confirm(self) -> None:
        p = self._preview
        if p is None or self._computing or self._sending:
            return
        prompt = (preview_text.confirm_prompt(p)
                  + f"\n\n送往：{self._takeover.target_text}")
        if not self._dialogs.confirm(preview_text.CONFIRM_TITLE, prompt, warning=True):
            return
        self._preview = None                # 同一份預覽只能送一次
        self.confirm_button.hide()
        self.cancel_button.hide()
        self._sending = True
        self._update_buttons()
        self._set_status(f"[{p.account_name}] {p.symbol} 送單中…")
        self._log(preview_text.start_log_line(p))

        def progress(msg: str) -> None:
            self._main.call.emit(lambda: None if self._closed else self._log(msg))

        def work():
            import strategy_takeover
            try:
                return self._takeover.submit(p, progress)
            except strategy_takeover.TakeoverStale as stale:
                return stale             # 帶回新預覽，要使用者再確認

        background(self._main, work, lambda result, error: self._sent(p, result, error),
                   name="qt-panel-takeover-submit")

    def _sent(self, preview, result, error) -> None:
        import strategy_takeover
        self._sending = False
        self._update_buttons()
        if self._closed:
            return
        if isinstance(result, strategy_takeover.TakeoverStale):
            self._show_preview(result.fresh, note=str(result))
            self._set_status(f"⚠️ {result}；沒有送出任何單。", "red")
            self._log(f"[!] 接管 {preview.symbol}：{result}")
            self._update_buttons()
            return
        if error:
            self._set_status(f"不能接管：{error}", "red")
            self._log(f"[X] 接管失敗: {error}")
            return
        ok = result.status == "COMPLETED"
        key = "green" if ok else ("red" if result.status == "FAILED" else "text")
        self._set_status(f"{preview_text.result_title(result.status)}：{result.message}", key)
        self._log(f"[{'OK' if ok else '!!'}] 接管 {preview.symbol}: {result.message}")
        try:
            for line in self._takeover.take_notices(preview.account_name):
                self._log(f"[手動持倉·{self._takeover.venue}] [{preview.account_name}] {line}")
        except Exception as e:
            self._log(f"[!] 接管結果通知失敗: {e}")
        self._reload_symbols(announce=False)

    # ── 讀取（測試用）───────────────────────────────────────────────

    def status_text(self) -> str:
        return self.status.text()

    def progress_text(self) -> str:
        return self.progress.toPlainText()

    def note_text(self) -> str:
        return self.note.text() if self.note.isVisibleTo(self) else ""

    def preview_text(self) -> str:
        if not self.preview.isVisibleTo(self):
            return ""
        texts = [self.title.text()]
        for i in range(self._lines.count()):
            widget = self._lines.itemAt(i).widget()
            if widget is not None:
                texts.append(widget.text())
        return "\n".join(texts)
