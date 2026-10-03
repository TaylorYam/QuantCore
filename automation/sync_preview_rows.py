"""Sync Pos preview rows and log summary, shared by the Tk and Qt Panels.

No GUI imports: every function takes a SyncPlan and returns the text the
window shows.  Colors come back as theme key names ("blue", "green", "red",
"text") so each toolkit maps its own palette.
"""

from __future__ import annotations

from dataclasses import dataclass

from strategy_owner import SKEWLS, SR, etf_owner
from sync_position import SyncPlan

TRADE_COLUMNS = ("Action", "Symbol", "Qty", "Current", "Target",
                 "Est.Price", "Est.Value", "Strategy")
TRADE_COL_WIDTHS = (7, 7, 6, 8, 8, 10, 11, 8)


@dataclass(frozen=True)
class TargetSection:
    strategy: str    # "SR" / "SP500" / "SkewLS"
    title: str
    color: str
    lines: list[str]


@dataclass(frozen=True)
class HoldingRow:
    text: str
    tag: str
    tag_color: str
    mark: str        # "" when the symbol is in the target
    mark_color: str


@dataclass(frozen=True)
class PausedRow:
    symbol: str
    text: str
    reason: str


@dataclass(frozen=True)
class TradeRow:
    values: tuple[str, ...]  # one cell per TRADE_COLUMNS
    color: str               # action color


def plan_subtitle(plan: SyncPlan) -> str:
    text = f"NAV: ${plan.nav:,.0f}   |   SR: {plan.sr_date}   |   SP500: {plan.sp500_date}"
    if plan.ctx.alloc_skewls > 0:
        text += f"   |   SkewLS: {plan.skewls_date}"
    return text


def _pre_combine_shares(plan: SyncPlan, strategy: str, sym: str) -> int:
    """Strategy-only target shares (pre-combine): the integer [[Combine]]
    summed for this strategy, so the preview never re-rounds it (#184).
    0 when Combine skipped the symbol (no price)."""
    return plan.targets.get(sym, {}).get('strategy_shares', {}).get(strategy, 0)


def _overlap_note(plan: SyncPlan, sym: str, strat_sh: int, label: str) -> str:
    combined = plan.targets.get(sym, {}).get('shares', 0)
    if combined == strat_sh:
        return ""
    return f"  [{label}: {combined:+d}sh]"


def target_sections(plan: SyncPlan) -> list[TargetSection]:
    """Strategy Target (理論持倉), one section per strategy with weights."""
    ctx = plan.ctx
    prices = plan.prices
    sections = []
    if plan.sr_wgt:
        lines = []
        for sym, w in sorted(plan.sr_wgt.items(), key=lambda x: -x[1]):
            px = prices.get(sym, 0)
            t_shares = _pre_combine_shares(plan, "SR", sym)
            t_val = t_shares * px
            lines.append(
                f"  {sym:<6s}  {w*100:5.1f}%  →  {t_shares:4d}sh  @ ${px:.2f}  = ${t_val:,.0f}"
                f"{_overlap_note(plan, sym, t_shares, 'combined')}")
        sections.append(TargetSection(
            "SR", f"SR ({ctx.alloc_sr:.0%} of NAV)", "blue", lines))
    if plan.sp500_wgt:
        lines = []
        for sym, w in sorted(plan.sp500_wgt.items(), key=lambda x: -x[1]):
            px = prices.get(sym, 0)
            t_shares = _pre_combine_shares(plan, "SP500", sym)
            t_val = t_shares * px
            lines.append(
                f"  {sym:<6s}  {w*100:5.1f}%  →  {t_shares:4d}sh  @ ${px:.2f}  = ${t_val:,.0f}"
                f"{_overlap_note(plan, sym, t_shares, 'combined')}")
        sections.append(TargetSection(
            "SP500", f"SP500 Momentum ({ctx.alloc_sp500:.0%} of NAV)", "green", lines))
    # SkewLS targets (signed: 含 long inverse ETFs + XLC short)
    if plan.skewls_wgt:
        lines = []
        for sym, w in sorted(plan.skewls_wgt.items(), key=lambda x: -abs(x[1])):
            px = prices.get(sym, 0)
            t_shares = _pre_combine_shares(plan, "SkewLS", sym)
            t_val = t_shares * px
            direction = "SHORT" if w < 0 else "LONG "
            lines.append(
                f"  {direction} {sym:<6s}  {w*100:+5.1f}%  →  {t_shares:+4d}sh  @ ${px:.2f}  = ${t_val:+,.0f}"
                f"{_overlap_note(plan, sym, t_shares, 'combined')}")
        sections.append(TargetSection(
            "SkewLS", f"Skew LS ({ctx.alloc_skewls:.0%} of NAV)", "red", lines))
    return sections


