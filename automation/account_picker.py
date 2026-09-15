"""AccountPicker — 操作列按鈕先選帳戶的小視窗（Issue #35）。

Issue #20 改成三帳戶並列後，Panel 沒有「目前選中的帳戶」。操作列上
只作用於單一帳戶的按鈕（Sync Pos）點下去先開這個視窗，選好才呼叫
on_pick(name)；Cancel、Esc、關閉視窗都不呼叫。

Pure UI：不碰 IB、不開 thread，選了之後做什麼由 caller 決定。
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable


class AccountPicker:
    """Modal dialog: one button per account + Cancel."""

    def __init__(self, parent: tk.Misc, title: str, accounts: list[str],
                 theme: dict, fonts: dict, btn_factory: Callable,
                 on_pick: Callable[[str], None]):
        self.t = theme
        self.f = fonts
        self._parent = parent
        self._title = title
        self._accounts = list(accounts)
        self._btn = btn_factory
        self._on_pick = on_pick
        self.win: tk.Toplevel | None = None

    def show(self) -> None:
        t, f = self.t, self.f
        win = tk.Toplevel(self._parent)
        self.win = win
        win.title(self._title)
        win.configure(bg=t["bg"])
        win.resizable(False, False)
        win.transient(self._parent)

        ico_path = getattr(self._parent, "_ico_path", None)
        if ico_path and ico_path.exists():
            win.iconbitmap(str(ico_path))

        hdr = tk.Frame(win, bg=t["accent"], padx=12, pady=6)
        hdr.pack(fill="x")
        tk.Label(hdr, text=self._title, font=f["section"],
                 bg=t["accent"], fg=t["accent_hl"]).pack(side="left")

        accounts_row = tk.Frame(win, bg=t["bg"], padx=12, pady=12)
        accounts_row.pack(fill="x")
        for name in self._accounts:
            self._btn(accounts_row, name, lambda n=name: self._pick(n),
                      t["blue"], width=10).pack(side="left", padx=4)

        footer = tk.Frame(win, bg=t["bg"], padx=12, pady=8)
        footer.pack(fill="x")
        self._btn(footer, "Cancel", self._close,
                  t["btn_bg"], width=10).pack(side="right", padx=4)

        win.bind("<Escape>", lambda e: self._close())

        # 置中在 Panel 上方（跟 messagebox 一樣），不要跑到螢幕角落
        win.update_idletasks()
        x = self._parent.winfo_rootx() + (self._parent.winfo_width() - win.winfo_reqwidth()) // 2
        y = self._parent.winfo_rooty() + (self._parent.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")

        win.grab_set()
        win.focus_set()

    def _pick(self, name: str) -> None:
        self._close()
        self._on_pick(name)

    def _close(self) -> None:
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
