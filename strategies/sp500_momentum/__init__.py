"""
SP500 Momentum Strategy
========================
快速 import 入口：

    from strategies.sp500_momentum import run, SP500Backtester, SelectionEngine
"""

from .backtester import SP500Backtester
from .selection  import SelectionEngine
from .run_strategy import run
from .           import config

__all__ = [
    "SP500Backtester",
    "SelectionEngine",
    "run",
    "config",
]
