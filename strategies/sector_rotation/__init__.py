"""
Sector Rotation MOM Strategy
==============================
快速 import 入口：

    from strategies.sector_rotation import load_data, run_full_backtest, get_latest_weights
"""

from .data_loader import load_data
from .signal      import compute_mom_score, compute_mom_weights, get_latest_weights, get_rebal_schedule
from .backtest    import run_backtest, run_full_backtest
from .            import config

__all__ = [
    "load_data",
    "compute_mom_score",
    "compute_mom_weights",
    "get_latest_weights",
    "get_rebal_schedule",
    "run_backtest",
    "run_full_backtest",
    "config",
]
