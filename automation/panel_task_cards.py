"""
TradesView — Recent Trades list (left-column panel)
(R3.3 extracted from control_panel.py).

Design:
  - Pure UI class, no imports from control_panel.
  - Dependencies injected via constructor: theme (dict), fonts (dict).
  - Owns its widget references — no pollution of Panel namespace.

Public API:
    tv = TradesView(parent, theme, fonts)
    tv.update(trades)      # list of trade dicts
"""

from __future__ import annotations

import tkinter as tk


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
                self._inner, text="No recent trades (7 days)",
                font=f["mono_sm"], bg=t["bg2"], fg=t["text_dim"],
            ).pack(anchor="w")
            return

        # Header row
        hdr = tk.Frame(self._inner, bg=t["bg2"])
        hdr.pack(fill="x")
        for txt, w in [
            ("Time", 14), ("Symbol", 7), ("Action", 5), ("Qty", 5),
            ("Price", 8), ("Strategy", 10), ("Status", 10),
        ]:
            tk.Label(hdr, text=txt, font=f["mono_sm"], bg=t["bg2"],
                     fg=t["text_dim"], width=w, anchor="w").pack(side="left")

        for tr in trades:
            row = tk.Frame(self._inner, bg=t["bg2"])
            row.pack(fill="x")
            dt_short = tr["datetime"][-8:] if len(tr["datetime"]) >= 8 else tr["datetime"]
            action_color = t["red"] if tr["action"] == "SELL" else t["green"]
            dry_tag = " [DRY]" if tr["dry_run"] else ""

            fill_px = tr.get("fill_price", 0)
            px_text = f"${fill_px:.2f}" if fill_px > 0 else "—"

            tk.Label(row, text=dt_short, font=f["mono_sm"], bg=t["bg2"],
                     fg=t["text_dim"], width=14, anchor="w").pack(side="left")
            tk.Label(row, text=tr["symbol"], font=f["mono_sm"], bg=t["bg2"],
                     fg=t["text"], width=7, anchor="w").pack(side="left")
            tk.Label(row, text=tr["action"], font=f["mono_sm"], bg=t["bg2"],
                     fg=action_color, width=5, anchor="w").pack(side="left")
            tk.Label(row, text=str(tr["qty"]), font=f["mono_sm"], bg=t["bg2"],
                     fg=t["text"], width=5, anchor="w").pack(side="left")
            tk.Label(row, text=px_text, font=f["mono_sm"], bg=t["bg2"],
                     fg=t["text"], width=8, anchor="w").pack(side="left")
            tk.Label(row, text=tr["strategy"], font=f["mono_sm"], bg=t["bg2"],
                     fg=t["text_dim"], width=10, anchor="w").pack(side="left")
            tk.Label(
                row, text=f'{tr["status"]}{dry_tag}',
                font=f["mono_sm"], bg=t["bg2"],
                fg=t["yellow"] if "DRY" in tr["status"] else t["green"],
                width=14, anchor="w",
            ).pack(side="left")
