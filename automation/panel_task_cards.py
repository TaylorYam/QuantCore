"""
TaskCard / TradesView — middle-column Scheduled Tasks + left-bottom Recent Trades
(R3.3 extracted from control_panel.py).

Design:
  - Pure UI classes, no imports from control_panel.
  - Dependencies injected via constructor: theme (dict), fonts (dict),
    btn_factory (Panel's styled button builder), plus callbacks.
  - Each class owns its widget references — no pollution of Panel namespace.

Public API:
    card = TaskCard(
        parent, task_name, label, timing,
        theme, fonts, btn_factory,
        on_toggle=lambda tn, enabled: ...,
        on_trigger=lambda tn: ...,
        on_install=lambda tn: ...,
    )
    card.update(info)      # info from get_task_state()
    card.pack(...)         # TaskCard *is* the tk.Frame subclass — pack/grid directly

    tv = TradesView(parent, theme, fonts)
    tv.update(trades)      # list of trade dicts
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable


_REQUIRED_THEME_KEYS_CARD = {
    "bg2", "bg3", "text", "text_dim",
    "green", "red", "yellow", "accent_hl", "btn_bg",
}
_REQUIRED_FONTS_KEYS_CARD = {"small", "mono_sm"}


class TaskCard:
    """One compact card for a Windows Scheduled Task row."""

    def __init__(
        self,
        parent: tk.Misc,
        task_name: str,
        label: str,
        timing: str,
        theme: dict,
        fonts: dict,
        btn_factory: Callable,
        on_toggle: Callable[[str, bool], None],
        on_trigger: Callable[[str], None],
        on_install: Callable[[str], None],
    ):
        missing_t = _REQUIRED_THEME_KEYS_CARD - set(theme)
        missing_f = _REQUIRED_FONTS_KEYS_CARD - set(fonts)
        if missing_t:
            raise ValueError(f"theme missing keys: {sorted(missing_t)}")
        if missing_f:
            raise ValueError(f"fonts missing keys: {sorted(missing_f)}")

        self.task_name = task_name
        self.t = theme
        self.f = fonts
        self._btn = btn_factory
        self._on_toggle = on_toggle
        self._on_trigger = on_trigger
        self._on_install = on_install
        self._enabled = False  # tracked so toggle callback knows current state

        t, f = theme, fonts
        card = tk.Frame(parent, bg=t["bg2"], padx=8, pady=4)
        card.columnconfigure(1, weight=1)
        self.frame = card

        # Row 0: badge | name | status
        self.badge = tk.Label(card, text="*", font=f["small"],
                              bg=t["bg2"], fg=t["text_dim"])
        self.badge.grid(row=0, column=0, sticky="w")
        tk.Label(card, text=label, font=f["small"],
                 bg=t["bg2"], fg=t["text"]).grid(
                     row=0, column=1, sticky="w", padx=(2, 0))
        self.state_lbl = tk.Label(card, text="...", font=f["small"],
                                   bg=t["bg2"], fg=t["text_dim"])
        self.state_lbl.grid(row=0, column=2, columnspan=3, sticky="e")

        # Row 1: timing | last | toggle | run
        tk.Label(card, text=timing, font=f["mono_sm"],
                 bg=t["bg2"], fg=t["text_dim"]).grid(
                     row=1, column=0, columnspan=2, sticky="w")

        self.last_lbl = tk.Label(card, text="", font=f["mono_sm"],
                                  bg=t["bg2"], fg=t["text_dim"])
        self.last_lbl.grid(row=1, column=2, sticky="e", padx=(4, 4))

        self.toggle_btn = btn_factory(
            card, "Enable", self._handle_toggle_click, t["green"],
            width=6, padx=3, pady=1, font=f["mono_sm"],
        )
        self.toggle_btn.grid(row=1, column=3, sticky="e", padx=(0, 2))

        run_btn = btn_factory(
            card, "Run", lambda tn=task_name: on_trigger(tn),
            t["btn_bg"], padx=4, pady=1, font=f["mono_sm"],
        )
        run_btn.grid(row=1, column=4, sticky="e")

    # ── pack/grid passthrough ────────────────────────────────

    def pack(self, **kwargs) -> None:
        self.frame.pack(**kwargs)

    def grid(self, **kwargs) -> None:
        self.frame.grid(**kwargs)

    # ── Click handlers ───────────────────────────────────────

    def _handle_toggle_click(self) -> None:
        """Routes Enable/Disable clicks to on_toggle. Install mode is handled
        separately — toggle_btn is rebound to _handle_install_click in update()."""
        self._on_toggle(self.task_name, self._enabled)

    def _handle_install_click(self) -> None:
        self._on_install(self.task_name)

    # ── Update from task info ────────────────────────────────

    def update(self, info: dict) -> None:
        """Refresh state from get_task_state() dict."""
        t = self.t

        if not info.get("found"):
            self.badge.config(fg=t["yellow"])
            self.state_lbl.config(text="! Not Installed", fg=t["yellow"])
            self.toggle_btn.config(
                text="Install", bg=t["accent_hl"], fg="black",
                state="normal",
                command=self._handle_install_click,
            )
            self.last_lbl.config(text="")
            return

        enabled = info["enabled"]
        self._enabled = enabled
        self.badge.config(fg=t["green"] if enabled else t["red"])
        self.state_lbl.config(
            text="[OK] Active" if enabled else "[X] Disabled",
            fg=t["green"] if enabled else t["red"],
        )
        self.last_lbl.config(text=f"Last: {info.get('last_run', '—')}")
        self.toggle_btn.config(
            text="Disable" if enabled else "Enable",
            bg=t["red"] if enabled else t["green"],
            fg="white",
            command=self._handle_toggle_click,
        )


# ══════════════════════════════════════════════════════════════
# TradesView — recent trades list (left-bottom panel)
# ══════════════════════════════════════════════════════════════

_REQUIRED_THEME_KEYS_TRADES = {
    "bg2", "text", "text_dim", "green", "red", "yellow",
}
_REQUIRED_FONTS_KEYS_TRADES = {"mono_sm"}


class TradesView:
    """Read-only list of recent trades (Time/Symbol/Action/Qty/Strategy/Status)."""

    def __init__(self, parent: tk.Misc, theme: dict, fonts: dict):
        missing_t = _REQUIRED_THEME_KEYS_TRADES - set(theme)
        missing_f = _REQUIRED_FONTS_KEYS_TRADES - set(fonts)
        if missing_t:
            raise ValueError(f"theme missing keys: {sorted(missing_t)}")
        if missing_f:
            raise ValueError(f"fonts missing keys: {sorted(missing_f)}")

        self.t = theme
        self.f = fonts

        self.frame = tk.Frame(parent, bg=theme["bg2"], padx=8, pady=4)

        # Placeholder text until first update()
        self._placeholder = tk.Label(
            self.frame, text="Loading...",
            font=fonts["mono_sm"], bg=theme["bg2"], fg=theme["text_dim"],
        )
        self._placeholder.pack(anchor="w")

    def pack(self, **kwargs) -> None:
        self.frame.pack(**kwargs)

    def grid(self, **kwargs) -> None:
        self.frame.grid(**kwargs)

    def update(self, trades: list) -> None:
        """Rebuild the whole trades table from the latest list."""
        t, f = self.t, self.f

        # Clear old contents
        for w in self.frame.winfo_children():
            w.destroy()

        if not trades:
            tk.Label(
                self.frame, text="No recent trades",
                font=f["mono_sm"], bg=t["bg2"], fg=t["text_dim"],
            ).pack(anchor="w")
            return

        # Header row
        hdr = tk.Frame(self.frame, bg=t["bg2"])
        hdr.pack(fill="x")
        for txt, w in [
            ("Time", 14), ("Symbol", 7), ("Action", 5), ("Qty", 5),
            ("Strategy", 16), ("Status", 10),
        ]:
            tk.Label(hdr, text=txt, font=f["mono_sm"], bg=t["bg2"],
                     fg=t["text_dim"], width=w, anchor="w").pack(side="left")

        # At most 8 recent rows
        for tr in trades[:8]:
            row = tk.Frame(self.frame, bg=t["bg2"])
            row.pack(fill="x")
            dt_short = tr["datetime"][-8:] if len(tr["datetime"]) >= 8 else tr["datetime"]
            action_color = t["red"] if tr["action"] == "SELL" else t["green"]
            dry_tag = " [DRY]" if tr["dry_run"] else ""

            tk.Label(row, text=dt_short, font=f["mono_sm"], bg=t["bg2"],
                     fg=t["text_dim"], width=14, anchor="w").pack(side="left")
            tk.Label(row, text=tr["symbol"], font=f["mono_sm"], bg=t["bg2"],
                     fg=t["text"], width=7, anchor="w").pack(side="left")
            tk.Label(row, text=tr["action"], font=f["mono_sm"], bg=t["bg2"],
                     fg=action_color, width=5, anchor="w").pack(side="left")
            tk.Label(row, text=str(tr["qty"]), font=f["mono_sm"], bg=t["bg2"],
                     fg=t["text"], width=5, anchor="w").pack(side="left")
            tk.Label(row, text=tr["strategy"], font=f["mono_sm"], bg=t["bg2"],
                     fg=t["text_dim"], width=16, anchor="w").pack(side="left")
            tk.Label(
                row, text=f'{tr["status"]}{dry_tag}',
                font=f["mono_sm"], bg=t["bg2"],
                fg=t["yellow"] if "DRY" in tr["status"] else t["green"],
                width=14, anchor="w",
            ).pack(side="left")
