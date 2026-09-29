"""Panel window for previewing, confirming, and tracking manual orders."""

from __future__ import annotations

import math
import threading
import tkinter as tk
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from tkinter import messagebox, ttk

from ime_guard import ascii_entry


@dataclass(frozen=True)
class ManualOrderRequest:
    account_name: str
    symbol: str
    quantity: int | None
    limit_price: float | None
    action: str = "BUY"
    allow_short: bool = False
    amount_usd: Decimal | None = None
    order_type: str = "LMT"
    session: str = "REGULAR"
    equity_pct: Decimal | None = None


class ManualOrderWindow:
    """Panel adapter; all account, contract, and order decisions live in service."""

    POLL_INTERVAL_MS = 10_000
    SHARES_MODE = "整股股數"
    CASH_MODE = "美元金額"
    PERCENT_MODE = "帳戶權益 %"
    PERCENT_CHOICES = ("25", "30", "50", "75", "100")
    ACTION_LABELS = {"買進": "BUY", "賣出": "SELL", "回補空單": "COVER"}
    STORED_ACTION_LABELS = {
        "BUY": "買進", "SELL": "賣出", "SELL_SHORT": "放空", "COVER": "回補空單",
    }
    FORM_ACTION_LABELS = {**STORED_ACTION_LABELS, "SELL_SHORT": "賣出"}
    SESSION_CHOICES = {
        "一般時段": "REGULAR", "盤前": "PRE_MARKET", "盤後": "AFTER_HOURS",
        "隔夜（IBKR Overnight）": "OVERNIGHT",
    }
    FORM_SESSION_LABELS = {code: label for label, code in SESSION_CHOICES.items()}
    STORED_SESSION_LABELS = {
        "REGULAR": "一般時段 RTH / DAY",
        "PRE_MARKET": "盤前 outsideRTH / DAY",
        "AFTER_HOURS": "盤後 outsideRTH / DAY",
        "OVERNIGHT": "隔夜 OVERNIGHT",
    }

    def __init__(self, parent, account_names, service, theme, fonts, log=None):
        self.parent = parent
        self.service = service
        self.theme = theme
        self.fonts = fonts
        self.log = log or (lambda _message: None)
        self._preview = None
        self._preview_request = None
        self._busy = False
        self._refreshing = False
        self._closed = False
        self._poll_id = None
        self._order_rows = {}
        self._replacement_order_ref = None
        self._replacement_request = None

        self.win = tk.Toplevel(parent)
        self.win.title("手動下單 — 整股限價委託")
        self.win.configure(bg=theme["bg"])
        self.win.geometry("1030x640")
        self.win.minsize(960, 560)
        self.win.transient(parent)
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        self.account_var = tk.StringVar(self.win, value="")
        self.symbol_var = tk.StringVar(self.win, value="")
        self.quantity_var = tk.StringVar(self.win, value="")
        self.amount_var = tk.StringVar(self.win, value="")
        self.percent_var = tk.StringVar(self.win, value="")
        self.input_mode_var = tk.StringVar(self.win, value=self.SHARES_MODE)
        self.price_var = tk.StringVar(self.win, value="")
        self.order_type_var = tk.StringVar(self.win, value="LMT")
        self.session_var = tk.StringVar(self.win, value="一般時段")
        self.action_var = tk.StringVar(self.win, value="買進")
        self.allow_short_var = tk.BooleanVar(self.win, value=False)
        for variable in (
            self.account_var, self.symbol_var,
            self.quantity_var, self.amount_var, self.percent_var,
            self.input_mode_var,
            self.price_var,
            self.order_type_var, self.session_var,
            self.action_var, self.allow_short_var,
        ):
            variable.trace_add("write", self._form_changed)
        self.action_var.trace_add("write", self._action_changed)
        self.input_mode_var.trace_add("write", self._input_mode_changed)
        self.order_type_var.trace_add("write", self._order_type_changed)

        self._build(account_names)
        self._render_orders(self._active_orders())
        if self.service.has_active_orders():
            self._refresh_status()
        self._schedule_poll()

    def _build(self, account_names):
        t = self.theme
        header = tk.Frame(self.win, bg=t["accent"], padx=16, pady=9)
        header.pack(fill="x")
        tk.Label(
            header, text="手動下單", font=self.fonts["section"],
            bg=t["accent"], fg=t["accent_hl"],
        ).pack(side="left")
        tk.Label(
            header, text="一般時段 LMT / MKT · 盤前／盤後／隔夜 LMT · 整股",
            font=self.fonts["small"], bg=t["accent"], fg=t["text"],
        ).pack(side="right")

        form = tk.Frame(self.win, bg=t["bg2"], padx=16, pady=12)
        form.pack(fill="x", padx=12, pady=(12, 8))
        for column, weight in ((1, 1), (3, 1)):
            form.columnconfigure(column, weight=weight)

        self._field_label(form, "Account", 0, 0)
        self.account_box = ttk.Combobox(
            form, textvariable=self.account_var,
            values=tuple(account_names), state="readonly", width=18,
        )
        self.account_box.grid(row=0, column=1, sticky="ew", padx=(0, 14), pady=5)

        self._field_label(form, "美股代碼", 0, 2)
        self.symbol_entry = ttk.Entry(form, textvariable=self.symbol_var, width=18)
        self.symbol_entry.grid(row=0, column=3, sticky="ew", pady=5)

        self._field_label(form, self.SHARES_MODE, 1, 0)
        self.quantity_entry = ttk.Entry(
            form, textvariable=self.quantity_var, width=18,
        )
        self.quantity_entry.grid(row=1, column=1, sticky="ew", padx=(0, 14), pady=5)

        self._field_label(form, "自填限價（USD）", 1, 2)
        self.price_entry = ttk.Entry(form, textvariable=self.price_var, width=18)
        self.price_entry.grid(row=1, column=3, sticky="ew", pady=5)

        self._field_label(form, "輸入方式", 2, 0)
        self.input_mode_box = ttk.Combobox(
            form, textvariable=self.input_mode_var,
            values=(self.SHARES_MODE, self.CASH_MODE, self.PERCENT_MODE),
            state="readonly", width=18,
        )
        self.input_mode_box.grid(row=2, column=1, sticky="ew", padx=(0, 14), pady=5)

        self._field_label(form, "美元金額（預估交易目標）", 2, 2)
        self.amount_entry = ttk.Entry(
            form, textvariable=self.amount_var, width=18, state="disabled",
        )
        self.amount_entry.grid(row=2, column=3, sticky="ew", pady=5)

        self._field_label(form, "權益百分比（IB 淨清算值 × %）", 3, 2)
        # 可自行輸入其他百分比；停用時 Combobox 不能選也不能打字。
        self.percent_box = ttk.Combobox(
            form, textvariable=self.percent_var,
            values=self.PERCENT_CHOICES, state="disabled", width=18,
        )
        self.percent_box.grid(row=3, column=3, sticky="ew", pady=5)
        # 中文輸入法會把 "." 吃進組字，184.4 變 1844（#138）
        for entry, variable in (
            (self.symbol_entry, self.symbol_var),
            (self.quantity_entry, self.quantity_var),
            (self.price_entry, self.price_var),
            (self.amount_entry, self.amount_var),
            (self.percent_box, self.percent_var),
        ):
            ascii_entry(entry, variable)

        self._field_label(form, "委託類型", 4, 2)
        self.order_type_box = ttk.Combobox(
            form, textvariable=self.order_type_var,
            values=("LMT", "MKT"), state="readonly", width=18,
        )
        self.order_type_box.grid(row=4, column=3, sticky="ew", pady=5)

        self._field_label(form, "方向", 4, 0)
        self.action_box = ttk.Combobox(
            form, textvariable=self.action_var,
            values=tuple(self.ACTION_LABELS), state="readonly", width=18,
        )
        self.action_box.grid(row=4, column=1, sticky="ew", padx=(0, 14), pady=5)

        self._field_label(form, "交易時段", 5, 0)
        self.session_box = ttk.Combobox(
            form, textvariable=self.session_var,
            values=tuple(self.SESSION_CHOICES), state="readonly", width=18,
        )
        self.session_box.grid(row=5, column=1, sticky="ew", padx=(0, 14), pady=5)
        self.allow_short_check = ttk.Checkbutton(
            form, text="允許放空", variable=self.allow_short_var,
            state="disabled",
        )
        self.allow_short_check.grid(row=5, column=2, columnspan=2, sticky="w", pady=5)

        helper = tk.Label(
            form,
            text=("只接受美國市場的美元個股或 ETF；策略 Universe 標的會被阻擋。"
                  "盤前／盤後／隔夜只收 LMT，且只在該時段開放時送出；"
                  "隔夜單送往 IBKR Overnight 市場。"),
            font=self.fonts["small"], bg=t["bg2"], fg=t["text_dim"],
            anchor="w",
        )
        helper.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(7, 2))

        actions = tk.Frame(form, bg=t["bg2"])
        actions.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(9, 0))
        self.preview_button = ttk.Button(
            actions, text="預覽委託", command=self._preview_order,
        )
        self.preview_button.pack(side="left")
        self.confirm_button = ttk.Button(
            actions, text="再次核對並送出", command=self._confirm_order,
            state="disabled",
        )
        self.confirm_button.pack(side="left", padx=(8, 0))
        self.form_status = tk.Label(
            actions, text="", font=self.fonts["small"],
            bg=t["bg2"], fg=t["text_dim"], anchor="w",
        )
        self.form_status.pack(side="left", fill="x", expand=True, padx=(12, 0))

        preview_frame = tk.Frame(
            self.win, bg=t["bg2"], padx=16, pady=10,
        )
        preview_frame.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(
            preview_frame, text="確認內容", font=self.fonts["badge"],
            bg=t["bg2"], fg=t["accent_hl"],
        ).pack(anchor="w")
        self.preview_text = tk.Label(
            preview_frame, text="填妥欄位後按「預覽委託」。",
            font=self.fonts["label"], bg=t["bg2"], fg=t["text"],
            justify="left", anchor="w",
        )
        self.preview_text.pack(fill="x", pady=(5, 0))

        orders_frame = tk.Frame(self.win, bg=t["bg"], padx=12, pady=2)
        orders_frame.pack(fill="both", expand=True)
        order_header = tk.Frame(orders_frame, bg=t["bg"])
        order_header.pack(fill="x", pady=(3, 5))
        tk.Label(
            order_header, text="手動委託狀態", font=self.fonts["badge"],
            bg=t["bg"], fg=t["accent_hl"],
        ).pack(side="left")
        self.refresh_button = ttk.Button(
            order_header, text="刷新 IB 狀態", command=self._refresh_status,
        )
        self.refresh_button.pack(side="right")

        self.modify_button = ttk.Button(
            order_header, text="修改條件", command=self._modify_selected,
            state="disabled",
        )
        self.modify_button.pack(side="right", padx=(0, 8))
        self.cancel_button = ttk.Button(
            order_header, text="取消選定委託", command=self._cancel_selected,
            state="disabled",
        )
        self.cancel_button.pack(side="right", padx=(0, 8))

        columns = (
            "account", "symbol", "action", "quantity", "price_condition",
            "session", "status", "filled",
        )
        self.orders = ttk.Treeview(
            orders_frame, columns=columns, show="headings", height=8,
        )
        headings = {
            "account": ("Account", 100),
            "symbol": ("標的", 80),
            "action": ("方向", 55),
            "quantity": ("數量", 65),
            "price_condition": ("價格條件", 105),
            "session": ("時段", 95),
            "status": ("IB 狀態", 135),
            "filled": ("成交", 120),
        }
        for column, (title, width) in headings.items():
            self.orders.heading(column, text=title)
            self.orders.column(column, width=width, minwidth=55, anchor="center")
        self.orders.bind("<<TreeviewSelect>>", self._order_selected)
        self.orders.pack(fill="both", expand=True)

        self.order_status = tk.Label(
            orders_frame, text="", font=self.fonts["small"],
            bg=t["bg"], fg=t["text_dim"], anchor="w",
        )
        self.order_status.pack(fill="x", pady=(4, 3))

    def _field_label(self, parent, text, row, column):
        tk.Label(
            parent, text=text, font=self.fonts["label"],
            bg=self.theme["bg2"], fg=self.theme["text"],
        ).grid(row=row, column=column, sticky="w", padx=(0, 8), pady=5)

    def _form_changed(self, *_args):
        if self._preview is not None:
            self._preview = None
            self._preview_request = None
            self.confirm_button.configure(state="disabled")
            self.preview_text.configure(
                text="欄位已變更，請重新預覽委託。",
            )

    def _action_changed(self, *_args):
        is_sell = self.action_var.get() == "賣出"
        if not is_sell:
            self.allow_short_var.set(False)
        self.allow_short_check.configure(
            state="normal" if is_sell and not self._busy else "disabled",
        )

    def _input_mode_changed(self, *_args):
        mode = self.input_mode_var.get()
        for mode_value, variable in (
            (self.SHARES_MODE, self.quantity_var),
            (self.CASH_MODE, self.amount_var),
            (self.PERCENT_MODE, self.percent_var),
        ):
            if mode != mode_value:
                variable.set("")
        self._apply_input_mode_state()

    def _apply_input_mode_state(self):
        mode = self.input_mode_var.get()
        for widget, mode_value in (
            (self.quantity_entry, self.SHARES_MODE),
            (self.amount_entry, self.CASH_MODE),
            (self.percent_box, self.PERCENT_MODE),
        ):
            widget.configure(
                state="normal" if mode == mode_value and not self._busy else "disabled",
            )

    def _order_type_changed(self, *_args):
        if self.order_type_var.get() == "MKT":
            self.price_var.set("")
        self.price_entry.configure(
            state="normal" if self.order_type_var.get() == "LMT" and not self._busy
            else "disabled",
        )

    def _read_request(self):
        account = self.account_var.get().strip()
        if not account:
            raise ValueError("請先選擇這筆委託要送往的 Account。")
        quantity = None
        amount = None
        percent = None
        if self.input_mode_var.get() == self.SHARES_MODE:
            raw_quantity = self.quantity_var.get().strip()
            if not raw_quantity.isdecimal():
                raise ValueError("整股股數必須是正整數。")
            quantity = int(raw_quantity)
            if quantity <= 0:
                raise ValueError("整股股數必須大於零。")
        elif self.input_mode_var.get() == self.CASH_MODE:
            try:
                amount = Decimal(self.amount_var.get().strip())
            except InvalidOperation as exc:
                raise ValueError("請輸入有效的美元金額。") from exc
            if not amount.is_finite() or amount <= 0:
                raise ValueError("美元金額必須大於零。")
        elif self.input_mode_var.get() == self.PERCENT_MODE:
            from manual_orders import equity_percent

            percent = equity_percent(self.percent_var.get())
        else:
            raise ValueError("請選擇整股股數、美元金額或帳戶權益 %。")
        order_type = self.order_type_var.get()
        if order_type == "LMT":
            try:
                price = float(self.price_var.get().strip())
            except ValueError as exc:
                raise ValueError("請輸入自填限價。") from exc
            if not math.isfinite(price) or price <= 0:
                raise ValueError("限價必須大於零。")
        elif order_type == "MKT":
            price = None
        else:
            raise ValueError("請選擇有效委託類型。")
        session = self.SESSION_CHOICES.get(self.session_var.get())
        if session is None:
            raise ValueError("請選擇交易時段。")
        if session != "REGULAR" and order_type != "LMT":
            # Never switch the trader's MKT to LMT or regular hours on their behalf.
            raise ValueError("盤前／盤後／隔夜只接受 LMT 限價單；請改選 LMT，或改用一般時段。")
        return ManualOrderRequest(
            account_name=account,
            symbol=self.symbol_var.get().strip(),
            quantity=quantity,
            limit_price=price,
            action=self.ACTION_LABELS[self.action_var.get()],
            allow_short=bool(self.allow_short_var.get()),
            amount_usd=amount,
            order_type=order_type,
            session=session,
            equity_pct=percent,
        )

    @staticmethod
    def _requested_quantity(request):
        from manual_orders import whole_share_quantity

        if request.equity_pct is not None:
            raise ValueError("修改委託時請改用整股股數或美元金額。")
        if request.order_type == "MKT":
            if request.quantity is None:
                raise ValueError("修改時請輸入明確股數。")
            return request.quantity
        return whole_share_quantity(
            shares=request.quantity, amount_usd=request.amount_usd,
            limit_price=request.limit_price,
        )

    def _preview_order(self):
        if self._busy:
            return
        try:
            request = self._read_request()
        except ValueError as exc:
            messagebox.showwarning("手動下單", str(exc), parent=self.win)
            return
        self._set_busy(True)
        self.form_status.configure(text="正在檢查 Account、Universe 與 IB 合約…")
        threading.Thread(
            target=self._preview_worker, args=(request,), daemon=True,
        ).start()

    def _preview_worker(self, request):
        try:
            kwargs = dict(
                action=request.action, allow_short=request.allow_short,
                amount_usd=request.amount_usd,
            )
            if request.order_type == "MKT":
                kwargs["order_type"] = "MKT"
            if request.session != "REGULAR":
                kwargs["session"] = request.session
            if request.equity_pct is not None:
                kwargs["equity_pct"] = request.equity_pct
            result = self.service.preview(
                request.account_name, request.symbol,
                request.quantity, request.limit_price,
                **kwargs,
            )
            self._post(lambda: self._preview_complete(request, result, None))
        except Exception as exc:
            self._post(lambda error=str(exc): self._preview_complete(request, None, error))

    def _preview_complete(self, request, preview, error):
        self._set_busy(False)
        if error:
            self._preview = None
            self._preview_request = None
            self.confirm_button.configure(state="disabled")
            self.form_status.configure(text="無法預覽")
            self.preview_text.configure(text=error, fg=self.theme["red"])
            return
        self._preview = preview
        self._preview_request = request
        self.form_status.configure(text=f"IB 合約已確認：{preview.primary_exchange}")
        self.preview_text.configure(
            text=preview.confirmation_text,
            fg=(self.theme.get("yellow", self.theme["red"])
                if preview.price_warning else self.theme["text"]),
        )
        self.confirm_button.configure(state="normal")

    def _confirm_order(self):
        if self._busy or self._preview is None:
            return
        try:
            current = self._read_request()
        except ValueError as exc:
            messagebox.showwarning("手動下單", str(exc), parent=self.win)
            return
        if current != self._preview_request:
            self._form_changed()
            return
        preview = self._preview
        self._set_busy(True)
        self.form_status.configure(text="正在重新檢查持倉與委託…")
        threading.Thread(
            target=self._validate_worker, args=(preview, current), daemon=True,
        ).start()

    def _validate_worker(self, preview, request):
        try:
            fresh = self.service.validate_preview(preview)
            self._post(lambda: self._validated_complete(preview, request, fresh, None))
        except Exception as exc:
            self._post(lambda error=str(exc): self._validated_complete(
                preview, request, None, error,
            ))

    def _validated_complete(self, preview, request, fresh, error):
        self._set_busy(False)
        if self._closed:
            return
        if error or fresh != preview:
            self._preview = None
            self._preview_request = None
            self.confirm_button.configure(state="disabled")
            self.form_status.configure(text="預覽已失效，請重新預覽")
            self.preview_text.configure(
                text=error or "資料在預覽後變更，請重新預覽",
                fg=self.theme["red"],
            )
            return
        if self._preview != preview or self._read_request() != request:
            self._form_changed()
            return
        if not messagebox.askyesno(
            "再次核對委託",
            fresh.confirmation_text + "\n\n確認送出這筆委託？",
            icon="warning" if fresh.price_warning else "question",
            parent=self.win,
        ):
            return
        self._set_busy(True)
        self.form_status.configure(text="送單中…")
        threading.Thread(
            target=self._submit_worker, args=(preview,), daemon=True,
        ).start()

    def _submit_worker(self, preview):
        try:
            result = self.service.submit(preview)
            self._post(lambda: self._submit_complete(result, None))
        except Exception as exc:
            self._post(lambda error=str(exc): self._submit_complete(None, error))

    def _submit_complete(self, result, error):
        self._set_busy(False)
        self._preview = None
        self._preview_request = None
        self.confirm_button.configure(state="disabled")
        if error:
            self.form_status.configure(text="送單前重新檢查未通過")
            self.preview_text.configure(text=error, fg=self.theme["red"])
            return
        needs_attention = result["status"] not in {"SENT", "ACCEPTED", "PARTIAL", "FILLED"}
        # The message carries IB's reason for rejections and forced cancels.
        self.form_status.configure(
            text=f"{result['account_name']} {result['symbol']}："
                 f"{result.get('status_message') or result.get('status_label', result['status'])}",
            fg=self.theme["red"] if needs_attention else self.theme["green"],
        )
        price_condition = (
            "MKT" if result.get("order_type") == "MKT"
            else f"LMT {result['limit_price']} USD"
        )
        session = result.get("session") or "REGULAR"
        self.log(
            f"[>] Manual order [{result['account_name']}] "
            f"{result['action']} {result['quantity']}sh {result['symbol']} "
            f"{price_condition} {session} — {result['status']}"
            + (f" ({result.get('status_message', '')})" if needs_attention else "")
        )
        self._render_orders(self._active_orders())
        self._refresh_status()

    def _set_busy(self, value):
        self._busy = value
        state = "disabled" if value else "normal"
        self.preview_button.configure(state=state)
        self.refresh_button.configure(state=state)
        self.account_box.configure(state="disabled" if value else "readonly")
        self.symbol_entry.configure(state=state)
        self.input_mode_box.configure(state="disabled" if value else "readonly")
        self._apply_input_mode_state()
        self.price_entry.configure(
            state=state if self.order_type_var.get() == "LMT" else "disabled",
        )
        self.order_type_box.configure(state="disabled" if value else "readonly")
        self.session_box.configure(state="disabled" if value else "readonly")
        self.action_box.configure(state="disabled" if value else "readonly")
        self.allow_short_check.configure(
            state="normal" if not value and self.action_var.get() == "賣出" else "disabled",
        )
        self.confirm_button.configure(
            state="disabled" if value or self._preview is None else "normal",
        )
        self._update_selection_actions()

    def _active_orders(self):
        try:
            return self.service.list_orders(active_only=True)
        except TypeError:
            return [row for row in self.service.list_orders()
                    if row.get("status") not in {
                        "FILLED", "REJECTED", "CANCELLED", "EXPIRED",
                    }]

    def _selected_order(self):
        selected = self.orders.selection()
        return self._order_rows.get(selected[0]) if selected else None

    def _update_selection_actions(self, *_args):
        row = self._selected_order()
        selectable = (
            not self._busy and row is not None
            and row.get("status") not in {
                "CANCEL_PENDING", "CANCEL_UNKNOWN", "IB_UNKNOWN",
                "SUBMISSION_UNKNOWN",
            }
        )
        state = "normal" if selectable else "disabled"
        if hasattr(self, "cancel_button"):
            self.cancel_button.configure(state=state)
        if hasattr(self, "modify_button"):
            self.modify_button.configure(
                state=state if row is not None and row.get("order_type", "LMT") == "LMT"
                else "disabled",
            )

    def _order_selected(self, *_args):
        self._update_selection_actions()

    def _cancel_selected(self):
        row = self._selected_order()
        if row is None or self._busy:
            return
        if not messagebox.askyesno(
            "取消手動委託",
            f"向 IB 要求取消 {row['account_name']} 的 {row['symbol']} "
            f"{row['quantity']} 股委託？\n\n取消完成前仍可能成交。",
            parent=self.win,
        ):
            return
        self._request_cancel(row["order_ref"], None)

    def _modify_selected(self):
        row = self._selected_order()
        if row is None or self._busy:
            return
        if row.get("order_type", "LMT") == "MKT":
            return
        row_session = row.get("session") or "REGULAR"
        if (self.account_var.get().strip() != row["account_name"]
                or self.symbol_var.get().strip().upper() != row["symbol"]
                or self.action_var.get() != self.FORM_ACTION_LABELS[row["action"]]
                or bool(self.allow_short_var.get()) != (row["action"] == "SELL_SHORT")
                or self.SESSION_CHOICES.get(self.session_var.get()) != row_session):
            self.account_var.set(row["account_name"])
            self.symbol_var.set(row["symbol"])
            self.action_var.set(self.FORM_ACTION_LABELS[row["action"]])
            self.allow_short_var.set(row["action"] == "SELL_SHORT")
            self.input_mode_var.set(self.SHARES_MODE)
            self.order_type_var.set("LMT")
            self.session_var.set(self.FORM_SESSION_LABELS[row_session])
            self.quantity_var.set(str(row["quantity"]))
            self.price_var.set(str(row["limit_price"]))
            self.form_status.configure(text="已載入選定委託；調整價格或股數後再次按修改。")
            self.preview_text.configure(
                text="修改流程會先等待 IB 確認取消，再提供新委託預覽。",
                fg=self.theme["text"],
            )
            return
        try:
            request = self._read_request()
            if request.order_type != "LMT":
                raise ValueError("修改限價單時請選擇 LMT。")
            requested_quantity = self._requested_quantity(request)
        except ValueError as exc:
            messagebox.showwarning("修改手動委託", str(exc), parent=self.win)
            return
        if (request.account_name != row["account_name"]
                or request.symbol != row["symbol"]
                or request.action != ("SELL" if row["action"] == "SELL_SHORT" else row["action"])
                or request.allow_short != (row["action"] == "SELL_SHORT")
                or request.session != row_session):
            messagebox.showwarning(
                "修改手動委託", "修改只允許變更原委託的價格或股數。", parent=self.win,
            )
            return
        if (requested_quantity == int(row["quantity"])
                and request.limit_price == float(row["limit_price"])):
            messagebox.showwarning(
                "修改手動委託", "請先變更價格或股數。", parent=self.win,
            )
            return
        cash_target = (
            f"預估交易目標 ${request.amount_usd:f} → "
            if request.amount_usd is not None else ""
        )
        prompt = (
            f"先取消目前的 {row['quantity']} 股 {row['symbol']} 委託，"
            f"等 IB 確認後再預覽新條件：\n"
            f"{cash_target}{requested_quantity} 股，LMT ${request.limit_price:.2f}"
            f"（{self.FORM_SESSION_LABELS[request.session]}）\n\n"
            "原委託取消前仍可能成交；不會自動送出新委託。"
        )
        if not messagebox.askyesno(
            "取消後修改手動委託", prompt,
            parent=self.win,
        ):
            return
        self._request_cancel(row["order_ref"], request)

    def _request_cancel(self, order_ref, replacement_request):
        row = self._order_rows.get(order_ref)
        if row is not None:
            pending_row = dict(
                row, status="CANCEL_PENDING", status_label="取消確認中",
                status_message="已記錄取消要求，等待 IB 確認",
            )
            self._order_rows[order_ref] = pending_row
            try:
                values = list(self.orders.item(order_ref, "values"))
                values[6] = pending_row["status_label"]
                self.orders.item(order_ref, values=values)
            except tk.TclError:
                pass
        self._set_busy(True)
        self.order_status.configure(text="已記錄取消要求，等待 IB 確認…")
        threading.Thread(
            target=self._cancel_worker,
            args=(order_ref, replacement_request), daemon=True,
        ).start()

    def _cancel_worker(self, order_ref, replacement_request):
        try:
            result = self.service.cancel(order_ref)
            self._post(lambda: self._cancel_complete(
                order_ref, replacement_request, result, None,
            ))
        except Exception as exc:
            self._post(lambda error=str(exc): self._cancel_complete(
                order_ref, replacement_request, None, error,
            ))

    def _cancel_complete(self, order_ref, replacement_request, result, error):
        self._set_busy(False)
        if self._closed:
            return
        if error:
            self.order_status.configure(
                text=f"取消結果待確認：{error}", fg=self.theme["red"],
            )
            self._render_orders(self._active_orders())
            return
        status = result["status"]
        if replacement_request is not None:
            self._replacement_order_ref = order_ref
            self._replacement_request = replacement_request
        if status in {"CANCEL_PENDING", "CANCEL_UNKNOWN", "SUBMISSION_UNKNOWN"}:
            self.order_status.configure(
                text=result.get("status_message") or "取消結果待確認；委託仍保持未結案",
                fg=self.theme["red"],
            )
            self._refresh_status()
        else:
            self.order_status.configure(
                text=result.get("status_label", status),
                fg=self.theme["text_dim"],
            )
            self._finish_replacement_if_ready(result)
            self._render_orders(self._active_orders())

    def _finish_replacement_if_ready(self, row):
        if (self._replacement_order_ref is None
                or row.get("order_ref") != self._replacement_order_ref):
            return
        request = self._replacement_request
        self._replacement_order_ref = None
        self._replacement_request = None
        if row.get("status") != "CANCELLED":
            self.preview_text.configure(
                text=(f"IB 已回報原委託：{row.get('status_label', row.get('status'))}；"
                      "不會送出修改後委託。"),
                fg=self.theme["text"],
            )
            return
        filled = int(row.get("filled_quantity") or 0)
        remaining = max(0, self._requested_quantity(request) - filled)
        if remaining <= 0:
            self.preview_text.configure(
                text="取消確認前的成交已達修改後股數；沒有可重下的數量。",
                fg=self.theme["text"],
            )
            return
        self.account_var.set(request.account_name)
        self.symbol_var.set(request.symbol)
        # After any partial fill, prefill the remaining shares explicitly so a
        # cash target cannot recalculate to the original total on re-preview.
        self.input_mode_var.set(self.SHARES_MODE)
        self.quantity_var.set(str(remaining))
        self.price_var.set(str(request.limit_price))
        self.action_var.set(self.FORM_ACTION_LABELS[request.action])
        self.allow_short_var.set(request.allow_short)
        self.session_var.set(self.FORM_SESSION_LABELS[request.session])
        self._preview = None
        self._preview_request = None
        self.confirm_button.configure(state="disabled")
        self.form_status.configure(text="IB 已確認取消；請預覽並再次確認新委託。")
        self.preview_text.configure(
            text=(f"原委託已取消，期間成交 {filled} 股。"
                  f"新委託數量已調整為 {remaining} 股；按「預覽委託」後再確認送出。"),
            fg=self.theme["text"],
        )

    def _refresh_status(self):
        if self._closed or self._refreshing:
            return
        self._refreshing = True
        self.refresh_button.configure(state="disabled")
        self.order_status.configure(text="正在向 IB 查詢手動委託狀態…")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        try:
            result = self.service.refresh_status()
            self._post(lambda: self._refresh_complete(result, None))
        except Exception as exc:
            self._post(lambda error=str(exc): self._refresh_complete(None, error))

    def _refresh_complete(self, result, error):
        self._refreshing = False
        if self._closed:
            return
        self.refresh_button.configure(state="normal")
        if error:
            self.order_status.configure(
                text=f"IB 狀態查詢失敗：{error}", fg=self.theme["red"],
            )
            self._render_orders(self._active_orders())
            return
        self._render_orders(result["orders"])
        if self._replacement_order_ref is not None:
            previous = next((row for row in self.service.list_orders()
                             if row.get("order_ref") == self._replacement_order_ref), None)
            if previous and previous.get("status") not in {
                "SENDING", "SENT", "ACCEPTED", "PARTIAL", "CANCEL_PENDING",
                "CANCEL_UNKNOWN", "IB_UNKNOWN", "SUBMISSION_UNKNOWN",
            }:
                self._finish_replacement_if_ready(previous)
        errors = result.get("errors", [])
        self.order_status.configure(
            text=("狀態已刷新" if not errors else "；".join(errors)),
            fg=self.theme["text_dim"] if not errors else self.theme["red"],
        )

    def _render_orders(self, rows):
        if self._closed:
            return
        for item in self.orders.get_children():
            self.orders.delete(item)
        self._order_rows = {}
        for row in rows:
            if row.get("status") in {"FILLED", "REJECTED", "CANCELLED", "EXPIRED"}:
                continue
            filled = int(row.get("filled_quantity") or 0)
            avg_price = row.get("avg_fill_price")
            fill_text = f"{filled}/{row['quantity']}"
            if avg_price:
                fill_text += " @ $" + f"{float(avg_price):.2f}"
            price_condition = (
                "MKT" if row.get("order_type") == "MKT"
                else f"LMT ${float(row['limit_price']):.2f}"
            )
            order_ref = row["order_ref"]
            self._order_rows[order_ref] = row
            self.orders.insert(
                "", "end", iid=order_ref,
                values=(
                    row["account_name"], row["symbol"],
                    self.STORED_ACTION_LABELS.get(row["action"], row["action"]),
                    row["quantity"], price_condition,
                    self.STORED_SESSION_LABELS.get(
                        row.get("session") or "REGULAR", row.get("session"),
                    ),
                    row.get("status_label", row["status"]), fill_text,
                ),
            )
        self._update_selection_actions()

    def _schedule_poll(self):
        if self._closed:
            return
        self._poll_id = self.win.after(self.POLL_INTERVAL_MS, self._poll_status)

    def _poll_status(self):
        self._poll_id = None
        if self._closed:
            return
        if self.service.has_active_orders() and not self._refreshing:
            self._refresh_status()
        self._schedule_poll()

    def _post(self, callback):
        if self._closed:
            return
        try:
            self.win.after(0, callback)
        except tk.TclError:
            pass

    def close(self):
        self._closed = True
        if self._poll_id is not None:
            try:
                self.win.after_cancel(self._poll_id)
            except tk.TclError:
                pass
        try:
            self.win.destroy()
        except tk.TclError:
            pass
