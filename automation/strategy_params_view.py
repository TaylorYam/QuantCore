"""
StrategyParamsView — Strategy Parameters panel
(R3.4 extracted from control_panel.py, Trading Settings removed in Issue #13).

Builds one frame:
  Strategy Parameters: ETF slider, Stocks slider, SkewLS slider, Exposure summary, Save button

Design:
  - Pure UI class — no config.json reads, no account_utils writes.
  - Panel owns all I/O; view just renders + emits callbacks.
  - on_save is called with no arguments. Panel reads
    current slider values via `get_alloc()` when it needs them.

Public API:
    view = StrategyParamsView(
        parent_params,
        theme, fonts, btn_factory,
        on_save=...,
    )
    view.update_cfg(cfg_dict)   # refresh from cfg (alloc_sr, alloc_sp500, alloc_skewls)
    sr, sp, sk = view.get_alloc()   # read slider values (3-tuple)
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable, Tuple

from strategy_params_display import allocations_from_cfg, strategy_params


_REQUIRED_THEME_KEYS = {
    "bg2", "bg3", "text", "text_dim",
    "blue", "green", "red", "orange", "accent_hl",
}
_REQUIRED_FONTS_KEYS = {"small", "label", "section", "badge"}


class StrategyParamsView:
    """Strategy Parameters widgets."""

    def __init__(
        self,
        parent_params: tk.Misc,
        theme: dict,
        fonts: dict,
        btn_factory: Callable,
        on_save: Callable[[], None],
    ):
        missing_t = _REQUIRED_THEME_KEYS - set(theme)
        missing_f = _REQUIRED_FONTS_KEYS - set(fonts)
        if missing_t:
            raise ValueError(f"theme missing keys: {sorted(missing_t)}")
        if missing_f:
            raise ValueError(f"fonts missing keys: {sorted(missing_f)}")

        self.t = theme
        self.f = fonts
        self._btn = btn_factory
        self._on_save = on_save

        self._build_strategy_params(parent_params)

    # ── Build: Strategy Parameters ───────────────────────────

    def _build_strategy_params(self, parent: tk.Misc) -> None:
        t, f = self.t, self.f
        frame = tk.Frame(parent, bg=t["bg2"], padx=8, pady=8)
        frame.pack(fill="x")
        self.params_frame = frame

        tk.Label(
            frame, text="Strategy Parameters", font=f["section"],
            bg=t["bg2"], fg=t["accent_hl"],
        ).pack(anchor="w", pady=(0, 6))

        # ETF allocation
        row_sr = tk.Frame(frame, bg=t["bg2"])
        row_sr.pack(fill="x", pady=2)
        tk.Label(
            row_sr, text="SR", font=f["label"],
            bg=t["bg2"], fg=t["blue"], width=8, anchor="w",
        ).pack(side="left")
        self.sr_scale = tk.Scale(
            row_sr, from_=0.0, to=1.0, resolution=0.05,
            orient="horizontal", length=100, showvalue=False,
            width=8, sliderlength=16, borderwidth=1, highlightthickness=0,
            bg=t["bg2"], fg=t["text"], troughcolor=t["bg3"],
            highlightbackground=t["bg2"], font=f["small"],
            command=self._on_alloc_change,
        )
        self.sr_scale.pack(side="left", fill="x", expand=True, padx=(8, 12))
        self.sr_pct_lbl = tk.Label(
            row_sr, text="80%", font=f["badge"],
            bg=t["bg2"], fg=t["blue"], width=5, anchor="e",
        )
        self.sr_pct_lbl.pack(side="left")

        # Stocks allocation
        row_sp = tk.Frame(frame, bg=t["bg2"])
        row_sp.pack(fill="x", pady=2)
        tk.Label(
            row_sp, text="SP500", font=f["label"],
            bg=t["bg2"], fg=t["green"], width=8, anchor="w",
        ).pack(side="left")
        self.sp_scale = tk.Scale(
            row_sp, from_=0.0, to=1.0, resolution=0.05,
            orient="horizontal", length=100, showvalue=False,
            width=8, sliderlength=16, borderwidth=1, highlightthickness=0,
            bg=t["bg2"], fg=t["text"], troughcolor=t["bg3"],
            highlightbackground=t["bg2"], font=f["small"],
            command=self._on_alloc_change,
        )
        self.sp_scale.pack(side="left", fill="x", expand=True, padx=(8, 12))
        self.sp_pct_lbl = tk.Label(
            row_sp, text="50%", font=f["badge"],
            bg=t["bg2"], fg=t["green"], width=5, anchor="e",
        )
        self.sp_pct_lbl.pack(side="left")

        # Skew LS allocation (S22)
        row_sk = tk.Frame(frame, bg=t["bg2"])
        row_sk.pack(fill="x", pady=2)
        tk.Label(
            row_sk, text="Skew LS", font=f["label"],
            bg=t["bg2"], fg=t["red"], width=8, anchor="w",
        ).pack(side="left")
        self.sk_scale = tk.Scale(
            row_sk, from_=0.0, to=1.0, resolution=0.05,
            orient="horizontal", length=100, showvalue=False,
            width=8, sliderlength=16, borderwidth=1, highlightthickness=0,
            bg=t["bg2"], fg=t["text"], troughcolor=t["bg3"],
            highlightbackground=t["bg2"], font=f["small"],
            command=self._on_alloc_change,
        )
        self.sk_scale.pack(side="left", fill="x", expand=True, padx=(8, 12))
        self.sk_pct_lbl = tk.Label(
            row_sk, text="0%", font=f["badge"],
            bg=t["bg2"], fg=t["red"], width=5, anchor="e",
        )
        self.sk_pct_lbl.pack(side="left")

        # Exposure + Save
        row_lev = tk.Frame(frame, bg=t["bg2"])
        row_lev.pack(fill="x", pady=(10, 2))
        tk.Label(
            row_lev, text="Exposure", font=f["label"],
            bg=t["bg2"], fg=t["text"], width=8, anchor="w",
        ).pack(side="left")
        self.lev_lbl = tk.Label(
            row_lev, text="130% (1.3x)", font=f["badge"],
            bg=t["bg2"], fg=t["orange"],
        )
        self.lev_lbl.pack(side="left", padx=4)
        self.lev_detail = tk.Label(
            frame, text="", font=f["small"],
            bg=t["bg2"], fg=t["text_dim"],
        )
        self.lev_detail.pack(anchor="w", pady=(2, 0))
        self._btn(
            row_lev, "Save", self._on_save, t["accent_hl"],
            padx=6, pady=2, font=f["small"],
        ).pack(side="right")

    # ── Update from config ───────────────────────────────────

    def update_cfg(self, cfg: dict) -> None:
        """Refresh allocation sliders from a cfg dict."""
        alloc_sr, alloc_sp, alloc_sk = allocations_from_cfg(cfg)
        self.sr_scale.set(alloc_sr)
        self.sp_scale.set(alloc_sp)
        self.sk_scale.set(alloc_sk)
        self._update_alloc_display(alloc_sr, alloc_sp, alloc_sk)

    def get_alloc(self) -> Tuple[float, float, float]:
        """Return (sr, sp, skewls) slider values rounded to 2 decimals."""
        return (
            round(self.sr_scale.get(), 2),
            round(self.sp_scale.get(), 2),
            round(self.sk_scale.get(), 2),
        )

    # ── Internal: slider display ─────────────────────────────

    def _on_alloc_change(self, _=None) -> None:
        sr = self.sr_scale.get()
        sp = self.sp_scale.get()
        sk = self.sk_scale.get()
        self._update_alloc_display(sr, sp, sk)

    def _update_alloc_display(self, alloc_sr: float, alloc_sp: float,
                              alloc_sk: float = 0.0) -> None:
        params = strategy_params(alloc_sr, alloc_sp, alloc_sk)
        for lbl, row in zip((self.sr_pct_lbl, self.sp_pct_lbl, self.sk_pct_lbl), params.rows):
            lbl.config(text=row.text)
        self.lev_lbl.config(text=params.leverage_text, fg=self.t[params.leverage_color])
        self.lev_detail.config(text=params.detail)
