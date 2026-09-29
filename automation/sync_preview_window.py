"""
SyncPreviewWindow — Sync Position 視窗（S10/R3.1 從 control_panel 抽出）

職責：
  1. 選完帳戶就開窗，邊算邊顯示進度（#37）
  2. 算完換成預覽：理論持倉、實際持倉、交易清單與總計
  3. 讓使用者按 Cancel / Confirm
  4. Confirm 後呼叫傳入的 on_confirm callback（帶 SyncPlan 當參數）

Design:
  - Pure UI — 不做 IB 查詢、不管理背景 thread、不寫 Excel/Telegram
  - 每一列的文字與顏色由 sync_preview_rows 產生（與 Qt Panel 共用）
  - 所有 Panel-specific 的依賴（theme colors、log 函式、btn factory）
    以 constructor 參數明確傳入，沒有 import control_panel
  - 計算中不 grab，Panel 照樣能操作；換成預覽才 modal
  - 視窗關掉之後 progress / show_plan / show_error 都不做事 —
    背景計算停不下來，但算完的結果直接丟掉

Usage:
    theme = {"bg": "#0b1120", "bg2": "#131d35", ...}
    window = SyncPreviewWindow(
        parent=self,                 # tk.Tk or Toplevel
        account_name="Taylor",
        theme=theme,                 # color dict
        log=self._log,               # Panel's output log callback
        btn_factory=self._btn,       # Panel's styled button factory
        on_confirm=lambda p: self._execute_sync(p),
    )
    window.show_computing()          # 立刻開窗
    window.progress("  Connecting to IB...")   # 背景計算的每一行
    window.show_plan(plan)           # 算完換成預覽（或 show_error(msg)）
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from typing import Callable

from sync_position import SyncPlan
import sync_preview_rows as rows


# Expected theme keys (documented for callers)
_THEME_KEYS = {
    "bg", "bg2", "bg3", "accent", "accent_hl",
    "text", "text_dim", "blue", "green", "red", "btn_bg",
}


class SyncPreviewWindow:
    """Sync Position window — progress while computing, then the plan preview."""

    def __init__(
        self,
        parent: tk.Misc,
        account_name: str,
        theme: dict,
        log: Callable[[str], None],
        btn_factory: Callable,
        on_confirm: Callable[[SyncPlan], None],
        on_takeover: Callable[[str], None] | None = None,
    ):
        missing = _THEME_KEYS - set(theme)
        if missing:
            raise ValueError(f"theme missing keys: {sorted(missing)}")
        self.parent = parent
        self.account_name = account_name
        self.plan: SyncPlan | None = None
        self.t = theme
        self._log = log
        self._btn = btn_factory
        self.on_confirm = on_confirm
        # 「交給策略接管」（#107）：Panel 注入；None = 不顯示按鈕
        self.on_takeover = on_takeover
        self.win: tk.Toplevel | None = None
        self._status: tk.Label | None = None
        self._body: tk.Frame | None = None
        self._progress: tk.Frame | None = None
        self._closed = False

    # ── Public API ─────────────────────────────────────────────

    def show_computing(self) -> None:
        """Open the window before the plan exists, showing compute progress."""
        self._build_window()
        self._build_computing_body()

    def progress(self, msg: str) -> None:
        """Append one compute-progress line."""
        if not self._alive():
            return
        tk.Label(self._progress, text=msg.strip(), font=("Consolas", 9),
                 bg=self.t["bg2"], fg=self.t["text"]).pack(anchor="w")

    def show_plan(self, plan: SyncPlan) -> None:
        """Swap the progress view for the plan preview, in the same window."""
        if not self._alive():
            return
        self.plan = plan
        # Log summary first (goes to parent Panel's Output Log)
        self._log_summary()
        self._status.config(text=rows.plan_subtitle(self.plan), fg=self.t["text_dim"])
        self._clear_body()
        self._build_preview_body()
        self.win.grab_set()

    def show_error(self, msg: str) -> None:
        """Keep the progress lines, show where the compute stopped."""
        if not self._alive():
            return
        t = self.t
        self._status.config(text="計算失敗", fg=t["red"])
        tk.Label(self._progress, text=msg, font=("Consolas", 9),
                 justify="left", wraplength=690,
                 bg=t["bg2"], fg=t["red"]).pack(anchor="w", pady=(6, 0))

    # ── Window state ───────────────────────────────────────────

    def _alive(self) -> bool:
        if self._closed or self.win is None:
            return False
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    # ── Log summary（寫到 parent Panel 的 Output Log）────────────

    def _log_summary(self) -> None:
        for line in rows.log_summary_lines(self.plan):
            self._log(line)

    # ── Window building ────────────────────────────────────────

    def _build_window(self) -> None:
        t = self.t
        title = f"Sync Position — {self.account_name}"

        win = tk.Toplevel(self.parent)
        self.win = win
        win.title(title)
        win.configure(bg=t["bg"])
        win.geometry("750x620")
        win.resizable(True, True)
        win.transient(self.parent)
        win.protocol("WM_DELETE_WINDOW", self._close)

        ico_path = getattr(self.parent, "_ico_path", None)
        if ico_path and ico_path.exists():
            win.iconbitmap(str(ico_path))

        # Title header — subtitle carries the compute status, then the plan's
        hdr = tk.Frame(win, bg=t["accent"], padx=12, pady=8)
        hdr.pack(fill="x")
        tk.Label(hdr, text=title,
                 font=("Segoe UI", 13, "bold"), bg=t["accent"], fg=t["text"]).pack(anchor="w")
        self._status = tk.Label(hdr, text="計算中…",
                                font=("Segoe UI", 9), bg=t["accent"], fg=t["text_dim"])
        self._status.pack(anchor="w")

        self._body = tk.Frame(win, bg=t["bg"])
        self._body.pack(fill="both", expand=True)

    def _clear_body(self) -> None:
        for child in self._body.winfo_children():
            child.destroy()
        self._progress = None

    def _build_computing_body(self) -> None:
        t = self.t
        self._progress = tk.Frame(self._body, bg=t["bg2"], padx=10, pady=8)
        self._progress.pack(fill="x", padx=12, pady=12)

        btn_frame = tk.Frame(self._body, bg=t["bg"], padx=12, pady=4)
        btn_frame.pack(fill="x")
        self._btn(btn_frame, "Close", self._close, t["btn_bg"], width=14).pack(side="left")

    def _build_preview_body(self) -> None:
        plan = self.plan
        t = self.t
        body = self._body

        # Scrollable main region
        main_canvas = tk.Canvas(body, bg=t["bg"], highlightthickness=0)
        main_sb = tk.Scrollbar(body, orient="vertical", command=main_canvas.yview)
        main_frame = tk.Frame(main_canvas, bg=t["bg"])
        main_frame.bind("<Configure>",
                        lambda e: main_canvas.configure(scrollregion=main_canvas.bbox("all")))
        main_canvas.create_window((0, 0), window=main_frame, anchor="nw")
        main_canvas.configure(yscrollcommand=main_sb.set)
        main_canvas.pack(side="left", fill="both", expand=True)
        main_sb.pack(side="right", fill="y")
        main_canvas.bind_all("<MouseWheel>",
                             lambda e: main_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

        # Build sections
        self._build_section_targets(main_frame)
        self._build_section_current(main_frame)
        if plan.paused:
            self._build_section_paused(main_frame)

        # Trade list + buttons
        if not plan.orders:
            self._build_no_trades_footer(main_frame)
        else:
            self._build_section_trades(main_frame)
            self._build_buttons(main_frame)

    # ── Section 1: Strategy Target ─────────────────────────────

    def _build_section_targets(self, parent: tk.Frame) -> None:
        t = self.t

        sec = tk.Frame(parent, bg=t["bg"], padx=12, pady=6)
        sec.pack(fill="x")
        tk.Label(sec, text="Strategy Target (理論持倉)",
                 font=("Segoe UI", 10, "bold"),
                 bg=t["bg"], fg=t["accent_hl"]).pack(anchor="w")

        sections = rows.target_sections(self.plan)
        for section in sections:
            frame = tk.Frame(sec, bg=t["bg2"], padx=8, pady=4)
            frame.pack(fill="x", pady=(4 if section.strategy == "SR" else 2, 2))
            tk.Label(frame, text=section.title,
                     font=("Consolas", 9, "bold"),
                     bg=t["bg2"], fg=t[section.color]).pack(anchor="w")
            for line in section.lines:
                tk.Label(frame, text=line,
                         font=("Consolas", 9), bg=t["bg2"], fg=t["text"],
                         ).pack(anchor="w")

        if not sections:
            tk.Label(sec, text="  (no target positions — all cash)",
                     font=("Consolas", 9),
                     bg=t["bg"], fg=t["text_dim"]).pack(anchor="w")

    # ── Section 2: Current Holdings ────────────────────────────

    def _build_section_current(self, parent: tk.Frame) -> None:
        t = self.t

        sec = tk.Frame(parent, bg=t["bg"], padx=12, pady=6)
        sec.pack(fill="x")
        tk.Label(sec, text="Current Holdings (實際持倉)",
                 font=("Segoe UI", 10, "bold"),
                 bg=t["bg"], fg=t["accent_hl"]).pack(anchor="w")

        # 策略相關持倉
        holdings = rows.current_holding_rows(self.plan)
        if holdings:
            cur_frame = tk.Frame(sec, bg=t["bg2"], padx=8, pady=4)
            cur_frame.pack(fill="x", pady=(4, 2))
            for holding in holdings:
                lbl = tk.Frame(cur_frame, bg=t["bg2"])
                lbl.pack(fill="x")
                tk.Label(lbl, text=holding.text,
                         font=("Consolas", 9), bg=t["bg2"], fg=t["text"]).pack(side="left")
                tk.Label(lbl, text=f"  [{holding.tag}]",
                         font=("Consolas", 9), bg=t["bg2"],
                         fg=t[holding.tag_color]).pack(side="left")
                if holding.mark:
                    tk.Label(lbl, text=holding.mark,
                             font=("Consolas", 9), bg=t["bg2"],
                             fg=t[holding.mark_color]).pack(side="left")
        else:
            tk.Label(sec, text="  (no strategy holdings)",
                     font=("Consolas", 9),
                     bg=t["bg"], fg=t["text_dim"]).pack(anchor="w")

        # 手動持倉
        others = rows.other_holding_lines(self.plan)
        if others:
            oth_frame = tk.Frame(sec, bg=t["bg2"], padx=8, pady=4)
            oth_frame.pack(fill="x", pady=(2, 2))
            tk.Label(oth_frame, text="Other (excluded from sync)",
                     font=("Consolas", 9, "bold"),
                     bg=t["bg2"], fg=t["text_dim"]).pack(anchor="w")
            for line in others:
                tk.Label(
                    oth_frame, text=line,
                    font=("Consolas", 9), bg=t["bg2"], fg=t["text_dim"],
                ).pack(anchor="w")

    # ── Section 2b: 手動持倉暫停（#106）──────────────────────────

    def _build_section_paused(self, parent: tk.Frame) -> None:
        """暫停標的與原因；這些單不在下方交易清單、不會送出。"""
        t = self.t

        sec = tk.Frame(parent, bg=t["bg"], padx=12, pady=6)
        sec.pack(fill="x")
        tk.Label(sec, text="⏸ 手動持倉暫停（這些標的的策略單不會送出）",
                 font=("Segoe UI", 10, "bold"),
                 bg=t["bg"], fg=t.get("yellow", t["accent_hl"])).pack(anchor="w")
        frame = tk.Frame(sec, bg=t["bg2"], padx=8, pady=4)
        frame.pack(fill="x", pady=(4, 2))
        for paused in rows.paused_rows(self.plan):
            row = tk.Frame(frame, bg=t["bg2"])
            row.pack(fill="x")
            tk.Label(row, text=paused.text,
                     font=("Consolas", 9), bg=t["bg2"], fg=t["text"]).pack(side="left")
            if self.on_takeover is not None:
                self._btn(row, "交給策略接管",
                          lambda s=paused.symbol: self._handle_takeover(s),
                          t["btn_bg"], width=14).pack(side="right")
            tk.Label(frame, text=paused.reason, font=("Consolas", 9),
                     justify="left", wraplength=680,
                     bg=t["bg2"], fg=t["text_dim"]).pack(anchor="w")

    # ── Section 3: Trade table ─────────────────────────────────

    def _build_section_trades(self, parent: tk.Frame) -> None:
        plan = self.plan
        t = self.t

        sep = tk.Frame(parent, bg=t["accent_hl"], height=1)
        sep.pack(fill="x", padx=12, pady=6)

        sec = tk.Frame(parent, bg=t["bg"], padx=12, pady=4)
        sec.pack(fill="x")
        tk.Label(sec,
                 text=rows.trades_heading(plan),
                 font=("Segoe UI", 10, "bold"),
                 bg=t["bg"], fg=t["accent_hl"]).pack(anchor="w")

        # 表頭
        thdr = tk.Frame(sec, bg=t["bg3"], padx=4, pady=3)
        thdr.pack(fill="x", pady=(4, 0))
        for c, w in zip(rows.TRADE_COLUMNS, rows.TRADE_COL_WIDTHS):
            tk.Label(thdr, text=c, font=("Consolas", 9, "bold"),
                     bg=t["bg3"], fg=t["accent_hl"], width=w, anchor="w").pack(side="left")

        # 行：SELL 先，BUY 後
        for trade in rows.trade_rows(plan):
            row = tk.Frame(sec, bg=t["bg"], padx=4, pady=1)
            row.pack(fill="x")
            for v, w in zip(trade.values, rows.TRADE_COL_WIDTHS):
                fg = t[trade.color] if v in ('BUY', 'SELL') else t["text"]
                tk.Label(row, text=v, font=("Consolas", 9),
                         bg=t["bg"], fg=fg, width=w, anchor="w").pack(side="left")

        # 總計
        summary = tk.Frame(parent, bg=t["bg3"], padx=12, pady=6)
        summary.pack(fill="x", padx=12, pady=(6, 0))
        tk.Label(
            summary,
            text=rows.trade_totals_text(plan),
            font=("Consolas", 10, "bold"),
            bg=t["bg3"], fg=t["text"],
        ).pack()

    # ── Button rows ────────────────────────────────────────────

    def _build_no_trades_footer(self, parent: tk.Frame) -> None:
        """When plan.orders is empty — show 'already on target' + single OK button."""
        t = self.t
        sec = tk.Frame(parent, bg=t["bg"], padx=12, pady=4)
        sec.pack(fill="x")
        tk.Label(sec, text=rows.trades_heading(self.plan),
                 font=("Segoe UI", 10, "bold"),
                 bg=t["bg"], fg=t["accent_hl"]).pack(anchor="w")
        tk.Label(sec, text="  Already on target — no trades needed",
                 font=("Consolas", 10),
                 bg=t["bg"], fg=t["green"]).pack(anchor="w", pady=8)

        btn_frame = tk.Frame(parent, bg=t["bg"], padx=12, pady=10)
        btn_frame.pack(fill="x")
        self._btn(btn_frame, "OK", self._close, t["btn_bg"], width=12).pack()
        self._log("[OK] Already on target, no trades needed")

    def _build_buttons(self, parent: tk.Frame) -> None:
        """Cancel + Confirm buttons at bottom."""
        t = self.t
        btn_frame = tk.Frame(parent, bg=t["bg"], padx=12, pady=10)
        btn_frame.pack(fill="x")
        self._btn(btn_frame, "Cancel", self._close,
                  t["btn_bg"], width=14).pack(side="left", padx=(0, 10))
        self._btn(btn_frame, "Confirm & Send (LMT)",
                  self._handle_confirm,
                  t["red"], width=22,
                  font=("Segoe UI", 10, "bold")).pack(side="right")

    # ── Button handlers ───────────────────────────────────────

    def _close(self) -> None:
        self._closed = True
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass

    def _handle_takeover(self, symbol: str) -> None:
        """交給策略接管（#107）：這份 Sync 預覽作廢，改開單一標的接管視窗。"""
        self._close()
        self.on_takeover(symbol)

    def _handle_confirm(self) -> None:
        """使用者按 Confirm — 跳確認 dialog，然後呼叫 on_confirm(plan)"""
        plan = self.plan
        if not messagebox.askyesno(
            "Confirm Send Orders", rows.confirm_prompt(plan), parent=self.win,
        ):
            return
        self._close()
        self._log(rows.sending_log_line(plan))
        self.on_confirm(plan)
