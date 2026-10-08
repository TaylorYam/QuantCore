"""Strategy Parameters display for the Panel (#150).

No GUI imports: takes the three Allocation values of one Account and returns the
text each Panel shows. Colors come back as theme key names ("blue", "green",
"red", "orange") so each toolkit maps its own palette. `allocation_change` checks
an Allocation edit and builds the confirmation text before a Panel saves it
(#220, the rules of the former Tk Panel Save).

Leverage is always derived as the sum of the Allocations (CONTEXT「Leverage」);
the `leverage` field in the config is never read here.
"""

from __future__ import annotations

from dataclasses import dataclass

# Saving is refused above this total (kept from the Tk Panel)
MAX_TOTAL_ALLOCATION = 2.5

# (config key, label, default, theme key) — defaults kept from the Tk Panel sliders
STRATEGIES = (
    ("alloc_sr", "SR", 0.80, "blue"),
    ("alloc_sp500", "SP500", 0.50, "green"),
    ("alloc_skewls", "Skew LS", 0.0, "red"),
)


@dataclass(frozen=True)
class AllocationRow:
    key: str
    label: str
    value: float
    text: str
    color: str


@dataclass(frozen=True)
class StrategyParams:
    rows: tuple[AllocationRow, ...]
    leverage_text: str
    leverage_color: str
    detail: str


def allocations_from_cfg(cfg: dict) -> tuple[float, float, float]:
    """(SR, SP500, Skew LS) Allocation from an account config dict."""
    return tuple(cfg.get(key, default) for key, _, default, _ in STRATEGIES)


def strategy_params(alloc_sr: float, alloc_sp500: float,
                    alloc_skewls: float = 0.0) -> StrategyParams:
    values = (alloc_sr, alloc_sp500, alloc_skewls)
    rows = tuple(AllocationRow(key, label, v, f"{v:.0%}", color)
                 for (key, label, _, color), v in zip(STRATEGIES, values))
    total = alloc_sr + alloc_sp500 + alloc_skewls
    if total > 1.0:
        detail, color = f"Borrow: {total - 1.0:.0%}", "orange"
    else:
        detail, color = "No leverage", "green"
    return StrategyParams(rows, f"{total:.0%} ({total:.1f}x)", color, detail)


@dataclass(frozen=True)
class AllocationChange:
    """One Account's Allocation edit, checked before saving."""
    account: str
    values: tuple[float, float, float]
    changed: bool
    error: str                  # non-empty = refused, do not save
    first_enable_skewls: bool   # Skew LS goes from 0 to above 0

    def updates(self) -> dict:
        """The accounts.json override fields to write for this Account."""
        return {key: v for (key, *_), v in zip(STRATEGIES, self.values)}

    def confirm_text(self) -> str:
        params = strategy_params(*self.values)
        lines = [f"儲存 [{self.account}] 的策略參數？", ""]
        for row in params.rows:
            first = row.key == "alloc_skewls" and self.first_enable_skewls
            note = "  ← 首次啟用（會放空 XLC）" if first else ""
            lines.append(f"  {row.label}：{row.text}{note}")
        lines.append(f"  合計 Leverage：{params.leverage_text}（{params.detail}）")
        lines += ["", "只改這個帳戶的設定。"]
        if self.first_enable_skewls:
            lines += ["", "注意：Skew LS 含 XLC 放空部位，請確認這個 IB 帳戶能放空（margin 帳戶）。"]
        return "\n".join(lines)


def allocation_error(values: tuple[float, float, float]) -> str:
    """Why these Allocations cannot be saved; empty string when they can."""
    total = round(sum(round(v, 2) for v in values), 2)
    if total > MAX_TOTAL_ALLOCATION:
        return f"合計 {total:.0%} 超過上限 {MAX_TOTAL_ALLOCATION:.0%}，不能儲存。"
    return ""


def allocation_change(account: str, cfg_before: dict,
                      values: tuple[float, float, float]) -> AllocationChange:
    """Check new (SR, SP500, Skew LS) Allocations against the Account's current config."""
    new = tuple(round(v, 2) for v in values)
    old = tuple(round(v, 2) for v in allocations_from_cfg(cfg_before))
    return AllocationChange(account, new, new != old, allocation_error(new),
                            first_enable_skewls=old[2] == 0.0 and new[2] > 0.0)
