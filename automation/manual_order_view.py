"""Panel window for previewing, confirming, and tracking manual orders."""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

import manual_order_form as order_form
from ime_guard import ascii_entry
from manual_order_form import ManualOrderForm


class ManualOrderWindow:
    """Panel adapter; all account, contract, and order decisions live in service."""

    POLL_INTERVAL_MS = 10_000
    SHARES_MODE = order_form.SHARES_MODE
    CASH_MODE = order_form.CASH_MODE
    PERCENT_MODE = order_form.PERCENT_MODE
    PERCENT_CHOICES = order_form.PERCENT_CHOICES
    ACTION_LABELS = order_form.ACTION_LABELS
    STORED_ACTION_LABELS = order_form.STORED_ACTION_LABELS
    FORM_ACTION_LABELS = order_form.FORM_ACTION_LABELS
    SESSION_CHOICES = order_form.SESSION_CHOICES
    FORM_SESSION_LABELS = order_form.FORM_SESSION_LABELS
    STORED_SESSION_LABELS = order_form.STORED_SESSION_LABELS

    SYMBOL_SUGGESTION_LIMIT = 8

    def __init__(self, parent, account_names, service, theme, fonts, log=None,
                 symbol_loader=None):
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
        # None = 載入中；False = 取不到清單（只少提示，不影響下單）
        self._symbols = None
        self._suggested = []
        self._accepting_symbol = False

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
        self.symbol_var.trace_add("write", self._symbol_changed)

        self._build(account_names)
        self._load_symbols(symbol_loader)
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
        symbol_cell = tk.Frame(form, bg=t["bg2"])
        symbol_cell.grid(row=0, column=3, sticky="ew", pady=5)
        self.symbol_entry = ttk.Entry(symbol_cell, textvariable=self.symbol_var, width=18)
        self.symbol_entry.pack(fill="x")
        # width=1：提示長短不影響欄寬，表單不會跟著跳動
        self.symbol_hint = tk.Label(
            symbol_cell, text="", font=self.fonts["small"], width=1,
            bg=t["bg2"], fg=t["text_dim"], anchor="w",
        )
        self.symbol_hint.pack(fill="x")

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

        # 代號建議浮在代碼欄下方（右緣對齊），不另開視窗、不搶焦點。
        self.symbol_suggestions = tk.Listbox(
            self.win, activestyle="none", exportselection=False,
            font=("Consolas", 10), bg=t["bg"], fg=t["text"],
            selectbackground=t["accent"], selectforeground=t["accent_hl"],
            highlightthickness=1, relief="flat",
        )
        self.symbol_suggestions.bind("<ButtonRelease-1>", self._accept_suggestion)
        self.symbol_entry.bind("<Down>", lambda _e: self._move_suggestion(1))
        self.symbol_entry.bind("<Up>", lambda _e: self._move_suggestion(-1))
        self.symbol_entry.bind("<Return>", self._accept_suggestion)
        self.symbol_entry.bind("<Escape>", lambda _e: self._hide_suggestions())
        self.symbol_entry.bind(
            "<FocusOut>", lambda _e: self.win.after(150, self._hide_if_unfocused),
            add="+",
        )

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

    def _load_symbols(self, loader):
        if loader is None:
            from us_symbols import load_directory as loader

        result = []

        def worker():
            try:
                result.append(loader())
            except Exception:
                result.append(None)

        # 下載可能要數秒；結果由主執行緒輪詢取回，背景執行緒不碰 Tk。
        def collect():
            if self._closed:
                return
            if not result:
                self.win.after(200, collect)
                return
            self._symbols = result[0] if result[0] else False
            self._update_symbol_hint()

        self._update_symbol_hint()
        threading.Thread(target=worker, daemon=True).start()
        self.win.after(200, collect)

    def _symbol_changed(self, *_args):
        self._update_symbol_hint()
        if (self._symbols and not self._accepting_symbol
                and self._focused() is self.symbol_entry):
            self._show_suggestions(
                self._symbols.search(self.symbol_var.get(), self.SYMBOL_SUGGESTION_LIMIT),
            )

    def _update_symbol_hint(self):
        hint, color = order_form.symbol_hint(self._symbols, self.symbol_var.get())
        self.symbol_hint.configure(text=hint, fg=self.theme[color])

    def _show_suggestions(self, matches):
        self._suggested = [symbol for symbol, _name in matches]
        box = self.symbol_suggestions
        box.delete(0, "end")
        lines = order_form.suggestion_lines(matches, self.symbol_var.get())
        if not lines:
            self._hide_suggestions()
            return
        for line in lines:
            box.insert("end", line)
        box.configure(height=len(lines))
        box.selection_set(0)
        box.place(in_=self.symbol_entry, relx=1.0, rely=1.0, y=2,
                  anchor="ne", width=520)
        box.lift()

    def _hide_suggestions(self):
        self._suggested = []
        self.symbol_suggestions.place_forget()

    def _focused(self):
        try:
            return self.win.focus_get()
        except (KeyError, tk.TclError):  # Combobox 下拉清單開著時 focus_get 會 KeyError
            return None

    def _hide_if_unfocused(self):
        if self._closed:
            return
        focus = self._focused()
        if focus not in (self.symbol_entry, self.symbol_suggestions):
            self._hide_suggestions()

    def _move_suggestion(self, step):
        if not self._suggested:
            return "break"
        box = self.symbol_suggestions
        current = box.curselection()
        index = (current[0] + step) if current else 0
        index = max(0, min(index, len(self._suggested) - 1))
        box.selection_clear(0, "end")
        box.selection_set(index)
        box.see(index)
        return "break"

    def _accept_suggestion(self, _event=None):
        if not self._suggested:
            return None
        current = self.symbol_suggestions.curselection()
        symbol = self._suggested[current[0] if current else 0]
        self._hide_suggestions()
        self._accepting_symbol = True
        try:
            self.symbol_var.set(symbol)
        finally:
            self._accepting_symbol = False
        self.symbol_entry.icursor("end")
        self.symbol_entry.focus_set()
        return "break"

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

    def _form(self):
        return ManualOrderForm(
            account=self.account_var.get(),
            symbol=self.symbol_var.get(),
            input_mode=self.input_mode_var.get(),
            quantity=self.quantity_var.get(),
            amount=self.amount_var.get(),
            percent=self.percent_var.get(),
            order_type=self.order_type_var.get(),
            price=self.price_var.get(),
            session=self.session_var.get(),
            action=self.action_var.get(),
            allow_short=bool(self.allow_short_var.get()),
        )

    def _read_request(self):
        return order_form.parse_request(self._form())

    @staticmethod
    def _requested_quantity(request):
        return order_form.requested_quantity(request)

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
            result = self.service.preview(
                request.account_name, request.symbol,
                request.quantity, request.limit_price,
                **order_form.preview_kwargs(request),
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
        self.form_status.configure(
            text=order_form.submit_status_text(result),
            fg=self.theme["red"] if order_form.needs_attention(result) else self.theme["green"],
        )
        self.log(order_form.submit_log_line(result))
        self._render_orders(self._active_orders())
        self._refresh_status()

    def _set_busy(self, value):
        self._busy = value
        state = "disabled" if value else "normal"
        self.preview_button.configure(state=state)
        self.refresh_button.configure(state=state)
        self.account_box.configure(state="disabled" if value else "readonly")
        self.symbol_entry.configure(state=state)
        if value:
            self._hide_suggestions()
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
            return order_form.open_orders(self.service.list_orders())

    def _selected_order(self):
        selected = self.orders.selection()
        return self._order_rows.get(selected[0]) if selected else None

    def _update_selection_actions(self, *_args):
        row = self._selected_order()
        if hasattr(self, "cancel_button"):
            self.cancel_button.configure(
                state="normal" if not self._busy and order_form.can_cancel(row) else "disabled",
            )
        if hasattr(self, "modify_button"):
            self.modify_button.configure(
                state="normal" if not self._busy and order_form.can_modify(row) else "disabled",
            )

    def _order_selected(self, *_args):
        self._update_selection_actions()

    def _cancel_selected(self):
        row = self._selected_order()
        if row is None or self._busy:
            return
        if not messagebox.askyesno(
            "取消手動委託", order_form.cancel_prompt(row), parent=self.win,
        ):
            return
        self._request_cancel(row["order_ref"], None)

    def _modify_selected(self):
        row = self._selected_order()
        if row is None or self._busy:
            return
        if row.get("order_type", "LMT") == "MKT":
            return
        if not order_form.form_matches_row(self._form(), row):
            loaded = order_form.form_from_row(row)
            self.account_var.set(loaded.account)
            self.symbol_var.set(loaded.symbol)
            self.action_var.set(loaded.action)
            self.allow_short_var.set(loaded.allow_short)
            self.input_mode_var.set(loaded.input_mode)
            self.order_type_var.set(loaded.order_type)
            self.session_var.set(loaded.session)
            self.quantity_var.set(loaded.quantity)
            self.price_var.set(loaded.price)
            self.form_status.configure(text="已載入選定委託；調整價格或股數後再次按修改。")
            self.preview_text.configure(
                text="修改流程會先等待 IB 確認取消，再提供新委託預覽。",
                fg=self.theme["text"],
            )
            return
        try:
            request = self._read_request()
            requested_quantity = order_form.modify_quantity(request)
            order_form.check_modify(row, request, requested_quantity)
        except ValueError as exc:
            messagebox.showwarning("修改手動委託", str(exc), parent=self.win)
            return
        if not messagebox.askyesno(
            "取消後修改手動委託", order_form.modify_prompt(row, request, requested_quantity),
            parent=self.win,
        ):
            return
        self._request_cancel(row["order_ref"], request)

    def _request_cancel(self, order_ref, replacement_request):
        row = self._order_rows.get(order_ref)
        if row is not None:
            pending_row = order_form.cancel_pending_row(row)
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
        replacement = order_form.replacement_after_cancel(row, request)
        if replacement.form is None:
            self.preview_text.configure(text=replacement.message, fg=self.theme["text"])
            return
        loaded = replacement.form
        self.account_var.set(loaded.account)
        self.symbol_var.set(loaded.symbol)
        self.input_mode_var.set(loaded.input_mode)
        self.quantity_var.set(loaded.quantity)
        self.price_var.set(loaded.price)
        self.action_var.set(loaded.action)
        self.allow_short_var.set(loaded.allow_short)
        self.session_var.set(loaded.session)
        self._preview = None
        self._preview_request = None
        self.confirm_button.configure(state="disabled")
        self.form_status.configure(text="IB 已確認取消；請預覽並再次確認新委託。")
        self.preview_text.configure(text=replacement.message, fg=self.theme["text"])

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
            if previous and not order_form.replacement_waiting(previous):
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
        for row in order_form.open_orders(rows):
            order_ref = row["order_ref"]
            self._order_rows[order_ref] = row
            self.orders.insert(
                "", "end", iid=order_ref, values=order_form.order_row_values(row),
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