def _strategy_symbols(plan: SyncPlan) -> list[str]:
    return [s for s in plan.current_pos if s not in plan.current_other_holdings]


# SR+SkewLS（兩邊都可能有）不屬於單一 Strategy，用一般文字色
_TAG_COLOR = {SR: "blue", SKEWLS: "red", "SP500": "green"}


def current_holding_rows(plan: SyncPlan) -> list[HoldingRow]:
    """Current Holdings (實際持倉) managed by the strategies."""
    alloc = (plan.ctx.alloc_sr, plan.ctx.alloc_skewls)
    rows = []
    for sym in sorted(_strategy_symbols(plan)):
        shares = int(plan.current_pos[sym])
        px = plan.prices.get(sym, 0)
        val = shares * px
        # 歸屬規則與 Portfolio 相同（strategy_owner.etf_owner，#187）；其餘是 SP500
        tag = etf_owner(sym, shares, alloc) or "SP500"
        tag_color = _TAG_COLOR.get(tag, "text")
        in_target = sym in plan.targets
        rows.append(HoldingRow(
            text=f"  {sym:<6s}  {shares:4d}sh  @ ${px:.2f}  = ${val:,.0f}",
            tag=tag, tag_color=tag_color,
            mark="" if in_target else "  ← not in target",
            mark_color="text" if in_target else "red",
        ))
    return rows


def other_holding_lines(plan: SyncPlan) -> list[str]:
    """Other (excluded from sync) — current_other_holdings."""
    lines = []
    for sym in sorted(plan.current_other_holdings):
        shares = int(plan.current_pos[sym])
        px = plan.prices.get(sym, 0)
        lines.append(f"  {sym:<6s}  {shares:4d}sh  @ ${px:.2f}  = ${shares * px:,.0f}")
    return lines


def paused_rows(plan: SyncPlan) -> list[PausedRow]:
    """暫停標的與原因；這些單不在交易清單、不會送出（#106）。"""
    held = {o.symbol: o for o in plan.paused_orders}
    rows = []
    for sym, reason in sorted(plan.paused.items()):
        o = held.get(sym)
        order_text = (f"  原本要 {o.action} {o.qty}sh ({o.current}→{o.target})"
                      if o else "  本次沒有對應的策略單")
        rows.append(PausedRow(sym, f"  {sym:<6s}{order_text}", f"    {reason}"))
    return rows


def trades_heading(plan: SyncPlan) -> str:
    return f"Required Trades (需要交易: {len(plan.orders)} 筆)"


def _sells_then_buys(plan: SyncPlan):
    return ([o for o in plan.orders if o.action == 'SELL']
            + [o for o in plan.orders if o.action == 'BUY'])


def trade_rows(plan: SyncPlan) -> list[TradeRow]:
    """Trade table rows: SELL 先，BUY 後。"""
    return [
        TradeRow((
            o.action, o.symbol, str(o.qty),
            str(o.current), str(o.target),
            f"${o.price:.2f}", f"${o.qty * o.price:,.0f}",
            o.strategy,
        ), "red" if o.action == 'SELL' else "green")
        for o in _sells_then_buys(plan)
    ]


