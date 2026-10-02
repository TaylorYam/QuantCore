"""Portfolio display calculations shared by the Tk and Qt Panels (#149).

No GUI imports: every function takes the portfolio dict (`ib_fetcher.fetch_portfolio`
format) or a `portfolio_queries.QueryState` and returns the text each Panel shows.
Colors come back as theme key names ("text", "text_dim", "green", "red",
"yellow", "orange") so each toolkit maps its own palette; the donut slice
colors are the chart's own fixed palette.
"""

from __future__ import annotations

from dataclasses import dataclass

from ib_fetcher import ETF_TICKERS
from strategy_owner import etf_owner

TICKER_NAMES: dict[str, str] = {
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


# ── Metric cards ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class Metric:
    key: str
    title: str
    text: str
    color: str


METRIC_TITLES = (
    ("nav", "Net Asset Value", "$—"),
    ("pnl", "Unrealized P&L", "$—"),
    ("margin", "Margin Cushion", "—%"),
    ("strat_exp", "Strategy Exposure", "—%"),
    ("total_exp", "Account Exposure", "—%"),
)


def exposures(pf: dict) -> tuple[float, float]:
    """(Strategy exposure %, Account exposure %) of NAV; 0 when NAV is not positive."""
    nav = pf["nav"]
    mktval = pf["total_mktval"]
    strategy_mktval = pf.get("strategy_mktval", mktval)
    if nav <= 0:
        return 0, 0
    return strategy_mktval / nav * 100, mktval / nav * 100


def metrics(pf: dict) -> list[Metric]:
    if not pf.get("available"):
        return [Metric(key, title, empty, "text_dim") for key, title, empty in METRIC_TITLES]

    upnl = pf["total_upnl"]
    cushion = pf["cushion"]
    strat_exposure, total_exposure = exposures(pf)
    sign = "+" if upnl >= 0 else ""
    if cushion < 10:
        m_color = "red"
    elif cushion < 25:
        m_color = "yellow"
    else:
        m_color = "green"
    values = {
        "nav": (f"${pf['nav']:,.0f}", "text"),
        "pnl": (f"{sign}${upnl:,.2f}", "green" if upnl >= 0 else "red"),
        "margin": (f"{cushion:.1f}%", m_color),
        "strat_exp": (f"{strat_exposure:.0f}%", "orange" if strat_exposure > 100 else "green"),
        "total_exp": (f"{total_exposure:.0f}%", "orange" if total_exposure > 100 else "green"),
    }
    return [Metric(key, title, *values[key]) for key, title, _ in METRIC_TITLES]


# ── Report date / query state ────────────────────────────────────────

def report_line(pf: dict) -> str:
    """Report date with the source tag for anything that isn't the Excel snapshot."""
    if not pf.get("available"):
        return f"Report: {pf.get('error', 'No data yet')}"
    source = pf.get("source", "Excel")
    src_tag = f"  [{source}]" if source != "Excel" else ""
    return f"Report Date: {pf['date']}{src_tag}"


def _last_updated(state) -> str:
    return (state.updated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            if state.updated_at else "—")


def refresh_summary(name: str, state) -> tuple[str, str]:
    """Per-account refresh summary: source (Manual/Auto), Updating/Failed, last time."""
    source = {"manual": "Manual", "auto": "Auto"}.get(state.refresh_source, "Unknown source")
    detail = source if state.updated_at else "No data yet"
    if state.running:
        detail += " · Updating"
    elif state.error:
        detail += " · Failed"
    return f"{name} | {detail}\n{_last_updated(state)}", "yellow" if state.error else "text"


def status_line(state) -> tuple[str, str]:
    """Report line after a query state update: OK/Updating/Failed | time (source)."""
    status = "Updating" if state.running else "Failed" if state.error else "OK"
    source_tag = f" ({state.refresh_source})" if state.refresh_source else ""
    return (f"{status} | {_last_updated(state)}{source_tag}",
            "yellow" if state.error else "text_dim")


# ── Holdings rows ────────────────────────────────────────────────────

HOLDING_HEADERS = ("UL", "SH", "Wt%", "Cost", "Price", "P&L", "P&L%", "Name")
# portfolio dict key per group, in display order (ETF / Stock / Other)
HOLDING_GROUPS = (("etf", "etf_holdings"), ("stock", "stock_holdings"),
                  ("other", "other_holdings"))
MANUAL = "手動"


def fmt_dollar(val: float) -> str:
    if val > 0:
        return f"+${val:,.0f}"
    elif val < 0:
        return f"-${abs(val):,.0f}"
    return "$0"


def owner_of(symbol: str, shares: float, group: str,
             manual: frozenset | set | dict = frozenset(),
             alloc: tuple[float, float] | None = None) -> str:
    """Which Strategy a holding belongs to (CONTEXT「手動持倉」).

    Holdings outside every Strategy (the "other" bucket) are manual, and so are
    `manual` symbols: manual holdings that collided with a Strategy Universe
    (`manual_attribution.paused_symbols`). ETFs follow `strategy_owner.etf_owner`
    (same rule as the daily snapshot): long sector ETFs go by the account's
    (alloc_sr, alloc_skewls), since SkewLS's long leg buys them too (#181).
    Universe stocks are SP500.
    """
    if group == "other" or symbol in manual:
        return MANUAL
    if group == "etf":
        return etf_owner(symbol, shares, alloc) or "SR"
    return "SP500"


@dataclass(frozen=True)
class HoldingRow:
    group: str          # "etf" / "stock" / "other"
    owner: str          # "SR" / "SkewLS" / "SP500" / "手動"
    symbol: str
    shares: float
    weight_pct: float
    avg_cost: float
    mkt_px: float
    mkt_val: float
    upnl: float
    upnl_pct: float
    name: str
    manual_reason: str = ""   # why a Strategy-Universe symbol counts as manual

    def cells(self) -> tuple[str, ...]:
        """One text cell per HOLDING_HEADERS."""
        sign = "+" if self.upnl_pct >= 0 else ""
        return (
            self.symbol,
            f"{self.shares}",
            f"{self.weight_pct:.0f}%",
            f"${self.avg_cost:,.2f}",
            f"${self.mkt_px:,.2f}",
            fmt_dollar(self.upnl),
            f"{sign}{self.upnl_pct:.1f}%",
            self.name,
        )


def holding_rows(holdings: list, nav: float, group: str,
                 manual: dict[str, str] | None = None,
                 alloc: tuple[float, float] | None = None) -> list[HoldingRow]:
    """One group's rows, largest |market value| first.

    manual: collided manual holdings, symbol → pause reason (see owner_of).
    alloc: the account's (alloc_sr, alloc_skewls); None keeps long sector ETFs as SR.
    """
    manual = manual or {}
    rows = []
    for h in sorted(holdings, key=lambda x: -abs(x["mkt_val"])):
        rows.append(HoldingRow(
            group=group,
            owner=owner_of(h["symbol"], h["shares"], group, manual, alloc),
            symbol=h["symbol"],
            shares=h["shares"],
            weight_pct=h["mkt_val"] / nav * 100 if nav > 0 else 0,
            avg_cost=h.get("avg_cost", 0),
            mkt_px=h["mkt_px"],
            mkt_val=h["mkt_val"],
            upnl=h.get("upnl", 0),
            upnl_pct=h.get("upnl_pct", 0),
            name=h.get("name") or TICKER_NAMES.get(h["symbol"], ""),
            manual_reason=manual.get(h["symbol"], "") if group != "other" else "",
        ))
    return rows


def all_holding_rows(pf: dict) -> list[HoldingRow]:
    if not pf.get("available"):
        return []
    nav = pf["nav"]
    manual = pf.get("manual_symbols") or {}
    alloc = pf.get("strategy_alloc")
    return [row for group, key in HOLDING_GROUPS
            for row in holding_rows(pf.get(key, []), nav, group, manual,
                                    tuple(alloc) if alloc else None)]


def holdings_text(rows: list[HoldingRow]) -> tuple[str, str]:
    """(header, body) as fixed-width text columns; ("", "") when there are no rows."""
    if not rows:
        return "", ""
    cells = [r.cells() for r in rows]
    widths = [len(h) for h in HOLDING_HEADERS]
    for r in cells:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))

    def _fmt_row(row):
        parts = [f"{row[0]:<{widths[0]}}"]
        for i in range(1, 7):
            parts.append(f"{row[i]:>{widths[i]}}")
        parts.append("")
        parts.append(f"{row[7]:<{widths[7]}}")
        return "  ".join(parts)

    return _fmt_row(HOLDING_HEADERS), "\n".join(_fmt_row(r) for r in cells)


