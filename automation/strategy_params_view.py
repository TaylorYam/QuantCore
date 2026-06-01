"""
StrategyParamsView — middle-column Trading Settings + Strategy Parameters
(R3.4 extracted from control_panel.py).

Builds two stacked frames:
  1. Trading Settings: Dry Run toggle button + Data date label
  2. Strategy Parameters: ETF slider, Stocks slider, Exposure summary, Save button

Design:
  - Pure UI class — no config.json reads, no account_utils writes.
  - Panel owns all I/O; view just renders + emits callbacks.
  - on_toggle_dry_run and on_save are called with no arguments. Panel reads
    current slider values via `get_alloc()` when it needs them.

Public API:
    view = StrategyParamsView(
        parent_settings, parent_params,
        theme, fonts, btn_factory,
        on_toggle_dry_run=..., on_save=...,
    )
    view.update_cfg(cfg_dict)   # refresh from cfg (dry_run, alloc_sr, alloc_sp500, alloc_skewls)
    view.update_data_date(str)  # set "Last: YYYY-MM-DD"
    sr, sp, sk = view.get_alloc()   # read slider values (3-tuple)

Note: Settings + Params live in different grid cells in Panel, so the view
takes two parents. Everything else is bundled inside one class for cohesion.
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable, Tuple


_REQUIRED_THEME_KEYS = {
    "bg2", "bg3", "text", "text_dim",
    "blue", "green", "red", "yellow", "orange", "cyan", "accent_hl",
}
_REQUIRED_FONTS_KEYS = {"small", "label", "section", "mono", "badge"}


class StrategyParamsView:
    """Trading Settings + Strategy Parameters middle-column widgets."""

    def __init__(
        self,
        parent_settings: tk.Misc,
        parent_params: tk.Misc,
        theme: dict,
        fonts: dict,
        btn_factory: Callable,
        on_toggle_dry_run: Callable[[], None],
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
        self._on_toggle_dry_run = on_toggle_dry_run
        self._on_save = on_save

        self._build_trading_settings(parent_settings)
        self._build_strategy_params(parent_params)

    # ── Build: Trading Settings ──────────────────────────────

    def _build_trading_settings(self, parent: tk.Misc) -> None:
        t, f = self.t, self.f
        frame = tk.Frame(parent, bg=t["bg2"], padx=10, pady=6)
        frame.pack(fill="x")  # parent manages placement, we just fill
        self.settings_frame = frame

        tk.Label(
            frame, text="Trading Settings", font=f["section"],
            bg=t["bg2"], fg=t["accent_hl"],
        ).pack(anchor="w")

        # Dry Run row
        row_dry = tk.Frame(frame, bg=t["bg2"])
        row_dry.pack(fill="x", pady=2)
        tk.Label(
            row_dry, text="Mode", font=f["label"],
            bg=t["bg2"], fg=t["text"], width=10, anchor="w",
        ).pack(side="left")
        self.dry_btn = tk.Button(
            row_dry, text="--", font=f["badge"],
            width=12, relief="flat", cursor="hand2",
            command=self._on_toggle_dry_run,
        )
        self.dry_btn.pack(side="left", padx=4)
        self.dry_lbl = tk.Label(
            row_dry, text="", font=f["small"],
            bg=t["bg2"], fg=t["text_dim"],
        )
        self.dry_lbl.pack(side="left")

        # Data Status row
        row_data = tk.Frame(frame, bg=t["bg2"])
        row_data.pack(fill="x", pady=2)
        tk.Label(
            row_data, text="Data", font=f["label"],
            bg=t["bg2"], fg=t["text"], width=10, anchor="w",
        ).pack(side="left")
        self.data_date_lbl = tk.Label(
            row_data, text="...", font=f["mono"],
            bg=t["bg2"], fg=t["cyan"],
        )
        self.data_date_lbl.pack(side="left")

    # ── Build: Strategy Parameters ───────────────────────────

    def _build_strategy_params(self, parent: tk.Misc) -> None:
        t, f = self.t, self.f
        frame = tk.Frame(parent, bg=t["bg2"], padx=10, pady=6)
        frame.pack(fill="x")
        self.params_frame = frame

        tk.Label(
            frame, text="Strategy Parameters", font=f["section"],
            bg=t["bg2"], fg=t["accent_hl"],
        ).pack(anchor="w")

        # ETF allocation
        row_sr = tk.Frame(frame, bg=t["bg2"])
        row_sr.pack(fill="x", pady=2)
        tk.Label(
            row_sr, text="ETF", font=f["label"],
            bg=t["bg2"], fg=t["blue"], width=8, anchor="w",
        ).pack(side="left")
        self.sr_scale = tk.Scale(
            row_sr, from_=0.0, to=1.0, resolution=0.05,
            orient="horizontal", length=140,
            bg=t["bg2"], fg=t["text"], troughcolor=t["bg3"],
            highlightbackground=t["bg2"], font=f["small"],
            command=self._on_alloc_change,
        )
        self.sr_scale.pack(side="left", padx=4)
        self.sr_pct_lbl = tk.Label(
            row_sr, text="80%", font=f["badge"],
            bg=t["bg2"], fg=t["blue"],
        )
        self.sr_pct_lbl.pack(side="left")

        # Stocks allocation
        row_sp = tk.Frame(frame, bg=t["bg2"])
        row_sp.pack(fill="x", pady=2)
        tk.Label(
            row_sp, text="Stocks", font=f["label"],
            bg=t["bg2"], fg=t["green"], width=8, anchor="w",
        ).pack(side="left")
        self.sp_scale = tk.Scale(
            row_sp, from_=0.0, to=1.0, resolution=0.05,
            orient="horizontal", length=140,
            bg=t["bg2"], fg=t["text"], troughcolor=t["bg3"],
            highlightbackground=t["bg2"], font=f["small"],
            command=self._on_alloc_change,
        )
        self.sp_scale.pack(side="left", padx=4)
        self.sp_pct_lbl = tk.Label(
            row_sp, text="50%", font=f["badge"],
            bg=t["bg2"], fg=t["green"],
        )
        self.sp_pct_lbl.pack(side="left")

        # Skew LS allocation (S22)
        row_sk = tk.Frame(frame, bg=t["bg2"])
        row_sk.pack(fill="x", pady=2)
        tk.Label(
            row_sk, text="SkewLS", font=f["label"],
            bg=t["bg2"], fg=t["red"], width=8, anchor="w",
        ).pack(side="left")
        self.sk_scale = tk.Scale(
            row_sk, from_=0.0, to=1.0, resolution=0.05,
            orient="horizontal", length=140,
            bg=t["bg2"], fg=t["text"], troughcolor=t["bg3"],
            highlightbackground=t["bg2"], font=f["small"],
            command=self._on_alloc_change,
        )
        self.sk_scale.pack(side="left", padx=4)
        self.sk_pct_lbl = tk.Label(
            row_sk, text="0%", font=f["badge"],
            bg=t["bg2"], fg=t["red"],
        )
        self.sk_pct_lbl.pack(side="left")

        # Exposure + Save
        row_lev = tk.Frame(frame, bg=t["bg2"])
        row_lev.pack(fill="x", pady=(3, 2))
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
            row_lev, text="", font=f["small"],
            bg=t["bg2"], fg=t["text_dim"],
        )
        self.lev_detail.pack(side="left")
        self._btn(
            row_lev, "Save", self._on_save, t["accent_hl"],
            padx=6, pady=2, font=f["small"],
        ).pack(side="right")

    # ── Update from config ───────────────────────────────────

    def update_cfg(self, cfg: dict) -> None:
        """Refresh dry_run button + allocation sliders from a cfg dict."""
        t = self.t

        # Dry Run
        dry = cfg.get("dry_run", True)
        self.dry_btn.config(
            text="SIMULATED" if dry else "LIVE",
            bg=t["yellow"] if dry else t["red"],
            fg="black" if dry else "white",
        )
        self.dry_lbl.config(text="(no real orders)" if dry else "!! LIVE !!")

        # Allocations
        alloc_sr = cfg.get("alloc_sr", 0.80)
        alloc_sp = cfg.get("alloc_sp500", 0.50)
        alloc_sk = cfg.get("alloc_skewls", 0.0)
        self.sr_scale.set(alloc_sr)
        self.sp_scale.set(alloc_sp)
        self.sk_scale.set(alloc_sk)
        self._update_alloc_display(alloc_sr, alloc_sp, alloc_sk)

    def update_data_date(self, date_str: str) -> None:
        self.data_date_lbl.config(text=f"Last: {date_str}")

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
        t = self.t
        total = alloc_sr + alloc_sp + alloc_sk
        self.sr_pct_lbl.config(text=f"{alloc_sr:.0%}")
        self.sp_pct_lbl.config(text=f"{alloc_sp:.0%}")
        self.sk_pct_lbl.config(text=f"{alloc_sk:.0%}")
        self.lev_lbl.config(text=f"{total:.0%} ({total:.1f}x)")

        if total > 1.0:
            borrow = total - 1.0
            self.lev_detail.config(text=f"Borrow: {borrow:.0%}")
            self.lev_lbl.config(fg=t["orange"])
        else:
            self.lev_detail.config(text="No leverage")
            self.lev_lbl.config(fg=t["green"])
