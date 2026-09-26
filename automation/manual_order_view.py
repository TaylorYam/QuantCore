"""Panel window for previewing, confirming, and tracking manual limit buys."""

from __future__ import annotations

import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk


@dataclass(frozen=True)
class ManualOrderRequest:
    account_name: str
    symbol: str
    quantity: int
    limit_price: float


class ManualOrderWindow:
    """Panel adapter; all account, contract, and order decisions live in service."""

    POLL_INTERVAL_MS = 10_000

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

        self.win = tk.Toplevel(parent)
        self.win.title("手動下單 — 整股限價買進")
        self.win.configure(bg=theme["bg"])
        self.win.geometry("700x600")
        self.win.minsize(650, 520)
        self.win.transient(parent)
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        self.account_var = tk.StringVar(self.win, value="")
        self.symbol_var = tk.StringVar(self.win, value="")
        self.quantity_var = tk.StringVar(self.win, value="")
        self.price_var = tk.StringVar(self.win, value="")
        for variable in (
            self.account_var, self.symbol_var,
            self.quantity_var, self.price_var,
        ):
            variable.trace_add("write", self._form_changed)

        self._build(account_names)
        self._render_orders(self.service.list_orders())
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
            header, text="一般時段 · BUY LMT · DAY",
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

        self._field_label(form, "整股股數", 1, 0)
        self.quantity_entry = ttk.Entry(
            form, textvariable=self.quantity_var, width=18,
        )
        self.quantity_entry.grid(row=1, column=1, sticky="ew", padx=(0, 14), pady=5)

        self._field_label(form, "自填限價（USD）", 1, 2)
        self.price_entry = ttk.Entry(form, textvariable=self.price_var, width=18)
        self.price_entry.grid(row=1, column=3, sticky="ew", pady=5)

        helper = tk.Label(
            form,
            text="只接受美國市場的美元個股或 ETF；策略 Universe 標的會被阻擋。",
            font=self.fonts["small"], bg=t["bg2"], fg=t["text_dim"],
            anchor="w",
        )
        helper.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(7, 2))

        actions = tk.Frame(form, bg=t["bg2"])
        actions.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(9, 0))
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

        columns = ("account", "symbol", "quantity", "limit", "status", "filled")
        self.orders = ttk.Treeview(
            orders_frame, columns=columns, show="headings", height=8,
        )
        headings = {
            "account": ("Account", 100),
            "symbol": ("標的", 80),
            "quantity": ("股數", 65),
            "limit": ("限價", 85),
            "status": ("IB 狀態", 135),
            "filled": ("成交", 120),
        }
        for column, (title, width) in headings.items():
            self.orders.heading(column, text=title)
            self.orders.column(column, width=width, minwidth=55, anchor="center")
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

    def _read_request(self):
        account = self.account_var.get().strip()
        if not account:
            raise ValueError("請先選擇這筆委託要送往的 Account。")
        raw_quantity = self.quantity_var.get().strip()
        if not raw_quantity.isdecimal():
            raise ValueError("整股股數必須是正整數。")
        quantity = int(raw_quantity)
        if quantity <= 0:
            raise ValueError("整股股數必須大於零。")
        try:
            price = float(self.price_var.get().strip())
        except ValueError as exc:
            raise ValueError("請輸入自填限價。") from exc
        if price <= 0:
            raise ValueError("限價必須大於零。")
        return ManualOrderRequest(
            account_name=account,
            symbol=self.symbol_var.get().strip(),
            quantity=quantity,
            limit_price=price,
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
            result = self.service.preview(
                request.account_name, request.symbol,
                request.quantity, request.limit_price,
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
            fg=self.theme["text"],
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
        if not messagebox.askyesno(
            "再次核對委託",
            self._preview.confirmation_text + "\n\n確認送出這筆委託？",
            parent=self.win,
        ):
            return
        preview = self._preview
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
            text=f"{result['account_name']} {result['symbol']}："
                 f"{result.get('status_label', result['status'])}",
            fg=self.theme["green"],
        )
        self.log(
            f"[>] Manual order [{result['account_name']}] "
            f"BUY {result['quantity']}sh {result['symbol']} "
            f"LMT {result['limit_price']} USD — {result['status']}"
        )
        self._render_orders(self.service.list_orders())
        self._refresh_status()

    def _set_busy(self, value):
        self._busy = value
        state = "disabled" if value else "normal"
        self.preview_button.configure(state=state)
        self.refresh_button.configure(state=state)
        self.account_box.configure(state="disabled" if value else "readonly")
        self.symbol_entry.configure(state=state)
        self.quantity_entry.configure(state=state)
        self.price_entry.configure(state=state)
        self.confirm_button.configure(
            state="disabled" if value or self._preview is None else "normal",
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
            self._render_orders(self.service.list_orders())
            return
        self._render_orders(result["orders"])
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
        for row in rows:
            filled = int(row.get("filled_quantity") or 0)
            avg_price = row.get("avg_fill_price")
            fill_text = f"{filled}/{row['quantity']}"
            if avg_price:
                fill_text += " @ $" + f"{float(avg_price):.2f}"
            limit_text = "$" + str(row["limit_price"])
            self.orders.insert(
                "", "end",
                values=(
                    row["account_name"], row["symbol"], row["quantity"],
                    limit_text, row.get("status_label", row["status"]), fill_text,
                ),
            )

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