def trade_totals_text(plan: SyncPlan) -> str:
    total_sell = 0.0
    total_buy = 0.0
    for o in plan.orders:
        if o.action == 'SELL':
            total_sell += o.qty * o.price
        else:
            total_buy += o.qty * o.price
    net = total_buy - total_sell
    net_sign = "+" if net >= 0 else ""
    return f"SELL: ${total_sell:,.0f}   |   BUY: ${total_buy:,.0f}   |   Net: {net_sign}${net:,.0f}"


def confirm_prompt(plan: SyncPlan) -> str:
    return (f"確定送出 {len(plan.orders)} 筆限價單到 [{plan.ctx.name}]？\n"
            f"(LMT ±0.3% buffer, GTC, outside RTH)")


def sending_log_line(plan: SyncPlan) -> str:
    return f"[>] Sending {len(plan.orders)} LMT orders for [{plan.ctx.name}]..."


def log_summary_lines(plan: SyncPlan) -> list[str]:
    """The plan summary written to the Panel's Output Log."""
    ctx = plan.ctx
    prices = plan.prices
    targets = plan.targets
    lines = [f"{'─' * 40}", "  Strategy Target (理論持倉)"]

    def note(sym: str, strat_sh: int) -> str:
        return _overlap_note(plan, sym, strat_sh, "combined w/ overlap")

    if plan.sr_wgt:
        lines.append(f"  SR ({ctx.alloc_sr:.0%} of NAV):")
        for sym, w in sorted(plan.sr_wgt.items(), key=lambda x: -x[1]):
            px = prices.get(sym, 0)
            t_sh = _pre_combine_shares(plan, "SR", sym)
            lines.append(f"    {sym:<6s} {w*100:5.1f}%  → {t_sh:4d}sh @ ${px:.2f}"
                         f"{note(sym, t_sh)}")
    if plan.sp500_wgt:
        lines.append(f"  SP500 ({ctx.alloc_sp500:.0%} of NAV):")
        for sym, w in sorted(plan.sp500_wgt.items(), key=lambda x: -x[1]):
            px = prices.get(sym, 0)
            t_sh = _pre_combine_shares(plan, "SP500", sym)
            lines.append(f"    {sym:<6s} {w*100:5.1f}%  → {t_sh:4d}sh @ ${px:.2f}"
                         f"{note(sym, t_sh)}")
    if plan.skewls_wgt:
        lines.append(f"  SkewLS ({ctx.alloc_skewls:.0%} of NAV):")
        for sym, w in sorted(plan.skewls_wgt.items(), key=lambda x: -abs(x[1])):
            px = prices.get(sym, 0)
            t_sh = _pre_combine_shares(plan, "SkewLS", sym)
            direction = "SHORT" if w < 0 else "LONG "
            lines.append(f"    {direction} {sym:<6s} {w*100:+5.1f}%  → {t_sh:+4d}sh @ ${px:.2f}"
                         f"{note(sym, t_sh)}")

    lines.append("  Current Holdings (實際持倉):")
    for sym in sorted(_strategy_symbols(plan)):
        sh = int(plan.current_pos[sym])
        px = prices.get(sym, 0)
        in_t = "" if sym in targets else " ← NOT in target"
        lines.append(f"    {sym:<6s} {sh:4d}sh @ ${px:.2f}{in_t}")

    lines.append(f"  Required Trades: {len(plan.orders)} 筆")
    for o in plan.orders:
        lines.append(f"    {o.action:<4s} {o.qty:4d}sh {o.symbol:<6s} "
                     f"({o.current}→{o.target})  ~${o.qty * o.price:,.0f}  "
                     f"[{o.strategy}]")
    if not plan.orders:
        lines.append("    Already on target")
    for sym, reason in sorted(plan.paused.items()):
        lines.append(f"  ⏸ {sym} 策略單暫停：{reason}")
    lines.append(f"{'─' * 40}")
    return lines
