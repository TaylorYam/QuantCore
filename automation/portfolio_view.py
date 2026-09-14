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
"""

from __future__ import annotations

import math
import tkinter as tk
from typing import Optional

from ib_fetcher import SR_ETFS, ETF_TICKERS


_REQUIRED_THEME_KEYS = {
    "bg", "bg2", "bg3", "text", "text_dim",
    "blue", "green", "red", "yellow", "orange",
}

_TICKER_NAMES: dict[str, str] = {
    "XLK": "Technology",      "XLF": "Financials",
    "XLV": "Health Care",     "XLE": "Energy",
    "XLI": "Industrials",     "XLY": "Cons Discret",
    "XLB": "Materials",       "XLP": "Cons Staples",
    "XLU": "Utilities",       "XLC": "Comm Services",
    "XLRE": "Real Estate",
    "REW": "UltraShort Tech",     "SEF": "Short Fin",
    "RXD": "UltraShort HC",       "ERY": "Bear Energy 3x",
    "SIJ": "UltraShort Ind",      "SCC": "UltraShort CD",
    "SMN": "UltraShort Mat",      "SZK": "UltraShort CS",
    "SDP": "UltraShort Util",     "REK": "Short RE",
    "SPY": "S&P 500 ETF",         "SSO": "Ultra S&P 500",
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
        cards = (
            ("nav_card", "Net Asset Value", "$—"),
            ("pnl_card", "Unrealized P&L", "$—"),
            ("margin_card", "Margin Cushion", "—%"),
            ("strat_exp_card", "Strategy Exposure", "—%"),
            ("total_exp_card", "Account Exposure", "—%"),
        )
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
        last = (state.updated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                if state.updated_at else "—")
        summary = getattr(self, "refresh_summary_lbl", None)
        if summary is not None:
            source = {"manual": "Manual", "auto": "Auto"}.get(state.refresh_source, "Unknown source")
            detail = source if state.updated_at else "No data yet"
            if state.running:
                detail += " · Updating"
            elif state.error:
                detail += " · Failed"
            summary.config(text=f"{name} | {detail}\n{last}",
                           fg=self.t["yellow"] if state.error else self.t["text"])
        status = "Updating" if state.running else "Failed" if state.error else "OK"
        source_tag = ""
        if state.refresh_source:
            source_tag = f" ({state.refresh_source})"
        self.report_date_lbl.config(
            text=f"{status} | {last}{source_tag}",
            fg=self.t["yellow"] if state.error else self.t["text_dim"])

    def update(self, pf: dict) -> None:
        t = self.t

        if not pf.get("available"):
            err = pf.get("error", "No data yet")
            self.nav_card._val_lbl.config(text="$—", fg=t["text_dim"])
            self.pnl_card._val_lbl.config(text="$—", fg=t["text_dim"])
            self.margin_card._val_lbl.config(text="—%", fg=t["text_dim"])
            self.strat_exp_card._val_lbl.config(text="—%", fg=t["text_dim"])
            self.total_exp_card._val_lbl.config(text="—%", fg=t["text_dim"])
            self.report_date_lbl.config(text=f"Report: {err}")
            self.etf_list.config(text="(no data)")
            self.stock_list.config(text="(no data)")
            self.other_list.config(text="(no data)")
            for header in (self.etf_hdr, self.stock_hdr, self.other_hdr):
                header.config(text="")
            if self.chart_canvas:
                self.chart_canvas.get_tk_widget().destroy()
                self.chart_canvas = None
            return

        nav = pf["nav"]
        upnl = pf["total_upnl"]
        cushion = pf["cushion"]
        mktval = pf["total_mktval"]
        strategy_mktval = pf.get("strategy_mktval", mktval)
        other_mktval = pf.get("other_mktval", 0)
        strat_exposure = (strategy_mktval / nav * 100) if nav > 0 else 0
        total_exposure = (mktval / nav * 100) if nav > 0 else 0
        exposure = strat_exposure

        self.nav_card._val_lbl.config(text=f"${nav:,.0f}", fg=t["text"])

        sign = "+" if upnl >= 0 else ""
        pnl_color = t["green"] if upnl >= 0 else t["red"]
        self.pnl_card._val_lbl.config(text=f"{sign}${upnl:,.2f}", fg=pnl_color)

        if cushion < 10:
            m_color = t["red"]
        elif cushion < 25:
            m_color = t["yellow"]
        else:
            m_color = t["green"]
        self.margin_card._val_lbl.config(text=f"{cushion:.1f}%", fg=m_color)

        s_color = t["orange"] if strat_exposure > 100 else t["green"]
        self.strat_exp_card._val_lbl.config(text=f"{strat_exposure:.0f}%", fg=s_color)

        a_color = t["orange"] if total_exposure > 100 else t["green"]
        self.total_exp_card._val_lbl.config(text=f"{total_exposure:.0f}%", fg=a_color)

        source = pf.get("source", "Excel")
        src_tag = f"  [{source}]" if source != "Excel" else ""
        self.report_date_lbl.config(text=f"Report Date: {pf['date']}{src_tag}")

        self._render_holdings_list(self.etf_hdr, self.etf_list,
                                    pf.get("etf_holdings", []), nav)
        self._render_holdings_list(self.stock_hdr, self.stock_list,
                                    pf.get("stock_holdings", []), nav)
        self._render_holdings_list(self.other_hdr, self.other_list,
                                    pf.get("other_holdings", []),
                                    nav, empty_text="(none)")

        if self.chart_canvas:
            try:
                self.chart_canvas.get_tk_widget().destroy()
            except Exception:
                pass
            self.chart_canvas = None

        all_holdings = pf.get("etf_holdings", []) + pf.get("stock_holdings", [])
        if (all_holdings or other_mktval > 0) and nav > 0:
            self._render_pie_chart(all_holdings, nav, exposure, other_val=other_mktval)

    @staticmethod
    def _fmt_dollar(val: float) -> str:
        if val > 0:
            return f"+${val:,.0f}"
        elif val < 0:
            return f"-${abs(val):,.0f}"
        return "$0"

    def _render_holdings_list(self, hdr_widget: tk.Label, widget: tk.Label,
                               holdings: list, nav: float,
                               empty_text: str = "(empty)") -> None:
        if not holdings:
            hdr_widget.config(text="")
            widget.config(text=empty_text)
            return

        sorted_h = sorted(holdings, key=lambda x: -abs(x["mkt_val"]))

        rows = []
        for h in sorted_h:
            w = h["mkt_val"] / nav * 100 if nav > 0 else 0
            avg = h.get("avg_cost", 0)
            upnl_val = h.get("upnl", 0)
            pct = h.get("upnl_pct", 0)
            sign = "+" if pct >= 0 else ""
            name = h.get("name") or _TICKER_NAMES.get(h["symbol"], "")
            rows.append((
                h["symbol"],
                f"{h['shares']}",
                f"{w:.0f}%",
                f"${avg:,.2f}",
                f"${h['mkt_px']:,.2f}",
                self._fmt_dollar(upnl_val),
                f"{sign}{pct:.1f}%",
                name,
            ))

        headers = ("UL", "SH", "Wt%", "Cost", "Price", "P&L", "P&L%", "Name")
        widths = [len(h) for h in headers]
        for r in rows:
            for i, cell in enumerate(r):
                widths[i] = max(widths[i], len(cell))

        def _fmt_row(cells):
            parts = [f"{cells[0]:<{widths[0]}}"]
            for i in range(1, 7):
                parts.append(f"{cells[i]:>{widths[i]}}")
            parts.append("")
            parts.append(f"{cells[7]:<{widths[7]}}")
            return "  ".join(parts)

        hdr_widget.config(text=_fmt_row(headers))
        lines = [_fmt_row(r) for r in rows]
        widget.config(text="\n".join(lines))

    # ── Donut chart ───────────────────────────────────────────

    def _render_pie_chart(self, holdings: list, nav: float, exposure: float,
                           other_val: float = 0) -> None:
        t = self.t

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

        if self.chart_canvas:
            self.chart_canvas.get_tk_widget().destroy()
            self.chart_canvas = None

        sorted_h = sorted(holdings, key=lambda x: -abs(x["mkt_val"]))
        labels = [h["symbol"] for h in sorted_h]
        values = [abs(h["mkt_val"]) for h in sorted_h]
        total_mktval = sum(values)

        etf_blues = ["#3b82f6", "#60a5fa", "#93c5fd", "#bfdbfe",
                      "#2563eb", "#1d4ed8", "#1e40af", "#1e3a8a",
                      "#6366f1", "#818cf8", "#a5b4fc"]
        stock_greens = ["#22c55e", "#4ade80", "#86efac", "#bbf7d0",
                         "#16a34a", "#15803d", "#166534", "#14532d"]
        colors = []
        ei, si = 0, 0
        for h in sorted_h:
            if h["symbol"] in ETF_TICKERS:
                colors.append(etf_blues[ei % len(etf_blues)])
                ei += 1
            else:
                colors.append(stock_greens[si % len(stock_greens)])
                si += 1

        if other_val > 0:
            labels.append("Other")
            values.append(abs(other_val))
            total_mktval += abs(other_val)
            colors.append("#6b7280")

        if exposure < 100 and nav > total_mktval:
            cash = nav - total_mktval
            labels.append("Cash")
            values.append(cash)
            colors.append("#4b5563")

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
            pct = values[i] / total_mktval * 100 if total_mktval > 0 else 0
            side = 1 if math.cos(math.radians(ang)) >= 0 else -1
            y_pos = 0.95 * math.sin(math.radians(ang))
            annotations.append((ang, pct, label, side, y_pos))

        min_gap = 0.28
        for side_val in [1, -1]:
            side_annots = [(i, a) for i, a in enumerate(annotations) if a[3] == side_val]
            side_annots.sort(key=lambda x: x[1][4])
            for k in range(1, len(side_annots)):
                prev_idx, prev = side_annots[k - 1]
                curr_idx, curr = side_annots[k]
                if curr[4] - prev[4] < min_gap:
                    new_y = prev[4] + min_gap
                    old = annotations[curr_idx]
                    annotations[curr_idx] = (old[0], old[1], old[2], old[3], new_y)

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

        if exposure > 100:
            center_text = f"{exposure:.0f}%\nof NAV"
            center_color = t["orange"]
        else:
            center_text = f"{exposure:.0f}%\nExposure"
            center_color = t["green"]
        ax.text(center_x, 0, center_text, ha="center", va="center",
                fontsize=8, fontweight="bold", color=center_color)

        fig.tight_layout(pad=0.3)

        canvas = FigureCanvasTkAgg(fig, master=self.chart_frame)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True)
        self.chart_canvas = canvas
        plt.close(fig)
