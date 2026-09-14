"""TradesPopup — per-account Recent Trades in a Toplevel (Issue #20).

Each account column has a button that opens (or focuses) its own
TradesPopup. The popup hosts a TradesView and refreshes on open.
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable

from panel_task_cards import TradesView


class TradesPopup:
    """Toplevel wrapper around TradesView for one account."""

    def __init__(self, parent: tk.Misc, account_name: str,
                 theme: dict, fonts: dict,
                 fetch_trades: Callable[[str], list]):
        self.t = theme
        self.f = fonts
        self._parent = parent
        self._account = account_name
        self._fetch_trades = fetch_trades
        self._win: tk.Toplevel | None = None
        self._view: TradesView | None = None

    def toggle(self) -> None:
        if self._win is None or not self._win.winfo_exists():
            self._create()
            self._refresh()
        elif self._win.state() in ("iconic", "withdrawn"):
            self._win.deiconify()
            self._win.lift()
            self._win.focus_force()
            self._refresh()
        else:
            self._win.lift()
            self._win.focus_force()

    def _create(self) -> None:
        t, f = self.t, self.f
        win = tk.Toplevel(self._parent)
        win.title(f"Recent Trades — {self._account}")
        win.configure(bg=t["bg"])
        win.geometry("800x450")
        win.minsize(500, 250)

        ico_path = getattr(self._parent, "_ico_path", None)
        if ico_path and ico_path.exists():
            win.iconbitmap(str(ico_path))

        hdr = tk.Frame(win, bg=t["accent"], padx=10, pady=4)
        hdr.pack(fill="x")
        tk.Label(hdr, text=f"Recent Trades — {self._account}",
                 font=f["section"], bg=t["accent"],
                 fg=t["accent_hl"]).pack(side="left")
        tk.Button(hdr, text="Refresh", font=f["small"],
                  bg=t["bg3"], fg="white", relief="flat",
                  cursor="hand2", padx=6, pady=2,
                  command=self._refresh).pack(side="right")

        view = TradesView(parent=win, theme=t, fonts=f)
        view.pack(fill="both", expand=True)

        win.protocol("WM_DELETE_WINDOW", lambda: win.withdraw())
        self._win = win
        self._view = view

    def _refresh(self) -> None:
        import threading

        def _bg():
            trades = self._fetch_trades(self._account)
            if self._view is not None and self._win is not None:
                try:
                    self._win.after(0, lambda t=trades: self._view.update(t))
                except Exception:
                    pass

        threading.Thread(target=_bg, daemon=True).start()
