"""
TradesView — Recent Trades list (left-column panel)
(R3.3 extracted from control_panel.py).

Design:
  - Pure UI class, no imports from control_panel; row text and colors come
    from trades_display (shared with the new Panel, #150).
  - Dependencies injected via constructor: theme (dict), fonts (dict).
  - Owns its widget references — no pollution of Panel namespace.

Public API:
    tv = TradesView(parent, theme, fonts)
    tv.update(trades)      # list of trade dicts
"""

from __future__ import annotations

import tkinter as tk

from trades_display import EMPTY_TEXT, TRADE_COLUMNS, trade_row


_REQUIRED_THEME_KEYS_TRADES = {
    "bg2", "text", "text_dim", "green", "red", "yellow",
}
_REQUIRED_FONTS_KEYS_TRADES = {"mono_sm"}


class TradesView:
    """Scrollable list of recent trades (7-day rolling window)."""

    def __init__(self, parent: tk.Misc, theme: dict, fonts: dict):
        missing_t = _REQUIRED_THEME_KEYS_TRADES - set(theme)
        missing_f = _REQUIRED_FONTS_KEYS_TRADES - set(fonts)
        if missing_t:
            raise ValueError(f"theme missing keys: {sorted(missing_t)}")
        if missing_f:
            raise ValueError(f"fonts missing keys: {sorted(missing_f)}")

        self.t = theme
        self.f = fonts

        # Outer frame (parent manages placement)
        self.frame = tk.Frame(parent, bg=theme["bg2"])

        # Scrollable canvas + inner frame
        self._canvas = tk.Canvas(
            self.frame, bg=theme["bg2"], highlightthickness=0,
        )
        self._scrollbar = tk.Scrollbar(
            self.frame, orient="vertical", command=self._canvas.yview,
        )
        self._inner = tk.Frame(self._canvas, bg=theme["bg2"], padx=8, pady=4)
        self._inner.bind(
            "<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")),
        )
        self._canvas_win = self._canvas.create_window(
            (0, 0), window=self._inner, anchor="nw",
        )
        self._canvas.configure(yscrollcommand=self._scrollbar.set)

        self._scrollbar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        # Resize inner frame width to match canvas
        self._canvas.bind("<Configure>", self._on_canvas_resize)

        # Mouse wheel scrolling
        self._inner.bind("<Enter>", self._bind_mousewheel)
        self._inner.bind("<Leave>", self._unbind_mousewheel)

        # Placeholder
        tk.Label(
            self._inner, text="Loading...",
            font=fonts["mono_sm"], bg=theme["bg2"], fg=theme["text_dim"],
        ).pack(anchor="w")

    def _on_canvas_resize(self, event):
        self._canvas.itemconfig(self._canvas_win, width=event.width)

    def _bind_mousewheel(self, _):
        self._canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, _):
        self._canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event):
        self._canvas.yview_scroll(-1 * (event.delta // 120), "units")

    def pack(self, **kwargs) -> None:
        self.frame.pack(**kwargs)

    def grid(self, **kwargs) -> None:
        self.frame.grid(**kwargs)

    def update(self, trades: list) -> None:
        """Rebuild the whole trades table from the latest list."""
        t, f = self.t, self.f

        for w in self._inner.winfo_children():
            w.destroy()

        if not trades:
            tk.Label(
                self._inner, text=EMPTY_TEXT,
                font=f["mono_sm"], bg=t["bg2"], fg=t["text_dim"],
            ).pack(anchor="w")
            return

        # Header row
        hdr = tk.Frame(self._inner, bg=t["bg2"])
        hdr.pack(fill="x")
        for txt, w in TRADE_COLUMNS:
            tk.Label(hdr, text=txt, font=f["mono_sm"], bg=t["bg2"],
                     fg=t["text_dim"], width=w, anchor="w").pack(side="left")

        for tr in trades:
            row = tk.Frame(self._inner, bg=t["bg2"])
            row.pack(fill="x")
            r = trade_row(tr)
            for text, fg, w in (
                (r.time, "text_dim", 14), (r.symbol, "text", 7),
                (r.action, r.action_color, 5), (r.qty, "text", 5),
                (r.price, "text", 8), (r.strategy, "text_dim", 10),
                (r.status, r.status_color, 14),
            ):
                tk.Label(row, text=text, font=f["mono_sm"], bg=t["bg2"],
                         fg=t[fg], width=w, anchor="w").pack(side="left")