# ── Donut chart data ─────────────────────────────────────────────────

MAX_DONUT_LABELS = 12
_ETF_BLUES = ["#3b82f6", "#60a5fa", "#93c5fd", "#bfdbfe",
              "#2563eb", "#1d4ed8", "#1e40af", "#1e3a8a",
              "#6366f1", "#818cf8", "#a5b4fc"]
_STOCK_GREENS = ["#22c55e", "#4ade80", "#86efac", "#bbf7d0",
                 "#16a34a", "#15803d", "#166534", "#14532d"]
MORE_COLOR = "#9ca3af"
OTHER_COLOR = "#6b7280"
CASH_COLOR = "#4b5563"


@dataclass(frozen=True)
class Slice:
    label: str
    value: float        # absolute market value
    color: str
    pct_of_nav: float


@dataclass(frozen=True)
class Donut:
    slices: list[Slice]
    exposure: float     # Strategy exposure %
    center: tuple[str, str]   # (text, theme color key)


def donut_slices(holdings: list, nav: float, exposure: float,
                 other_val: float = 0) -> list[Slice]:
    """Top 12 strategy holdings, "+N more", manual "Other", then Cash (only under 100%)."""
    sorted_h = sorted(holdings, key=lambda x: -abs(x["mkt_val"]))
    total_mktval = sum(abs(h["mkt_val"]) for h in sorted_h)
    top_h = sorted_h[:MAX_DONUT_LABELS]
    rest_h = sorted_h[MAX_DONUT_LABELS:]

    parts: list[tuple[str, float, str]] = []
    ei = si = 0
    for h in top_h:
        if h["symbol"] in ETF_TICKERS:
            color = _ETF_BLUES[ei % len(_ETF_BLUES)]
            ei += 1
        else:
            color = _STOCK_GREENS[si % len(_STOCK_GREENS)]
            si += 1
        parts.append((h["symbol"], abs(h["mkt_val"]), color))
    if rest_h:
        parts.append((f"+{len(rest_h)} more", sum(abs(h["mkt_val"]) for h in rest_h),
                      MORE_COLOR))
    if other_val > 0:
        parts.append(("Other", abs(other_val), OTHER_COLOR))
        total_mktval += abs(other_val)
    if exposure < 100 and nav > total_mktval:
        parts.append(("Cash", nav - total_mktval, CASH_COLOR))
    return [Slice(label, value, color, value / nav * 100 if nav > 0 else 0)
            for label, value, color in parts]


def donut_center(exposure: float) -> tuple[str, str]:
    if exposure > 100:
        return f"{exposure:.0f}%\nof NAV", "orange"
    return f"{exposure:.0f}%\nExposure", "green"


def donut(pf: dict) -> Donut | None:
    """Donut data for a portfolio; None when there is nothing to draw."""
    if not pf.get("available"):
        return None
    nav = pf["nav"]
    holdings = pf.get("etf_holdings", []) + pf.get("stock_holdings", [])
    other_val = pf.get("other_mktval", 0)
    if not (holdings or other_val > 0) or nav <= 0:
        return None
    exposure, _ = exposures(pf)
    return Donut(donut_slices(holdings, nav, exposure, other_val), exposure,
                 donut_center(exposure))
