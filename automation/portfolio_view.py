"""
PortfolioView — per-account Portfolio Overview (Issue #20 rewrite)

Split-build API for the 3-column layout: metrics, chart, and holdings
can be placed into separate parent frames so Strategy Params can sit
below the metrics.

Public API:
    pv = PortfolioView(theme, fonts)
    pv.build_metrics(parent)    # net asset value, P&L, margin cushion, exposures
    pv.build_chart(parent)      # donut chart
    pv.build_holdings(parent)   # ETF / Stock / Other lists
    pv.update_state(name, state)
    pv.update(pf_dict)

Display text and colors come from `portfolio_display` (shared with the Qt
Panel, #149); this module only owns the Tk widgets and the matplotlib donut.
"""

from __future__ import annotations

import math
import tkinter as tk
from typing import Optional

import portfolio_display as display


_REQUIRED_THEME_KEYS = {
    "bg", "bg2", "bg3", "text", "text_dim",
    "blue", "green", "red", "yellow", "orange",
}

_REQUIRED_FONTS_KEYS = {"small", "mono_sm", "med_num"}


class PortfolioView:
    """Per-account Portfolio Overview widgets + update logic."""

    def __init__(self, theme: dict, fonts: dict):
        missing_t = _REQUIRED_THEME_KEYS - set(theme)
        missing_f = _REQUIRED_FONTS_KEYS - set(fonts)
        if missing_t:
            raise ValueError(f"theme missing keys: {sorted(missing_t)}")
        if missing_f:
            raise ValueError(f"fonts missing keys: {sorted(missing_f)}")
        self.t = theme
        self.f = fonts
        self.chart_canvas: Optional[object] = None
        self.refresh_summary_lbl = None

    # ── Build: Metrics ───────────────────────────────────────────

    def build_metrics(self, parent: tk.Misc) -> None:
        t, f = self.t, self.f

        row = tk.Frame(parent, bg=t["bg2"])
        row.pack(fill="x")
        cards = [(f"{key}_card", title, value)
                 for key, title, value in display.METRIC_TITLES]
        for index, (attribute, title, value) in enumerate(cards):
            row.columnconfigure(index, weight=1, uniform="metrics")
            card = self._metric_card(row, title, value, t["text"])
            card.grid(row=0, column=index, sticky="nsew",
                      padx=(0, 4 if index < len(cards) - 1 else 0), pady=(0, 4))
            setattr(self, attribute, card)

        self.report_date_lbl = tk.Label(
            parent, text="Report: —",
            font=f["small"], bg=t["bg2"], fg=t["text_dim"],
        )
        self.report_date_lbl.pack(anchor="w", pady=(2, 3))

    # ── Build: Chart ─────────────────────────────────────────────

    def build_chart(self, parent: tk.Misc) -> None:
        self.chart_frame = parent

    # ── Build: Holdings ──────────────────────────────────────────

    def build_holdings(self, parent: tk.Misc) -> None:
        t, f = self.t, self.f

        etf_frame = tk.Frame(parent, bg=t["bg3"], padx=6, pady=4)
        etf_frame.pack(fill="x", pady=(0, 3))
        tk.Label(etf_frame, text="ETF Holdings (SR)", font=f["small"],
                 bg=t["bg3"], fg=t["blue"]).pack(anchor="w")
        self.etf_hdr = tk.Label(etf_frame, text="", font=f["mono_sm"],
                                 bg=t["bg3"], fg=t["text_dim"], justify="left", anchor="nw")
        self.etf_hdr.pack(anchor="w", fill="x")
        self.etf_list = tk.Label(etf_frame, text="—", font=f["mono_sm"],
                                  bg=t["bg3"], fg=t["text"], justify="left", anchor="nw")
        self.etf_list.pack(anchor="w", fill="x")

        stock_frame = tk.Frame(parent, bg=t["bg3"], padx=6, pady=4)
        stock_frame.pack(fill="x", pady=(0, 3))
        tk.Label(stock_frame, text="Stock Holdings (MOM)", font=f["small"],
                 bg=t["bg3"], fg=t["green"]).pack(anchor="w")
        self.stock_hdr = tk.Label(stock_frame, text="", font=f["mono_sm"],
                                   bg=t["bg3"], fg=t["text_dim"], justify="left", anchor="nw")
        self.stock_hdr.pack(anchor="w", fill="x")
        self.stock_list = tk.Label(stock_frame, text="—", font=f["mono_sm"],
                                    bg=t["bg3"], fg=t["text"], justify="left", anchor="nw")
        self.stock_list.pack(anchor="w", fill="x")

        other_frame = tk.Frame(parent, bg=t["bg3"], padx=6, pady=4)
        other_frame.pack(fill="x")
        tk.Label(other_frame, text="Other (Manual)", font=f["small"],
                 bg=t["bg3"], fg=t["text_dim"]).pack(anchor="w")
        self.other_hdr = tk.Label(other_frame, text="", font=f["mono_sm"],
                                   bg=t["bg3"], fg=t["text_dim"], justify="left", anchor="nw")
        self.other_hdr.pack(anchor="w", fill="x")
        self.other_list = tk.Label(other_frame, text="—", font=f["mono_sm"],
                                    bg=t["bg3"], fg=t["text_dim"], justify="left", anchor="nw")
        self.other_list.pack(anchor="w", fill="x")
        self.other_frame = other_frame

    # ── Metric card helper ───────────────────────────────────────

    def _metric_card(self, parent: tk.Misc, title: str, value: str, color: str) -> tk.Frame:
        t = self.t
        f = self.f
        card = tk.Frame(parent, bg=t["bg3"], padx=8, pady=6)
        title_lbl = tk.Label(card, text=title, font=f["small"], bg=t["bg3"],
                             fg=t["text_dim"], anchor="w", justify="left", height=2)
        title_lbl.pack(anchor="w", fill="x")
        card.bind("<Configure>", lambda e: title_lbl.configure(wraplength=max(40, e.width - 20)))
        val_lbl = tk.Label(card, text=value, font=f["med_num"], bg=t["bg3"], fg=color)
        val_lbl.pack(anchor="e")
        card._val_lbl = val_lbl  # type: ignore[attr-defined]
        return card

    # ── Update ────────────────────────────────────────────────

    def update_state(self, name, state) -> None:
        self.update(state.portfolio or {"available": False, "error": "No data yet"})
        summary = getattr(self, "refresh_summary_lbl", None)
        if summary is not None:
            text, color = display.refresh_summary(name, state)
            summary.config(text=text, fg=self.t[color])
        text, color = display.status_line(state)
        self.report_date_lbl.config(text=text, fg=self.t[color])

    def _show_metrics(self, pf: dict) -> None:
        for metric in display.metrics(pf):
            card = getattr(self, f"{metric.key}_card")
            card._val_lbl.config(text=metric.text, fg=self.t[metric.color])

    def update(self, pf: dict) -> None:
        if not pf.get("available"):
            self._show_metrics(pf)
            self.report_date_lbl.config(text=display.report_line(pf))
            self.etf_list.config(text="(no data)")
            self.stock_list.config(text="(no data)")
            self.other_list.config(text="(no data)")
            for header in (self.etf_hdr, self.stock_hdr, self.other_hdr):
                header.config(text="")
            if self.chart_canvas:
                self.chart_canvas.get_tk_widget().destroy()
                self.chart_canvas = None
            return

        self._show_metrics(pf)
        self.report_date_lbl.config(text=display.report_line(pf))

        nav = pf["nav"]
        self._render_holdings_list(self.etf_hdr, self.etf_list,
                                    pf.get("etf_holdings", []), nav, "etf")
        self._render_holdings_list(self.stock_hdr, self.stock_list,
                                    pf.get("stock_holdings", []), nav, "stock")
        self._render_holdings_list(self.other_hdr, self.other_list,
                                    pf.get("other_holdings", []),
                                    nav, "other", empty_text="(none)")

        if self.chart_canvas:
            try:
                self.chart_canvas.get_tk_widget().destroy()
            except Exception:
                pass
            self.chart_canvas = None

        donut = display.donut(pf)
        if donut is not None:
            self._draw_donut(donut)

    def _render_holdings_list(self, hdr_widget: tk.Label, widget: tk.Label,
                               holdings: list, nav: float, group: str,
                               empty_text: str = "(empty)") -> None:
        header, body = display.holdings_text(display.holding_rows(holdings, nav, group))
        hdr_widget.config(text=header)
        widget.config(text=body or empty_text)

    # ── Donut chart ───────────────────────────────────────────

    _MAX_PIE_LABELS = display.MAX_DONUT_LABELS

    def _render_pie_chart(self, holdings: list, nav: float, exposure: float,
                           other_val: float = 0) -> None:
        slices = display.donut_slices(holdings, nav, exposure, other_val)
        self._draw_donut(display.Donut(slices, exposure, display.donut_center(exposure)))

    def _draw_donut(self, donut: "display.Donut") -> None:
        t = self.t

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

        if self.chart_canvas:
            self.chart_canvas.get_tk_widget().destroy()
            self.chart_canvas = None

        labels = [s.label for s in donut.slices]
        values = [s.value for s in donut.slices]
        colors = [s.color for s in donut.slices]

        fig, ax = plt.subplots(figsize=(2.8, 2.0), dpi=100)
        fig.patch.set_facecolor(t["bg2"])
        ax.set_facecolor(t["bg2"])
        ax.axis("off")

        center_x = -0.15
        wedges, _ = ax.pie(
            values, labels=None, colors=colors,
            startangle=90, counterclock=False,
            wedgeprops=dict(width=0.38, edgecolor=t["bg2"], linewidth=2),
            radius=0.85, center=(center_x, 0),
        )

        annotations = []
        for i, (wedge, label) in enumerate(zip(wedges, labels)):
            ang = (wedge.theta2 + wedge.theta1) / 2
            pct = donut.slices[i].pct_of_nav
            side = 1 if math.cos(math.radians(ang)) >= 0 else -1
            y_pos = 0.95 * math.sin(math.radians(ang))
            annotations.append((ang, pct, label, side, y_pos))

        min_gap = 0.28
        y_bound = 1.15
        for side_val in [1, -1]:
            side_annots = [(i, a) for i, a in enumerate(annotations) if a[3] == side_val]
            if len(side_annots) < 2:
                continue
            side_annots.sort(key=lambda x: x[1][4])
            ys = [a[4] for _, a in side_annots]
            for k in range(1, len(ys)):
                if ys[k] - ys[k - 1] < min_gap:
                    ys[k] = ys[k - 1] + min_gap
            if ys[-1] > y_bound:
                ys[-1] = y_bound
                for k in range(len(ys) - 2, -1, -1):
                    if ys[k + 1] - ys[k] < min_gap:
                        ys[k] = ys[k + 1] - min_gap
            for k, (idx, _) in enumerate(side_annots):
                old = annotations[idx]
                annotations[idx] = (old[0], old[1], old[2], old[3], ys[k])

        for ang, pct, label, side, y_final in annotations:
            x_edge = center_x + 0.70 * math.cos(math.radians(ang))
            y_edge = 0.70 * math.sin(math.radians(ang))
            x_text = center_x + 1.20 * side
            ha = "left" if side > 0 else "right"
            ax.annotate(
                f"{label} {pct:.0f}%",
                xy=(x_edge, y_edge),
                xytext=(x_text + 0.05 * side, y_final),
                ha=ha, va="center",
                fontsize=7, fontweight="bold", color="white",
                arrowprops=dict(
                    arrowstyle="-",
                    color="#6b7d99",
                    lw=0.8,
                    connectionstyle=f"arc3,rad={0.1 * side}",
                ),
            )

        center_text, center_color = donut.center
        ax.text(center_x, 0, center_text, ha="center", va="center",
                fontsize=8, fontweight="bold", color=t[center_color])

        fig.tight_layout(pad=0.3)

        canvas = FigureCanvasTkAgg(fig, master=self.chart_frame)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True)
        self.chart_canvas = canvas
        plt.close(fig)
