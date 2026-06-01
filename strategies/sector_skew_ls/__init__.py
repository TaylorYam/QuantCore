"""
Sector Skew Long-Short Strategy
==============================
偏態因子多空策略：用 11 SPDR 板塊 ETF 的滾動報酬偏態作為訊號，
top 5 long、bottom 5 short；空頭透過反向 ETF 代理（10 檔）+ XLC 直接放空實現。

快速 import 入口：

    from strategies.sector_skew_ls import (
        load_data, run_backtest_proxy,
        compute_long_short_weights, to_inverse_proxy_weights,
        config,
    )

Live signal 入口（S16 完成後）：

    from automation.signals import get_skewls_weights
"""

from .data_loader import load_data
from .signal      import (
    compute_skew_score,
    compute_vix_regime,
    compute_long_short_weights,
    to_inverse_proxy_weights,
    get_latest_weights_live,
)
from .backtest    import run_backtest_proxy
from .            import config

__all__ = [
    "load_data",
    "compute_skew_score",
    "compute_vix_regime",
    "compute_long_short_weights",
    "to_inverse_proxy_weights",
    "get_latest_weights_live",
    "run_backtest_proxy",
    "config",
]
