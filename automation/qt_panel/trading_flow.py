"""新 Panel Trading 的操作流程（#188）：不含 Qt，畫面只負責顯示與對話框。

- `ManualOrderFlow`：手動單的「預覽 → 再次核對 → 確認 → 送出」、撤單、
  「修改 = 先撤再送」（等 IB 確認原單取消才提供新委託預覽）與狀態刷新。
  帳戶、合約、送單的判斷全在 `manual_orders.ManualOrderService`，欄位解析與
  文字在 `manual_order_form`；這裡只管流程與「畫面現在該顯示什麼」（FlowView）。
- `CancellableJob`：Sync Pos 與交給策略接管共用的可取消背景計算 — 一次只跑一個；
  放棄、重算或關閉後，舊結果與進度一律不採用；背景還連著 IB（同一個 clientId）
  時不能重開。

背景執行是注入的 runner（seam）：`run(work, done)` 在背景跑 work、結束後在
主執行緒呼叫 `done(結果, 錯誤訊息或 None)`；`post(fn)` 把背景執行緒的呼叫排回
主執行緒。Qt 版是 `trading_common.QtRunner`，測試用同步或手動控制完成時機的版本。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Optional

import manual_order_form as order_form

Done = Callable[[object, Optional[str]], None]

# IB 取消結果還沒確定：委託仍保持未結案，等下一次刷新
_CANCEL_UNSETTLED = {"CANCEL_PENDING", "CANCEL_UNKNOWN", "SUBMISSION_UNKNOWN"}


@dataclass(frozen=True)
class Status:
    """一行狀態文字與顏色（theme.color 的 key）。"""
    text: str = ""
    key: str = "text_dim"


@dataclass(frozen=True)
class FlowView:
    """手動單畫面現在該顯示什麼。"""
    busy: bool = False                 # 有單在跑：表單、預覽、撤單、修改都暫停
    refreshing: bool = False           # 正在向 IB 刷新委託狀態
    can_confirm: bool = False          # 有有效預覽且不忙
    form_status: Status = Status()
    preview: Status = Status("填妥欄位後按「預覽委託」。", "text")
    order_status: Status = Status()
    orders: tuple = ()                 # 未結案委託列（manual_order_form.open_orders）


class ManualOrderFlow:
    """手動單流程。dialogs 需要 `confirm(title, text, warning=False) -> bool` 與
    `show_message(title, text, error=False)`；read_form／load_form 讀寫畫面上的表單；
    on_change(FlowView) 在畫面該更新時呼叫。"""

    def __init__(self, service, *, runner, dialogs,
                 read_form: Callable[[], order_form.ManualOrderForm],
                 load_form: Callable[[order_form.ManualOrderForm], None],
                 log: Callable[[str], None], on_change: Callable[[FlowView], None],
                 target_text: str = ""):
        self.service = service
        self._runner = runner
        self._dialogs = dialogs
        self._read_form = read_form
        self._load_form = load_form
        self._log = log
        self._on_change = on_change
        self._target_text = target_text
        self._preview = None
        self._preview_request = None
        self._closed = False
        self._rows: dict[str, dict] = {}
        self._replacement_order_ref = None
        self._replacement_request = None
        self.view = FlowView()

    # ── 狀態 ─────────────────────────────────────────────────────────

    def _update(self, **changes) -> None:
        view = replace(self.view, **changes)
        view = replace(view, can_confirm=not view.busy and self._preview is not None)
        self.view = view
        self._on_change(view)

    def _run(self, work: Callable[[], object], done: Done) -> None:
        self._runner.run(work, lambda result, error: None if self._closed
                         else done(result, error))

    def row(self, order_ref: str | None) -> dict | None:
        return self._rows.get(order_ref) if order_ref else None

    def can_cancel(self, row: dict | None) -> bool:
        return not self.view.busy and order_form.can_cancel(row)

    def can_modify(self, row: dict | None) -> bool:
        return not self.view.busy and order_form.can_modify(row)

    # ── 生命週期 ─────────────────────────────────────────────────────

    def start(self) -> None:
        self._closed = False
        self._render_orders(self._active_orders())
        if self.has_work():
            self.refresh()

    def stop(self) -> None:
        self._closed = True

    def has_work(self) -> bool:
        return self.service.has_active_orders() or self.service.has_pending_notifications()

    def poll(self) -> None:
        if not self._closed and not self.view.refreshing and self.has_work():
            self.refresh()

    # ── 表單 ─────────────────────────────────────────────────────────

    def form_changed(self) -> None:
        if self._preview is not None:
            self._clear_preview()
            self._update(preview=Status("欄位已變更，請重新預覽委託。", "text"))

    def _clear_preview(self) -> None:
        self._preview = None
        self._preview_request = None

    # ── 預覽 → 再次核對 → 確認 → 送出 ─────────────────────────────────

    def preview(self) -> None:
        if self.view.busy:
            return
        try:
            request = order_form.parse_request(self._read_form())
        except ValueError as exc:
            self._dialogs.show_message("手動下單", str(exc), error=True)
            return
        self._update(busy=True, form_status=Status("正在檢查 Account、Universe 與 IB 合約…"))
        service = self.service

        def work():
            return service.preview(request.account_name, request.symbol,
                                   request.quantity, request.limit_price,
                                   **order_form.preview_kwargs(request))

        self._run(work, lambda result, error: self._preview_done(request, result, error))

    def _preview_done(self, request, preview, error) -> None:
        if error:
            self._clear_preview()
            self._update(busy=False, form_status=Status("無法預覽"),
                         preview=Status(error, "red"))
            return
        self._preview = preview
        self._preview_request = request
        self._update(busy=False,
                     form_status=Status(f"IB 合約已確認：{preview.primary_exchange}"),
                     preview=Status(preview.confirmation_text,
                                    "yellow" if preview.price_warning else "text"))

    def confirm(self) -> None:
        if self.view.busy or self._preview is None:
            return
        try:
            current = order_form.parse_request(self._read_form())
        except ValueError as exc:
            self._dialogs.show_message("手動下單", str(exc), error=True)
            return
        if current != self._preview_request:
            self.form_changed()
            return
        preview = self._preview
        self._update(busy=True, form_status=Status("正在重新檢查持倉與委託…"))
        self._run(lambda: self.service.validate_preview(preview),
                  lambda fresh, error: self._validated(preview, current, fresh, error))

    def _validated(self, preview, request, fresh, error) -> None:
        if error or fresh != preview:
            self._clear_preview()
            self._update(busy=False, form_status=Status("預覽已失效，請重新預覽"),
                         preview=Status(error or "資料在預覽後變更，請重新預覽", "red"))
            return
        self._update(busy=False)
        try:
            unchanged = (self._preview == preview
                         and order_form.parse_request(self._read_form()) == request)
        except ValueError:
            unchanged = False
        if not unchanged:
            self.form_changed()
            return
        prompt = fresh.confirmation_text
        if self._target_text:
            prompt += f"\n\n送往：{self._target_text}"
        if not self._dialogs.confirm("再次核對委託", prompt + "\n\n確認送出這筆委託？",
                                     warning=bool(fresh.price_warning)):
            return
        self._update(busy=True, form_status=Status("送單中…"))
        self._run(lambda: self.service.submit(preview), self._submitted)

    def _submitted(self, result, error) -> None:
        self._clear_preview()
        if error:
            self._update(busy=False, form_status=Status("送單前重新檢查未通過"),
                         preview=Status(error, "red"))
            return
        self._update(busy=False, form_status=Status(
            order_form.submit_status_text(result),
            "red" if order_form.needs_attention(result) else "green"))
        self._log(order_form.submit_log_line(result))
        self._render_orders(self._active_orders())
        self.refresh()

    # ── 撤單與修改 ───────────────────────────────────────────────────

    def cancel(self, row: dict | None) -> None:
        if row is None or self.view.busy:
            return
        if not self._dialogs.confirm("取消手動委託", order_form.cancel_prompt(row)):
            return
        self._request_cancel(row["order_ref"], None)

    def modify(self, row: dict | None) -> None:
        if row is None or self.view.busy or row.get("order_type", "LMT") == "MKT":
            return
        if not order_form.form_matches_row(self._read_form(), row):
            self._load_form(order_form.form_from_row(row))
            self._update(form_status=Status("已載入選定委託；調整價格或股數後再次按修改。"),
                         preview=Status("修改流程會先等待 IB 確認取消，再提供新委託預覽。", "text"))
            return
        try:
            request = order_form.parse_request(self._read_form())
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
        row = self._rows.get(order_ref)
        orders = self.view.orders
        if row is not None:
            pending = order_form.cancel_pending_row(row)
            self._rows[order_ref] = pending
            orders = tuple(pending if r.get("order_ref") == order_ref else r for r in orders)
        self._update(busy=True, orders=orders,
                     order_status=Status("已記錄取消要求，等待 IB 確認…"))
        self._run(lambda: self.service.cancel(order_ref),
                  lambda result, error: self._cancelled(order_ref, replacement_request,
                                                        result, error))

    def _cancelled(self, order_ref, replacement_request, result, error) -> None:
        if error:
            self._update(busy=False, order_status=Status(f"取消結果待確認：{error}", "red"))
            self._render_orders(self._active_orders())
            return
        self._update(busy=False)
        status = result["status"]
        if replacement_request is not None:
            self._replacement_order_ref = order_ref
            self._replacement_request = replacement_request
        if status in _CANCEL_UNSETTLED:
            self._update(order_status=Status(
                result.get("status_message") or "取消結果待確認；委託仍保持未結案", "red"))
            self.refresh()
        else:
            self._update(order_status=Status(result.get("status_label", status)))
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
            self._update(preview=Status(replacement.message, "text"))
            return
        self._load_form(replacement.form)
        self._clear_preview()
        self._update(form_status=Status("IB 已確認取消；請預覽並再次確認新委託。"),
                     preview=Status(replacement.message, "text"))

    # ── 委託狀態 ─────────────────────────────────────────────────────

    def _active_orders(self) -> list[dict]:
        return self.service.list_orders(active_only=True)

    def refresh(self) -> None:
        if self._closed or self.view.refreshing:
            return
        self._update(refreshing=True, order_status=Status("正在向 IB 查詢手動委託狀態…"))
        self._run(self.service.refresh_status, self._refreshed)

    def _refreshed(self, result, error) -> None:
        self._update(refreshing=False)
        if error:
            self._update(order_status=Status(f"IB 狀態查詢失敗：{error}", "red"))
            self._render_orders(self._active_orders())
            return
        self._render_orders(result["orders"])
        if self._replacement_order_ref is not None:
            previous = next((row for row in self.service.list_orders()
                             if row.get("order_ref") == self._replacement_order_ref), None)
            if previous and not order_form.replacement_waiting(previous):
                self._finish_replacement_if_ready(previous)
        errors = result.get("errors", [])
        self._update(order_status=Status("；".join(errors) if errors else "狀態已刷新",
                                         "red" if errors else "text_dim"))

    def _render_orders(self, rows) -> None:
        orders = tuple(order_form.open_orders(rows))
        self._rows = {row["order_ref"]: row for row in orders}
        self._update(orders=orders)


class CancellableJob:
    """一次只跑一個的可取消背景計算（Sync Pos、交給策略接管共用）。

    start(work, on_progress, on_done)：work(progress) 在背景跑，progress(msg) 可在
    背景執行緒呼叫；on_progress／on_done 只在這一次計算仍有人在等時才在主執行緒
    呼叫。discard()：放棄這次計算（結果不採用）；背景連線結束前不能重開。close()：
    畫面關閉，之後的結果與進度都不採用。
    """

    def __init__(self, runner):
        self._runner = runner
        self._generation = 0
        self._closed = False
        self.computing = False          # 有人在等結果
        self.worker_running = False     # 背景還在跑（還連著 IB）

    @property
    def can_start(self) -> bool:
        return not self.computing and not self.worker_running and not self._closed

    def _current(self, generation: int) -> bool:
        return generation == self._generation and not self._closed

    def start(self, work: Callable[[Callable[[str], None]], object],
              on_progress: Callable[[str], None],
              on_done: Done, *, on_settled: Callable[[], None] = lambda: None) -> bool:
        """開始計算；正在算、背景還沒結束或已關閉時回傳 False。
        on_settled：背景結束但結果已沒人在等（放棄、重算、關閉）時呼叫，讓畫面
        重新開放「計算」。"""
        if not self.can_start:
            return False
        self._generation += 1
        generation = self._generation
        self.computing = True
        self.worker_running = True

        def progress(msg: str) -> None:
            self._runner.post(lambda: on_progress(msg) if self._current(generation) else None)

        def done(result, error) -> None:
            self.worker_running = False
            if not self._current(generation):
                on_settled()
                return          # 已放棄、重算或關閉：沒人在等這份結果，不顯示、不送
            self.computing = False
            on_done(result, error)

        self._runner.run(lambda: work(progress), done)
        return True

    def discard(self) -> bool:
        """放棄正在進行的計算；沒有在算時回傳 False。"""
        if not self.computing:
            return False
        self._generation += 1
        self.computing = False
        return True

    def close(self) -> None:
        self._closed = True
        self._generation += 1
