"""新 Panel 的 Sync Pos（#152）：選帳戶 → 計算進度 → 預覽 → 確認才送。

流程和 Tk 的 `sync_preview_window` 一樣；預覽的文字與表格列共用
`sync_preview_rows`（#146），計算與送單沿用 `sync_position`。

並行模式只接 paper（`PaperSync`）：連線走送單閘門；手動歸屬紀錄與手動單紀錄用
paper 專用檔；SP500／SkewLS state 只讀不寫；不寫 trade_log.xlsx、不發 Telegram，
結果與暫停通知只寫 Output Log。計算中按「放棄」或關掉 Panel，算完的結果直接丟掉。
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QPlainTextEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import manual_attribution
import sync_preview_rows as preview_rows

from qt_panel import theme
from qt_panel.trading_common import MainThread, background
from qt_panel.widgets import Card, clear_layout, label

FIELD_WIDTH = 240


class PaperSync:
    """paper 版 Sync Pos 的計算與送單（不含畫面）。"""

    def __init__(self, *, target, send_gate, files, account_ctx_loader: Callable):
        self._target = target
        self._gate = send_gate
        self._files = files
        self._load_ctx = account_ctx_loader
        self.target_text = target.describe()

    def _connect(self, ctx):
        ib = self._gate.broker()
        try:
            ib.connect(ctx.host, ctx.port, clientId=ctx.client_id, timeout=20)
        except BaseException:
            ib.disconnect()            # 同 ib_fetcher.connect：連不上也要收掉 socket
            raise
        return ib

    def records(self):
        from sync_position import SyncRecords
        return SyncRecords(connect=self._connect, attribution_ledger=self._files.attribution,
                           manual_orders_store=self._files.orders,
                           persist_strategy_state=False)

    def compute(self, account: str, log_fn: Callable[[str], None]):
        from account_utils import ROLE_REBALANCE
        import sync_position

        ctx = self._target.sync_ctx(self._load_ctx(account, ROLE_REBALANCE))
        return sync_position.compute_sync_plan(ctx, log_fn=log_fn, records=self.records())

    def take_notices(self, plan) -> list[str]:
        """新出現的暫停通知：回傳要寫進 Output Log 的行，並在 paper 歸屬紀錄記已通知，
        同一件事只提一次（Tk 是發 Telegram 成功才記）。"""
        lines = manual_attribution.pause_notice_lines(plan.pause_notices)
        manual_attribution.mark_notified(plan.ctx.name, [n.key for n in plan.pause_notices],
                                         path=self._files.attribution)
        return lines

    def submit(self, plan, log_fn: Callable[[str], None]):
        import sync_position
        return sync_position.submit_sync_orders(plan, log_fn=log_fn)


def _mono(text: str = "", color_key: str = "text") -> QWidget:
    lbl = label(text)
    lbl.setStyleSheet(f"font-family: '{theme.MONO_FONT}'; font-size: 9pt; "
                      f"color: {theme.color(color_key)};")
    lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lbl


class SyncPosPanel(QWidget):
    def __init__(self, account_names: list[str], sync: PaperSync, *, dialogs,
                 log: Callable[[str], None]):
        super().__init__()
        self._sync = sync
        self._dialogs = dialogs
        self._log = log
        self._main = MainThread()
        self._generation = 0          # 計算的代號：放棄或重算後，舊的結果不採用
        self._computing = False
        # 背景計算還連著 paper（同一個 clientId）：放棄後也要等它結束才能重算
        self._worker_running = False
        self._sending = False
        self._closed = False
        self._plan = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        card = Card("Sync Pos · 把帳戶拉到策略目標（LMT）")
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(label("Account"))
        self.account_box = QComboBox()
        self.account_box.addItems(account_names)
        self.account_box.setFixedWidth(FIELD_WIDTH)
        self.account_box.currentTextChanged.connect(self._account_changed)
        row.addWidget(self.account_box)
        self.compute_button = QPushButton("計算")
        self.compute_button.setObjectName("Primary")
        self.compute_button.clicked.connect(self.compute)
        self.discard_button = QPushButton("放棄")
        self.discard_button.setEnabled(False)
        self.discard_button.clicked.connect(self.discard)
        row.addWidget(self.compute_button)
        row.addWidget(self.discard_button)
        row.addStretch(1)
        card.body.addLayout(row)
        helper = label("計算：讀 paper 帳戶的權益與持倉，依該帳戶的策略設定算理論持倉與差額；"
                       "確認後以 LMT（ask×1.003／bid×0.997、outside RTH）送出。"
                       "手動持倉暫停中的標的不送。", name="Faint")
        helper.setWordWrap(True)
        card.body.addWidget(helper)
        self.status = label(name="Dim")
        self.status.setWordWrap(True)
        card.body.addWidget(self.status)
        self.progress = QPlainTextEdit()
        self.progress.setObjectName("Log")
        self.progress.setReadOnly(True)
        self.progress.setFixedHeight(120)
        self.progress.hide()
        card.body.addWidget(self.progress)
        lay.addWidget(card)

        self.preview = Card("預覽")
        self.subtitle = label(name="Dim")
        self.preview.body.addWidget(self.subtitle)
        self._sections = QVBoxLayout()
        self._sections.setSpacing(6)
        self.preview.body.addLayout(self._sections)
        self.trades_heading = label(name="SectionTitle")
        self.preview.body.addWidget(self.trades_heading)
        self.trades = QTableWidget(0, len(preview_rows.TRADE_COLUMNS))
        self.trades.setHorizontalHeaderLabels(preview_rows.TRADE_COLUMNS)
        self.trades.verticalHeader().setVisible(False)
        self.trades.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.trades.setSelectionMode(QAbstractItemView.NoSelection)
        self.trades.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.preview.body.addWidget(self.trades)
        self.totals = label()
        self.totals.setStyleSheet("font-weight: 600;")
        self.preview.body.addWidget(self.totals)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.confirm_button = QPushButton("確認送出（LMT）")
        self.confirm_button.setObjectName("Primary")
        self.confirm_button.clicked.connect(self.confirm)
        buttons.addWidget(self.confirm_button)
        self.preview.body.addLayout(buttons)
        self.preview.hide()
        lay.addWidget(self.preview)
        lay.addStretch(1)

    # ── 生命週期 ─────────────────────────────────────────────────────

    def stop(self) -> None:
        self._closed = True
        self._generation += 1

    def computing(self) -> bool:
        return self._computing

    def sending(self) -> bool:
        return self._sending

    def _set_status(self, text: str, key: str = "text_dim") -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {theme.color(key)};")

    def _update_buttons(self) -> None:
        idle = not self._computing and not self._sending
        self.compute_button.setEnabled(idle and not self._worker_running)
        self.account_box.setEnabled(idle)
        self.discard_button.setEnabled(self._computing)
        self.confirm_button.setEnabled(idle and self._plan is not None)

    # ── 計算 ─────────────────────────────────────────────────────────

    def compute(self) -> None:
        if self._computing or self._sending or self._worker_running or self._closed:
            return
        account = self.account_box.currentText()
        self._generation += 1
        generation = self._generation
        self._computing = True
        self._worker_running = True
        self._plan = None
        self.preview.hide()
        self.progress.clear()
        self.progress.show()
        self._set_status(f"[{account}] 計算中…（{self._sync.target_text}）")
        self._update_buttons()
        self._log(f"[>] Sync Position（paper）— computing targets for [{account}]...")

        def progress(msg: str) -> None:
            self._main.call.emit(lambda: self._progress(generation, msg))

        background(self._main, lambda: self._sync.compute(account, progress),
                   lambda plan, error: self._computed(generation, plan, error),
                   name="qt-panel-sync-compute")

    def _progress(self, generation: int, msg: str) -> None:
        if generation == self._generation and not self._closed:
            self.progress.appendPlainText(msg.strip())
            self._log(msg)

    def _computed(self, generation: int, plan, error) -> None:
        self._worker_running = False
        if generation != self._generation or self._closed:
            self._update_buttons()
            return          # 已放棄或已重算：沒人在等這份結果，不顯示、不送
        self._computing = False
        if error:
            self._set_status(f"計算失敗：{error}", "red")
            self._log(f"[X] Sync compute failed: {error}")
            self._update_buttons()
            return
        self._plan = plan if plan.orders else None
        for line in preview_rows.log_summary_lines(plan):
            self._log(line)
        try:
            for line in self._sync.take_notices(plan):
                self._log(f"[手動持倉·paper] [{plan.ctx.name}] {line}")
        except Exception as e:
            self._log(f"[!] 手動持倉暫停通知失敗: {e}")
        self._show_plan(plan)
        self._set_status(f"[{plan.ctx.name}] 計算完成，請核對後再送出。")
        self._update_buttons()

    def _account_changed(self, *_args) -> None:
        if self.preview.isVisibleTo(self) and not self._sending:
            self._plan = None
            self.preview.hide()
            self._set_status("帳戶已變更，請重新計算。")
            self._update_buttons()

    def discard(self) -> None:
        if not self._computing:
            return
        self._generation += 1
        self._computing = False
        self._set_status("已放棄這次計算；結果不會顯示、不會送出（背景連線結束後可重新計算）。")
        self._log("[i] Sync Pos 計算已放棄")
        self._update_buttons()

    # ── 預覽 ─────────────────────────────────────────────────────────

    def _show_plan(self, plan) -> None:
        self.subtitle.setText(preview_rows.plan_subtitle(plan))
        clear_layout(self._sections)

        def section(title: str, key: str, lines: list[tuple[str, str]]) -> None:
            head = label(title)
            head.setStyleSheet(f"color: {theme.color(key)}; font-weight: 600;")
            self._sections.addWidget(head)
            for text, line_key in lines:
                self._sections.addWidget(_mono(text, line_key))

        for sec in preview_rows.target_sections(plan):
            section(sec.title, sec.color, [(line, "text") for line in sec.lines])
        holdings = preview_rows.current_holding_rows(plan)
        if holdings:
            section("Current Holdings（實際持倉）", "text",
                    [(f"{h.text}  [{h.tag}]{h.mark}", h.mark_color if h.mark else "text")
                     for h in holdings])
        others = preview_rows.other_holding_lines(plan)
        if others:
            section("Other（不在 Sync 範圍）", "text_dim", [(line, "text_dim") for line in others])
        paused = preview_rows.paused_rows(plan)
        if paused:
            lines = []
            for p in paused:
                lines += [(p.text, "text"), (p.reason, "text_dim")]
            section("手動持倉暫停（這些標的的策略單不會送出）", "yellow", lines)

        self.trades_heading.setText(preview_rows.trades_heading(plan))
        self.trades.setRowCount(0)
        for trade in preview_rows.trade_rows(plan):
            r = self.trades.rowCount()
            self.trades.insertRow(r)
            for c, value in enumerate(trade.values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignCenter)
                if value in ("BUY", "SELL"):
                    item.setForeground(QColor(theme.color(trade.color)))
                self.trades.setItem(r, c, item)
        self.trades.setFixedHeight(
            self.trades.horizontalHeader().height() + 4
            + sum(self.trades.rowHeight(r) for r in range(self.trades.rowCount())))
        if plan.orders:
            self.totals.setText(preview_rows.trade_totals_text(plan))
            self.trades.show()
            self.confirm_button.show()
        else:
            self.totals.setText("Already on target — no trades needed")
            self.trades.hide()
            self.confirm_button.hide()
            self._log("[OK] Already on target, no trades needed")
        self.preview.show()

    # ── 送出 ─────────────────────────────────────────────────────────

    def confirm(self) -> None:
        plan = self._plan
        if plan is None or self._computing or self._sending:
            return
        prompt = (preview_rows.confirm_prompt(plan)
                  + f"\n\n送往：{self._sync.target_text}")
        if not self._dialogs.confirm("確認送出 Sync Pos", prompt, warning=True):
            return
        self._plan = None                   # 同一份預覽只能送一次
        self.confirm_button.hide()
        self._sending = True
        self._update_buttons()
        self._set_status(f"[{plan.ctx.name}] 送單中…")
        self._log(preview_rows.sending_log_line(plan))

        def progress(msg: str) -> None:
            self._main.call.emit(lambda: None if self._closed else self._log(msg))

        background(self._main, lambda: self._sync.submit(plan, progress),
                   lambda result, error: self._sent(plan, result, error),
                   name="qt-panel-sync-submit")

    def _sent(self, plan, result, error) -> None:
        self._sending = False
        self._update_buttons()
        if self._closed:
            return
        if error:
            self._set_status(f"送單失敗：{error}", "red")
            self._log(f"[X] Sync execute failed: {error}")
            return
        placed, fills = result
        self._log(f"[OK] Placed {len(placed)} orders")
        for f in fills:
            icon = "OK" if f["status"] == "Filled" else "!!"
            self._log(f"  [{icon}] {f['action']} {f['filled']}/{f['qty']}sh "
                      f"{f['symbol']} @ ${f['avg_price']:.2f} [{f['status']}]")
        self._set_status(f"[{plan.ctx.name}] 已送出 {len(placed)} 筆限價單（paper），"
                         "成交請看 Output Log 與 IB。", "green")

    # ── 讀取（測試用）───────────────────────────────────────────────

    def status_text(self) -> str:
        return self.status.text()

    def subtitle_text(self) -> str:
        return self.subtitle.text()

    def preview_text(self) -> str:
        texts = [self.subtitle.text()]
        for i in range(self._sections.count()):
            widget = self._sections.itemAt(i).widget()
            if widget is not None:
                texts.append(widget.text())
        texts += [self.trades_heading.text(), self.totals.text()]
        return "\n".join(texts)

    def trade_table(self) -> list[tuple[str, ...]]:
        if not self.preview.isVisibleTo(self) or not self.trades.isVisibleTo(self):
            return []
        return [tuple(self.trades.item(r, c).text() for c in range(self.trades.columnCount()))
                for r in range(self.trades.rowCount())]

