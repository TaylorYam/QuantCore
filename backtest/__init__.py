"""
QuantCore — Backtest Layer
"""

from .metrics  import compute, annual_returns, print_table
from .combo    import build_all, to_equity_curves
from .charts   import plot_equity_curves
from .leverage import apply_leverage, ib_blended_rate, leverage_summary

__all__ = [
    "compute", "annual_returns", "print_table",
    "build_all", "to_equity_curves",
    "plot_equity_curves",
    "apply_leverage", "ib_blended_rate", "leverage_summary",
]
