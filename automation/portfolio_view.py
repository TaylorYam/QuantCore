"""
PortfolioView — Panel 左欄的 Portfolio Overview（R3.2 從 control_panel 抽出）

職責：
  - 4 個指標卡（NAV / uPnL / Margin / Strategy+Account Exposure）
  - 3 個持倉列表（ETF / Stock / Other）
  - 右側 matplotlib donut chart

對外 API：
  pv = PortfolioView(parent=frame, theme=theme, fonts=fonts)
  pv.update(pf_dict)   # 每次 refresh_thread 取得新資料後呼叫

設計：
  - 純 UI、read-only（不觸發任何 callback）
  - 依賴透過 constructor 注入（theme dict、fonts dict）
  - 所有 widget references 存在 self.X（不汙染 Panel 的 namespace）
"""

from __future__ import annotations

import math
import tkinter as tk
from typing import Optional

from ib_fetcher import SR_ETFS


_REQUIRED_THEME_KEYS = {
    "bg", "bg2", "bg3", "text", "text_dim",
    "blue", "green", "red", "yellow", "orange",
}

_REQUIRED_FONTS_KEYS = {"small", "mono_sm", "med_num"}


class PortfolioView:
    """Left-column Portfolio Overview widgets + update logic."""

    def __init__(self, parent: tk.Misc, theme: dict, fonts: dict):
        missing_t = _REQUIRED_THEME_KEYS - set(theme)
        missing_f = _REQUIRED_FONTS_KEYS - set(fonts)
        if missing_t:
            raise ValueError(f"theme missing keys: {sorted(missing_t)}")
        if missing_f:
            raise ValueError(f"fonts missing keys: {sorted(missing_f)}")
        self.t = theme
        self.f = fonts

        # Widgets (built in _build)
        self.overview_frame: tk.Frame
        self.nav_card: tk.Frame
        self.pnl_card: tk.Frame
        self.margin_card: tk.Frame
        self.strat_exp_card: tk.Frame
        self.total_exp_card: tk.Frame
        self.report_date_lbl: tk.Label
        self.etf_list: tk.Label
        self.stock_list: tk.Label
        self.other_list: tk.Label
        self.other_frame: tk.Frame
        self.chart_frame: tk.Frame
        self.chart_canvas: Optional[object] = None

        self._build(parent)

    # ── Build ─────────────────────────────────────────────────

    def _build(self, parent: tk.Misc) -> None:
        t = self.t
        f = self.f

        self.overview_frame = tk.Frame(parent, bg=t["bg2"], padx=8, pady=4)
        self.overview_frame.pack(fill="both", expand=True)

        # 左右分欄：左=數字+列表 右=圓餅圖
        left_pf = tk.Frame(self.overview_frame, bg=t["bg2"])
        left_pf.pack(side="left", fill="both", expand=True)

        right_pf = tk.Frame(self.overview_frame, bg=t["bg2"])
        right_pf.pack(side="right", fill="y")

        # ── 左上：指標卡（2x2 grid）
        cards_frame = tk.Frame(left_pf, bg=t["bg2"])
        cards_frame.pack(fill="x")

        row1 = tk.Frame(cards_frame, bg=t["bg2"])
        row1.pack(fill="x")
        self.nav_card = self._metric_card(row1, "NAV", "$—", t["text"])
        self.nav_card.pack(side="left", fill="x", expand=True, padx=(0, 3), pady=(0, 3))
        self.pnl_card = self._metric_card(row1, "Unrealized P&L", "$—", t["text"])
        self.pnl_card.pack(side="left", fill="x", expand=True, pady=(0, 3))

        row2 = tk.Frame(cards_frame, bg=t["bg2"])
        row2.pack(fill="x")
        self.margin_card = self._metric_card(row2, "Margin Cushion", "—%", t["text"])
        self.margin_card.pack(side="left", fill="x", expand=True, padx=(0, 3), pady=(0, 3))
        self.strat_exp_card = self._metric_card(row2, "Strategy Exp.", "—%", t["text"])
        self.strat_exp_card.pack(side="left", fill="x", expand=True, padx=(0, 3), pady=(0, 3))
        self.total_exp_card = self._metric_card(row2, "Account Exp.", "—%", t["text"])
        self.total_exp_card.pack(side="left", fill="x", expand=True, pady=(0, 3))

        # 報告日期
        self.report_date_lbl = tk.Label(
            left_pf, text="Report: —",
            font=f["small"], bg=t["bg2"], fg=t["text_dim"],
        )
        self.report_date_lbl.pack(anchor="w", pady=(2, 3))

        # ── 左下：持倉文字列表
        etf_frame = tk.Frame(left_pf, bg=t["bg3"], padx=6, pady=4)
        etf_frame.pack(fill="x", pady=(0, 3))
        tk.Label(etf_frame, text="ETF Holdings (SR)", font=f["small"],
                 bg=t["bg3"], fg=t["blue"]).pack(anchor="w")
        self.etf_list = tk.Label(etf_frame, text="—", font=f["mono_sm"],
                                  bg=t["bg3"], fg=t["text"], justify="left", anchor="nw")
        self.etf_list.pack(anchor="w", fill="x")

        stock_frame = tk.Frame(left_pf, bg=t["bg3"], padx=6, pady=4)
        stock_frame.pack(fill="x", pady=(0, 3))
        tk.Label(stock_frame, text="Stock Holdings (MOM)", font=f["small"],
                 bg=t["bg3"], fg=t["green"]).pack(anchor="w")
        self.stock_list = tk.Label(stock_frame, text="—", font=f["mono_sm"],
                                    bg=t["bg3"], fg=t["text"], justify="left", anchor="nw")
        self.stock_list.pack(anchor="w", fill="x")

        other_frame = tk.Frame(left_pf, bg=t["bg3"], padx=6, pady=4)
        other_frame.pack(fill="x")
        tk.Label(other_frame, text="Other (Manual, excluded)", font=f["small"],
                 bg=t["bg3"], fg=t["text_dim"]).pack(anchor="w")
        self.other_list = tk.Label(other_frame, text="—", font=f["mono_sm"],
                                    bg=t["bg3"], fg=t["text_dim"], justify="left", anchor="nw")
        self.other_list.pack(anchor="w", fill="x")
        self.other_frame = other_frame

        # ── 右：圓餅圖
        self.chart_frame = right_pf
        self.chart_canvas = None

    def _metric_card(self, parent: tk.Misc, title: str, value: str, color: str) -> tk.Frame:
        t = self.t
        f = self.f
        card = tk.Frame(parent, bg=t["bg3"], padx=8, pady=6)
        tk.Label(card, text=title, font=f["small"], bg=t["bg3"], fg=t["text_dim"]).pack(anchor="w")
        val_lbl = tk.Label(card, text=value, font=f["med_num"], bg=t["bg3"], fg=color)
        val_lbl.pack(anchor="w")
        card._val_lbl = val_lbl  # type: ignore[attr-defined]
        return card

    # ── Update ────────────────────────────────────────────────

    def update(self, pf: dict) -> None:
        """Refresh all widgets from a portfolio snapshot dict."""
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
            return

        nav = pf["nav"]
        upnl = pf["total_upnl"]
        cushion = pf["cushion"]
        mktval = pf["total_mktval"]
        strategy_mktval = pf.get("strategy_mktval", mktval)
        other_mktval = pf.get("other_mktval", 0)
        strat_exposure = (strategy_mktval / nav * 100) if nav > 0 else 0
        total_exposure = (mktval / nav * 100) if nav > 0 else 0
        exposure = strat_exposure  # center text uses strategy exposure

        # NAV
        self.nav_card._val_lbl.config(text=f"${nav:,.0f}", fg=t["text"])

        # P&L
        sign = "+" if upnl >= 0 else ""
        pnl_color = t["green"] if upnl >= 0 else t["red"]
        self.pnl_card._val_lbl.config(text=f"{sign}${upnl:,.2f}", fg=pnl_color)

        # Margin
        if cushion < 10:
            m_color = t["red"]
        elif cushion < 25:
            m_color = t["yellow"]
        else:
            m_color = t["green"]
        self.margin_card._val_lbl.config(text=f"{cushion:.1f}%", fg=m_color)

        # Strategy Exposure
        s_color = t["orange"] if strat_exposure > 100 else t["green"]
        self.strat_exp_card._val_lbl.config(text=f"{strat_exposure:.0f}%", fg=s_color)

        # Account Exposure
        a_color = t["orange"] if total_exposure > 100 else t["green"]
        self.total_exp_card._val_lbl.config(text=f"{total_exposure:.0f}%", fg=a_color)

        # Date + source
        source = pf.get("source", "Excel")
        src_tag = f"  [{source}]" if source != "Excel" else ""
        self.report_date_lbl.config(text=f"Report Date: {pf['date']}{src_tag}")

        # Holdings lists
        self._render_holdings_list(self.etf_list, pf.get("etf_holdings", []), nav)
        self._render_holdings_list(self.stock_list, pf.get("stock_holdings", []), nav)
        self._render_holdings_list(self.other_list, pf.get("other_holdings", []),
                                    nav, empty_text="(none)")

        # Pie chart — always clear old canvas first, so switching to an account
        # with no holdings drops the previous account's chart (S10 post-fix bug)
        if self.chart_canvas:
            try:
                self.chart_canvas.get_tk_widget().destroy()
            except Exception:
                pass
            self.chart_canvas = None

        all_holdings = pf.get("etf_holdings", []) + pf.get("stock_holdings", [])
        if (all_holdings or other_mktval > 0) and nav > 0:
            self._render_pie_chart(all_holdings, nav, exposure, other_val=other_mktval)

    def _render_holdings_list(self, widget: tk.Label, holdings: list,
                               nav: float, empty_text: str = "(empty)") -> None:
        if not holdings:
            widget.config(text=empty_text)
            return
        lines = []
        for h in sorted(holdings, key=lambda x: -abs(x["mkt_val"])):
            sign = "+" if h["upnl"] >= 0 else ""
            w = h["mkt_val"] / nav * 100 if nav > 0 else 0
            lines.append(
                f"{h['symbol']:5s} {h['shares']:4d}sh  "
                f"${h['mkt_px']:.2f}  {w:4.0f}%  "
                f"{sign}{h['upnl_pct']:.1f}%"
            )
        widget.config(text="\n".join(lines))

    # ── Donut chart ───────────────────────────────────────────

    def _render_pie_chart(self, holdings: list, nav: float, exposure: float,
                           other_val: float = 0) -> None:
        """Render matplotlib donut chart into self.chart_frame."""
        t = self.t

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

        # Clear old canvas (defensive — update() already cleared, but keep
        # this for callers who invoke _render_pie_chart directly)
        if self.chart_canvas:
            self.chart_canvas.get_tk_widget().destroy()
            self.chart_canvas = None

        sorted_h = sorted(holdings, key=lambda x: -abs(x["mkt_val"]))
        labels = [h["symbol"] for h in sorted_h]
        values = [abs(h["mkt_val"]) for h in sorted_h]
        total_mktval = sum(values)

        # Colors: ETF blues, Stock greens
        etf_blues = ["#3b82f6", "#60a5fa", "#93c5fd", "#bfdbfe",
                      "#2563eb", "#1d4ed8", "#1e40af", "#1e3a8a",
                      "#6366f1", "#818cf8", "#a5b4fc"]
        stock_greens = ["#22c55e", "#4ade80", "#86efac", "#bbf7d0",
                         "#16a34a", "#15803d", "#166534", "#14532d"]
        colors = []
        ei, si = 0, 0
        for h in sorted_h:
            if h["symbol"] in SR_ETFS:
                colors.append(etf_blues[ei % len(etf_blues)])
                ei += 1
            else:
                colors.append(stock_greens[si % len(stock_greens)])
                si += 1

        # Other manual holdings (grey)
        if other_val > 0:
            labels.append("Other")
            values.append(abs(other_val))
            total_mktval += abs(other_val)
            colors.append("#6b7280")

        # Cash (if exposure < 100%)
        if exposure < 100 and nav > total_mktval:
            cash = nav - total_mktval
            labels.append("Cash")
            values.append(cash)
            colors.append("#4b5563")

        fig, ax = plt.subplots(figsize=(3.6, 2.4), dpi=100)
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

        # Collect annotation info with y-overlap avoidance
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
                fontsize=7.5, fontweight="bold", color="white",
                arrowprops=dict(
                    arrowstyle="-",
                    color="#6b7d99",
                    lw=0.8,
                    connectionstyle=f"arc3,rad={0.1 * side}",
                ),
            )

        # Center text
        if exposure > 100:
            center_text = f"{exposure:.0f}%\nof NAV"
            center_color = t["orange"]
        else:
            center_text = f"{exposure:.0f}%\nExposure"
            center_color = t["green"]
        ax.text(center_x, 0, center_text, ha="center", va="center",
                fontsize=9, fontweight="bold", color=center_color)

        fig.tight_layout(pad=0.3)

        canvas = FigureCanvasTkAgg(fig, master=self.chart_frame)
        canvas.draw()
        canvas.get_tk_widget().pack()
        self.chart_canvas = canvas
        plt.close(fig)
