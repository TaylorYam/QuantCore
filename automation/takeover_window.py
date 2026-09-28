"""
TakeoverWindow — 交給策略接管視窗（#107）

職責：
  1. 開窗就顯示計算進度（連 IB、算策略目標）
  2. 換成預覽：Account、標的、手動紀錄／IB 現有股數、策略整數目標、差額與方向
  3. Cancel 什麼都不送；「確認接管」→ 再確認一次 → on_confirm(preview)
  4. 顯示送單結果（完成／等成交／失敗）或「資料已變動」的新預覽

Pure UI — 不連 IB、不管執行緒、不發 Telegram（control_panel 負責）。
依賴以 constructor 注入，與 SyncPreviewWindow 同一套 theme／按鈕工廠。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from typing import Callable

_RESULT_TITLE = {
    "COMPLETED": "✅ 接管完成",
    "PENDING": "🕐 已送出，等成交與對帳",
    "FAILED": "❌ 接管未完成",
    "DRY_RUN": "[DRY RUN] 沒有送單",
}


class TakeoverWindow:
    def __init__(self, parent: tk.Misc, account_name: str, symbol: str,
                 theme: dict, log: Callable[[str], None], btn_factory: Callable,
                 on_confirm: Callable):
        self.parent = parent
        self.account_name = account_name
        self.symbol = symbol
        self.t = theme
        self._log = log
        self._btn = btn_factory
        self.on_confirm = on_confirm
        self.preview = None
        self.win: tk.Toplevel | None = None
        self._status: tk.Label | None = None
        self._body: tk.Frame | None = None
        self._closed = False

    # ── Public API ─────────────────────────────────────────────

    def show_computing(self) -> None:
        t = self.t
        title = f"交給策略接管 — {self.account_name} {self.symbol}"
        win = tk.Toplevel(self.parent)
        self.win = win
        win.title(title)
        win.configure(bg=t["bg"])
        win.geometry("640x460")
        win.transient(self.parent)
        win.protocol("WM_DELETE_WINDOW", self._close)
        hdr = tk.Frame(win, bg=t["accent"], padx=12, pady=8)
        hdr.pack(fill="x")
        tk.Label(hdr, text=title, font=("Segoe UI", 13, "bold"),
                 bg=t["accent"], fg=t["text"]).pack(anchor="w")
        self._status = tk.Label(hdr, text="計算中…", font=("Segoe UI", 9),
                                bg=t["accent"], fg=t["text_dim"])
        self._status.pack(anchor="w")
        self._body = tk.Frame(win, bg=t["bg2"], padx=12, pady=10)
        self._body.pack(fill="both", expand=True, padx=12, pady=12)
        self._footer([("Close", self._close, t["btn_bg"])])

    def progress(self, msg: str) -> None:
        if self._alive():
            self._label(msg.strip())

    def show_preview(self, preview, note: str = "") -> None:
        """預覽（note = 為何換了新預覽，例如資料已變動）。"""
        if not self._alive():
            return
        self.preview = preview
        t = self.t
        self._status.config(text="確認後只送這一檔", fg=t["text_dim"])
        self._clear()
        if note:
            self._label(f"⚠️ {note}", fg=t["red"])
        for line in preview.lines():
            self._label(line)
        confirm = ("確認接管" if preview.quantity == 0
                   else f"確認接管並送出 {preview.action} {preview.quantity}")
        self._footer([("Cancel", self._close, t["btn_bg"]),
                      (confirm, self._handle_confirm, t["red"])])

    def show_sending(self) -> None:
        if self._alive():
            self._status.config(text="送單中…", fg=self.t["text_dim"])
            self._footer([])

    def show_result(self, result) -> None:
        if not self._alive():
            return
        t = self.t
        ok = result.status == "COMPLETED"
        self._status.config(text=_RESULT_TITLE.get(result.status, result.status),
                            fg=t["green"] if ok else t["text"])
        self._label("")
        self._label(result.message, fg=t["green"] if ok else t["text"])
        self._footer([("Close", self._close, t["btn_bg"])])

    def show_error(self, msg: str) -> None:
        if not self._alive():
            return
        self._status.config(text="不能接管", fg=self.t["red"])
        self._label(msg, fg=self.t["red"])
        self._footer([("Close", self._close, self.t["btn_bg"])])

    # ── internals ──────────────────────────────────────────────

    def _alive(self) -> bool:
        if self._closed or self.win is None:
            return False
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def _label(self, text: str, fg: str | None = None) -> None:
        tk.Label(self._body, text=text, font=("Consolas", 9), justify="left",
                 wraplength=590, bg=self.t["bg2"],
                 fg=fg or self.t["text"]).pack(anchor="w")

    def _clear(self) -> None:
        for child in self._body.winfo_children():
            child.destroy()

    def _footer(self, buttons) -> None:
        old = getattr(self, "_buttons", None)
        if old is not None:
            old.destroy()
        frame = tk.Frame(self.win, bg=self.t["bg"], padx=12, pady=8)
        frame.pack(fill="x")
        self._buttons = frame
        for i, (text, cmd, color) in enumerate(buttons):
            self._btn(frame, text, cmd, color, width=24 if i else 12).pack(
                side="right" if i else "left")

    def _close(self) -> None:
        self._closed = True
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass

    def _handle_confirm(self) -> None:
        p = self.preview
        what = ("不需下單，直接把這檔交給策略" if p.quantity == 0
                else f"送出 {p.action} {p.quantity} 股 {p.symbol}（LMT，只這一檔）")
        if not messagebox.askyesno(
                "確認交給策略接管",
                f"[{p.account_name}] {p.symbol}\n{what}\n"
                "成交並與 IB 持倉對上後才解除暫停。確定？",
                parent=self.win):
            return
        self._log(f"[>] 交給策略接管 [{p.account_name}] {p.symbol}...")
        self.show_sending()
        self.on_confirm(p)
