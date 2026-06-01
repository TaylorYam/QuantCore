"""
SyncPreviewWindow — Sync Position 預覽對話框（S10/R3.1 從 control_panel 抽出）

職責：
  1. 顯示理論持倉（Strategy Target）
  2. 顯示實際持倉（Current Holdings）
  3. 顯示交易清單（Required Trades）與總計
  4. 讓使用者按 Cancel / Confirm
  5. Confirm 後呼叫傳入的 on_confirm callback（帶 SyncPlan 當參數）

Design:
  - Pure UI — 不做 IB 查詢、不管理背景 thread、不寫 Excel/Telegram
  - 所有 Panel-specific 的依賴（theme colors、log 函式、btn factory）
    以 constructor 參數明確傳入，沒有 import control_panel
  - Modal（grab_set），會阻擋 parent 互動直到關閉

Usage:
    theme = {"bg": "#0b1120", "bg2": "#131d35", ...}
    window = SyncPreviewWindow(
        parent=self,                 # tk.Tk or Toplevel
        plan=sync_plan,              # sync_position.SyncPlan
        theme=theme,                 # color dict
        log=self._log,               # Panel's output log callback
        btn_factory=self._btn,       # Panel's styled button factory
        on_confirm=lambda p: self._execute_sync(p),
    )
    window.show()
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from typing import Callable

from sync_position import SyncPlan
from ib_fetcher import SR_ETFS, SKEWLS_INVERSE_TICKERS


# Expected theme keys (documented for callers)
_THEME_KEYS = {
    "bg", "bg2", "bg3", "accent", "accent_hl",
    "text", "text_dim", "blue", "green", "red", "btn_bg",
}


class SyncPreviewWindow:
    """Modal review dialog for a Sync Position plan."""

    def __init__(
        self,
        parent: tk.Misc,
        plan: SyncPlan,
        theme: dict,
        log: Callable[[str], None],
        btn_factory: Callable,
        on_confirm: Callable[[SyncPlan], None],
    ):
        missing = _THEME_KEYS - set(theme)
        if missing:
            raise ValueError(f"theme missing keys: {sorted(missing)}")
        self.parent = parent
        self.plan = plan
        self.t = theme
        self._log = log
        self._btn = btn_factory
        self.on_confirm = on_confirm
        self.win: tk.Toplevel | None = None

    # ── Public API ─────────────────────────────────────────────

    def show(self) -> None:
        """Build and display the modal window. Returns immediately after
        the dialog is grabbed; dialog lifecycle is driven by button clicks."""
        # Write log summary first (goes to parent Panel's Output Log)
        self._log_summary()

        # Build the Toplevel
        self._build_window()

    # ── Log summary（寫到 parent Panel 的 Output Log）────────────

    def _log_summary(self) -> None:
        plan = self.plan
        ctx = plan.ctx
        prices = plan.prices
        targets = plan.targets

        self._log(f"{'─' * 40}")
        self._log(f"  Strategy Target (理論持倉)")

        def _strat_sh(alloc: float, w: float, px: float) -> int:
            """Strategy-only target shares (pre-combine). Avoids double-count
            display when the same ticker appears in multiple strategies."""
            return int(round(plan.nav * alloc * w / px)) if px > 0 else 0

        def _overlap_note(sym: str, strat_sh: int) -> str:
            combined = targets.get(sym, {}).get('shares', 0)
            if combined == strat_sh:
                return ""
            return f"  [combined w/ overlap: {combined:+d}sh]"

        if plan.sr_wgt:
            self._log(f"  SR ({ctx.alloc_sr:.0%} of NAV):")
            for sym, w in sorted(plan.sr_wgt.items(), key=lambda x: -x[1]):
                px = prices.get(sym, 0)
                t_sh = _strat_sh(ctx.alloc_sr, w, px)
                self._log(f"    {sym:<6s} {w*100:5.1f}%  → {t_sh:4d}sh @ ${px:.2f}"
                          f"{_overlap_note(sym, t_sh)}")
        if plan.sp500_wgt:
            self._log(f"  SP500 ({ctx.alloc_sp500:.0%} of NAV):")
            for sym, w in sorted(plan.sp500_wgt.items(), key=lambda x: -x[1]):
                px = prices.get(sym, 0)
                t_sh = _strat_sh(ctx.alloc_sp500, w, px)
                self._log(f"    {sym:<6s} {w*100:5.1f}%  → {t_sh:4d}sh @ ${px:.2f}"
                          f"{_overlap_note(sym, t_sh)}")
        if plan.skewls_wgt:
            self._log(f"  SkewLS ({ctx.alloc_skewls:.0%} of NAV):")
            for sym, w in sorted(plan.skewls_wgt.items(), key=lambda x: -abs(x[1])):
                px = prices.get(sym, 0)
                t_sh = _strat_sh(ctx.alloc_skewls, w, px)
                direction = "SHORT" if w < 0 else "LONG "
                self._log(f"    {direction} {sym:<6s} {w*100:+5.1f}%  → {t_sh:+4d}sh @ ${px:.2f}"
                          f"{_overlap_note(sym, t_sh)}")

        strategy_syms = [s for s in plan.current_pos
                         if s not in plan.current_other_holdings]
        self._log(f"  Current Holdings (實際持倉):")
        for sym in sorted(strategy_syms):
            sh = int(plan.current_pos[sym])
            px = prices.get(sym, 0)
            in_t = "" if sym in targets else " ← NOT in target"
            self._log(f"    {sym:<6s} {sh:4d}sh @ ${px:.2f}{in_t}")

        self._log(f"  Required Trades: {len(plan.orders)} 筆")
        for o in plan.orders:
            self._log(f"    {o.action:<4s} {o.qty:4d}sh {o.symbol:<6s} "
                      f"({o.current}→{o.target})  ~${o.qty * o.price:,.0f}  "
                      f"[{o.strategy}]")
        if not plan.orders:
            self._log(f"    Already on target")
        self._log(f"{'─' * 40}")

    # ── Window building ────────────────────────────────────────

    def _build_window(self) -> None:
        plan = self.plan
        ctx = plan.ctx
        t = self.t

        win = tk.Toplevel(self.parent)
        self.win = win
        win.title(f"Sync Position — {ctx.name}")
        win.configure(bg=t["bg"])
        win.geometry("750x620")
        win.resizable(True, True)
        win.transient(self.parent)
        win.grab_set()

        # Title header
        hdr = tk.Frame(win, bg=t["accent"], padx=12, pady=8)
        hdr.pack(fill="x")
        tk.Label(hdr, text=f"Sync Position — {ctx.name}",
                 font=("Segoe UI", 13, "bold"), bg=t["accent"], fg=t["text"]).pack(anchor="w")
        hdr_text = f"NAV: ${plan.nav:,.0f}   |   SR: {plan.sr_date}   |   SP500: {plan.sp500_date}"
        if ctx.alloc_skewls > 0:
            hdr_text += f"   |   SkewLS: {plan.skewls_date}"
        tk.Label(hdr, text=hdr_text,
                 font=("Segoe UI", 9), bg=t["accent"], fg=t["text_dim"]).pack(anchor="w")

        # Scrollable main region
        main_canvas = tk.Canvas(win, bg=t["bg"], highlightthickness=0)
        main_sb = tk.Scrollbar(win, orient="vertical", command=main_canvas.yview)
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

        # Trade list + buttons
        if not plan.orders:
            self._build_no_trades_footer(main_frame)
        else:
            self._build_section_trades(main_frame)
            self._build_buttons(main_frame)

    # ── Section 1: Strategy Target ─────────────────────────────

    def _build_section_targets(self, parent: tk.Frame) -> None:
        plan = self.plan
        t = self.t
        alloc_sr = plan.ctx.alloc_sr
        alloc_sp500 = plan.ctx.alloc_sp500
        alloc_skewls = plan.ctx.alloc_skewls
        prices = plan.prices
        targets = plan.targets

        sec = tk.Frame(parent, bg=t["bg"], padx=12, pady=6)
        sec.pack(fill="x")
        tk.Label(sec, text="Strategy Target (理論持倉)",
                 font=("Segoe UI", 10, "bold"),
                 bg=t["bg"], fg=t["accent_hl"]).pack(anchor="w")

        def _strat_sh(alloc: float, w: float, px: float) -> int:
            """Strategy-only target shares (pre-combine). Avoids double-count
            display when the same ticker appears in multiple strategies."""
            return int(round(plan.nav * alloc * w / px)) if px > 0 else 0

        def _overlap_note(sym: str, strat_sh: int) -> str:
            combined = targets.get(sym, {}).get('shares', 0)
            if combined == strat_sh:
                return ""
            return f"  [combined: {combined:+d}sh]"

        # SR targets
        if plan.sr_wgt:
            sr_frame = tk.Frame(sec, bg=t["bg2"], padx=8, pady=4)
            sr_frame.pack(fill="x", pady=(4, 2))
            tk.Label(sr_frame, text=f"SR ({alloc_sr:.0%} of NAV)",
                     font=("Consolas", 9, "bold"),
                     bg=t["bg2"], fg=t["blue"]).pack(anchor="w")
            for sym, w in sorted(plan.sr_wgt.items(), key=lambda x: -x[1]):
                px = prices.get(sym, 0)
                t_shares = _strat_sh(alloc_sr, w, px)
                t_val = t_shares * px
                tk.Label(
                    sr_frame,
                    text=f"  {sym:<6s}  {w*100:5.1f}%  →  {t_shares:4d}sh  @ ${px:.2f}  = ${t_val:,.0f}"
                         f"{_overlap_note(sym, t_shares)}",
                    font=("Consolas", 9), bg=t["bg2"], fg=t["text"],
                ).pack(anchor="w")

        # SP500 targets
        if plan.sp500_wgt:
            sp_frame = tk.Frame(sec, bg=t["bg2"], padx=8, pady=4)
            sp_frame.pack(fill="x", pady=(2, 2))
            tk.Label(sp_frame, text=f"SP500 Momentum ({alloc_sp500:.0%} of NAV)",
                     font=("Consolas", 9, "bold"),
                     bg=t["bg2"], fg=t["green"]).pack(anchor="w")
            for sym, w in sorted(plan.sp500_wgt.items(), key=lambda x: -x[1]):
                px = prices.get(sym, 0)
                t_shares = _strat_sh(alloc_sp500, w, px)
                t_val = t_shares * px
                tk.Label(
                    sp_frame,
                    text=f"  {sym:<6s}  {w*100:5.1f}%  →  {t_shares:4d}sh  @ ${px:.2f}  = ${t_val:,.0f}"
                         f"{_overlap_note(sym, t_shares)}",
                    font=("Consolas", 9), bg=t["bg2"], fg=t["text"],
                ).pack(anchor="w")

        # SkewLS targets (signed: 含 long inverse ETFs + XLC short)
        if plan.skewls_wgt:
            sk_frame = tk.Frame(sec, bg=t["bg2"], padx=8, pady=4)
            sk_frame.pack(fill="x", pady=(2, 2))
            tk.Label(sk_frame, text=f"Skew LS ({alloc_skewls:.0%} of NAV)",
                     font=("Consolas", 9, "bold"),
                     bg=t["bg2"], fg=t["red"]).pack(anchor="w")
            for sym, w in sorted(plan.skewls_wgt.items(), key=lambda x: -abs(x[1])):
                px = prices.get(sym, 0)
                t_shares = _strat_sh(alloc_skewls, w, px)
                t_val = t_shares * px
                direction = "SHORT" if w < 0 else "LONG "
                tk.Label(
                    sk_frame,
                    text=f"  {direction} {sym:<6s}  {w*100:+5.1f}%  →  {t_shares:+4d}sh  @ ${px:.2f}  = ${t_val:+,.0f}"
                         f"{_overlap_note(sym, t_shares)}",
                    font=("Consolas", 9), bg=t["bg2"], fg=t["text"],
                ).pack(anchor="w")

        if not plan.sr_wgt and not plan.sp500_wgt and not plan.skewls_wgt:
            tk.Label(sec, text="  (no target positions — all cash)",
                     font=("Consolas", 9),
                     bg=t["bg"], fg=t["text_dim"]).pack(anchor="w")

    # ── Section 2: Current Holdings ────────────────────────────

    def _build_section_current(self, parent: tk.Frame) -> None:
        plan = self.plan
        t = self.t
        prices = plan.prices
        targets = plan.targets
        current_pos = plan.current_pos
        current_other_holdings = plan.current_other_holdings

        sec = tk.Frame(parent, bg=t["bg"], padx=12, pady=6)
        sec.pack(fill="x")
        tk.Label(sec, text="Current Holdings (實際持倉)",
                 font=("Segoe UI", 10, "bold"),
                 bg=t["bg"], fg=t["accent_hl"]).pack(anchor="w")

        # 策略相關持倉
        strategy_syms = [s for s in current_pos if s not in current_other_holdings]
        if strategy_syms:
            cur_frame = tk.Frame(sec, bg=t["bg2"], padx=8, pady=4)
            cur_frame.pack(fill="x", pady=(4, 2))
            for sym in sorted(strategy_syms):
                shares = int(current_pos[sym])
                px = prices.get(sym, 0)
                val = shares * px
                # 三策略分類：inverse ETF 或 sector ETF 負部位 → SkewLS
                if sym in SKEWLS_INVERSE_TICKERS or (sym in SR_ETFS and shares < 0):
                    tag, tag_color = "SkewLS", t["red"]
                elif sym in SR_ETFS:
                    tag, tag_color = "SR", t["blue"]
                else:
                    tag, tag_color = "SP500", t["green"]
                in_target = sym in targets
                mark = "" if in_target else "  ← not in target"
                mark_color = t["text"] if in_target else t["red"]
                lbl = tk.Frame(cur_frame, bg=t["bg2"])
                lbl.pack(fill="x")
                tk.Label(lbl,
                         text=f"  {sym:<6s}  {shares:4d}sh  @ ${px:.2f}  = ${val:,.0f}",
                         font=("Consolas", 9), bg=t["bg2"], fg=t["text"]).pack(side="left")
                tk.Label(lbl, text=f"  [{tag}]",
                         font=("Consolas", 9), bg=t["bg2"], fg=tag_color).pack(side="left")
                if mark:
                    tk.Label(lbl, text=mark,
                             font=("Consolas", 9), bg=t["bg2"], fg=mark_color).pack(side="left")
        else:
            tk.Label(sec, text="  (no strategy holdings)",
                     font=("Consolas", 9),
                     bg=t["bg"], fg=t["text_dim"]).pack(anchor="w")

        # 手動持倉
        if current_other_holdings:
            oth_frame = tk.Frame(sec, bg=t["bg2"], padx=8, pady=4)
            oth_frame.pack(fill="x", pady=(2, 2))
            tk.Label(oth_frame, text="Other (excluded from sync)",
                     font=("Consolas", 9, "bold"),
                     bg=t["bg2"], fg=t["text_dim"]).pack(anchor="w")
            for sym in sorted(current_other_holdings):
                shares = int(current_pos[sym])
                px = prices.get(sym, 0)
                val = shares * px
                tk.Label(
                    oth_frame,
                    text=f"  {sym:<6s}  {shares:4d}sh  @ ${px:.2f}  = ${val:,.0f}",
                    font=("Consolas", 9), bg=t["bg2"], fg=t["text_dim"],
                ).pack(anchor="w")

    # ── Section 3: Trade table ─────────────────────────────────

    def _build_section_trades(self, parent: tk.Frame) -> None:
        plan = self.plan
        t = self.t

        sep = tk.Frame(parent, bg=t["accent_hl"], height=1)
        sep.pack(fill="x", padx=12, pady=6)

        sec = tk.Frame(parent, bg=t["bg"], padx=12, pady=4)
        sec.pack(fill="x")
        tk.Label(sec,
                 text=f"Required Trades (需要交易: {len(plan.orders)} 筆)",
                 font=("Segoe UI", 10, "bold"),
                 bg=t["bg"], fg=t["accent_hl"]).pack(anchor="w")

        # 表頭
        cols = ("Action", "Symbol", "Qty", "Current", "Target",
                "Est.Price", "Est.Value", "Strategy")
        col_widths = (7, 7, 6, 8, 8, 10, 11, 8)
        thdr = tk.Frame(sec, bg=t["bg3"], padx=4, pady=3)
        thdr.pack(fill="x", pady=(4, 0))
        for c, w in zip(cols, col_widths):
            tk.Label(thdr, text=c, font=("Consolas", 9, "bold"),
                     bg=t["bg3"], fg=t["accent_hl"], width=w, anchor="w").pack(side="left")

        # 行：SELL 先，BUY 後
        total_sell = 0.0
        total_buy = 0.0
        sells = [o for o in plan.orders if o.action == 'SELL']
        buys = [o for o in plan.orders if o.action == 'BUY']

        for o in sells + buys:
            is_sell = o.action == 'SELL'
            color = t["red"] if is_sell else t["green"]
            est_val = o.qty * o.price
            if is_sell:
                total_sell += est_val
            else:
                total_buy += est_val

            row = tk.Frame(sec, bg=t["bg"], padx=4, pady=1)
            row.pack(fill="x")
            vals = (
                o.action, o.symbol, str(o.qty),
                str(o.current), str(o.target),
                f"${o.price:.2f}", f"${est_val:,.0f}",
                o.strategy,
            )
            for v, w in zip(vals, col_widths):
                fg = color if v in ('BUY', 'SELL') else t["text"]
                tk.Label(row, text=v, font=("Consolas", 9),
                         bg=t["bg"], fg=fg, width=w, anchor="w").pack(side="left")

        # 總計
        summary = tk.Frame(parent, bg=t["bg3"], padx=12, pady=6)
        summary.pack(fill="x", padx=12, pady=(6, 0))
        net = total_buy - total_sell
        net_sign = "+" if net >= 0 else ""
        tk.Label(
            summary,
            text=f"SELL: ${total_sell:,.0f}   |   BUY: ${total_buy:,.0f}   |   Net: {net_sign}${net:,.0f}",
            font=("Consolas", 10, "bold"),
            bg=t["bg3"], fg=t["text"],
        ).pack()

    # ── Button rows ────────────────────────────────────────────

    def _build_no_trades_footer(self, parent: tk.Frame) -> None:
        """When plan.orders is empty — show 'already on target' + single OK button."""
        t = self.t
        sec = tk.Frame(parent, bg=t["bg"], padx=12, pady=4)
        sec.pack(fill="x")
        tk.Label(sec, text="Required Trades (需要交易: 0 筆)",
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
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass

    def _handle_confirm(self) -> None:
        """使用者按 Confirm — 跳確認 dialog，然後呼叫 on_confirm(plan)"""
        plan = self.plan
        if not messagebox.askyesno(
            "Confirm Send Orders",
            f"確定送出 {len(plan.orders)} 筆限價單到 [{plan.ctx.name}]？\n"
            f"(LMT ±0.3% buffer, GTC, outside RTH)",
            parent=self.win,
        ):
            return
        self._close()
        self._log(f"[>] Sending {len(plan.orders)} LMT orders for [{plan.ctx.name}]...")
        self.on_confirm(plan)
