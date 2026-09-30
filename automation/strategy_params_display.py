"""Strategy Parameters display shared by the Tk and Qt Panels (#150).

No GUI imports: takes the three Allocation values of one Account and returns the
text each Panel shows. Colors come back as theme key names ("blue", "green",
"red", "orange") so each toolkit maps its own palette.

Leverage is always derived as the sum of the Allocations (CONTEXT「Leverage」);
the `leverage` field in the config is never read here.
"""

from __future__ import annotations

from dataclasses import dataclass

# (config key, label, default, theme key) — defaults match the Tk Panel sliders
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
