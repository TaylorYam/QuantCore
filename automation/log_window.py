"""LogWindow — Output Log in a Toplevel popup (Issue #20).

Replaces the inline ScrolledText. Panel holds a single instance;
the button toggles visibility (show/focus if hidden, deiconify if
minimised). _log() writes to the popup's text widget regardless of
visibility — the log accumulates even while the window is closed.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import scrolledtext


class LogWindow:
    """Popup window hosting the Output Log."""

    def __init__(self, parent: tk.Misc, theme: dict, fonts: dict):
        self.t = theme
        self.f = fonts
        self._parent = parent

        self._win: tk.Toplevel | None = None
        self._text: scrolledtext.ScrolledText | None = None
        self._buffer: list[str] = []

    def toggle(self) -> None:
        if self._win is None or not self._win.winfo_exists():
            self._create()
        elif self._win.state() == "iconic":
            self._win.deiconify()
            self._win.lift()
            self._win.focus_force()
        elif self._win.state() == "withdrawn":
            self._win.deiconify()
            self._win.lift()
            self._win.focus_force()
        else:
            self._win.lift()
            self._win.focus_force()

    def log(self, msg: str) -> None:
        self._buffer.append(msg)
        if self._text is not None and self._text.winfo_exists():
            self._text.config(state="normal")
            self._text.insert("end", msg + "\n")
            self._text.see("end")
            self._text.config(state="disabled")

    def clear(self) -> None:
        self._buffer.clear()
        if self._text is not None and self._text.winfo_exists():
            self._text.config(state="normal")
            self._text.delete("1.0", "end")
            self._text.config(state="disabled")

    def _create(self) -> None:
        t, f = self.t, self.f
        win = tk.Toplevel(self._parent)
        win.title("QuantCore — Output Log")
        win.configure(bg=t["bg"])
        win.geometry("900x500")
        win.minsize(500, 300)

        ico_path = getattr(self._parent, "_ico_path", None)
        if ico_path and ico_path.exists():
            win.iconbitmap(str(ico_path))

        hdr = tk.Frame(win, bg=t["accent"], padx=10, pady=4)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Output Log", font=f["section"],
                 bg=t["accent"], fg=t["accent_hl"]).pack(side="left")
        tk.Button(hdr, text="Clear", font=f["small"],
                  bg=t["bg3"], fg="white", relief="flat",
                  cursor="hand2", padx=6, pady=2,
                  command=self.clear).pack(side="right")

        text = scrolledtext.ScrolledText(
            win, font=f["mono"],
            bg="#0a0f1a", fg="#d1d5db", insertbackground=t["text"],
            relief="flat", padx=8, pady=6, wrap="word",
        )
        text.pack(fill="both", expand=True)
        text.config(state="disabled")

        if self._buffer:
            text.config(state="normal")
            text.insert("end", "\n".join(self._buffer) + "\n")
            text.see("end")
            text.config(state="disabled")

        win.protocol("WM_DELETE_WINDOW", lambda: win.withdraw())
        self._win = win
        self._text = text
