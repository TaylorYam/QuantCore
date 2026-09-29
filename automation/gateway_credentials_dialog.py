"""Gateway 帳密輸入框（Issue #121）。

「啟動 Gateway」發現某帳戶還沒存帳密、或使用者按「Gateway 帳密」更新時跳出。
密碼欄以 • 遮住。OK 呼叫 on_done(Credentials)；Cancel、Esc、關閉視窗呼叫
on_done(None)。

Pure UI：不碰認證管理員、不開 thread，存不存由 caller 決定。
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable

from gateway_start import Credentials


class GatewayCredentialsDialog:
    """Modal dialog: username + masked password + OK/Cancel."""

    def __init__(self, parent: tk.Misc, account: str, theme: dict, fonts: dict,
                 btn_factory: Callable, on_done: Callable[[Credentials | None], None],
                 username: str = ""):
        self.t = theme
        self.f = fonts
        self._parent = parent
        self._account = account
        self._btn = btn_factory
        self._on_done = on_done
        self._initial_username = username
        self._done = False
        self.win: tk.Toplevel | None = None

    def show(self) -> None:
        t, f = self.t, self.f
        win = tk.Toplevel(self._parent)
        self.win = win
        win.title(f"Gateway 帳密 — {self._account}")
        win.configure(bg=t["bg"])
        win.resizable(False, False)
        win.transient(self._parent)
        win.protocol("WM_DELETE_WINDOW", lambda: self._finish(None))

        ico_path = getattr(self._parent, "_ico_path", None)
        if ico_path and ico_path.exists():
            win.iconbitmap(str(ico_path))

        hdr = tk.Frame(win, bg=t["accent"], padx=12, pady=6)
        hdr.pack(fill="x")
        tk.Label(hdr, text=f"{self._account} — IB Gateway 帳密", font=f["section"],
                 bg=t["accent"], fg=t["accent_hl"]).pack(side="left")

        form = tk.Frame(win, bg=t["bg"], padx=12, pady=12)
        form.pack(fill="x")
        tk.Label(form, text="帳號", font=f["small"], bg=t["bg"], fg=t["text"]
                 ).grid(row=0, column=0, sticky="w", pady=3)
        self._user = tk.Entry(form, width=28, font=f["mono"])
        self._user.insert(0, self._initial_username)
        self._user.grid(row=0, column=1, padx=(8, 0), pady=3)
        tk.Label(form, text="密碼", font=f["small"], bg=t["bg"], fg=t["text"]
                 ).grid(row=1, column=0, sticky="w", pady=3)
        self._pw = tk.Entry(form, width=28, font=f["mono"], show="•")
        self._pw.grid(row=1, column=1, padx=(8, 0), pady=3)
        tk.Label(form, text="存在 Windows 認證管理員，不會寫進任何檔案。",
                 font=f["small"], bg=t["bg"], fg=t["text_dim"]
                 ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        footer = tk.Frame(win, bg=t["bg"], padx=12, pady=8)
        footer.pack(fill="x")
        self._btn(footer, "Cancel", lambda: self._finish(None),
                  t["btn_bg"], width=10).pack(side="right", padx=4)
        self._btn(footer, "OK", self._ok, t["blue"], width=10).pack(side="right", padx=4)

        win.bind("<Escape>", lambda e: self._finish(None))
        win.bind("<Return>", lambda e: self._ok())

        # 置中在 Panel 上方（跟 AccountPicker 一樣）
        win.update_idletasks()
        x = self._parent.winfo_rootx() + (self._parent.winfo_width() - win.winfo_reqwidth()) // 2
        y = self._parent.winfo_rooty() + (self._parent.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")

        win.grab_set()
        (self._pw if self._initial_username else self._user).focus_set()

    def _ok(self) -> None:
        username = self._user.get().strip()
        password = self._pw.get()
        if not username or not password:
            (self._user if not username else self._pw).focus_set()
            return
        self._finish(Credentials(username, password))

    def _finish(self, creds: Credentials | None) -> None:
        if self._done:
            return
        self._done = True
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
        self._on_done(creds)
