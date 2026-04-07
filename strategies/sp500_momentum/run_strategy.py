"""
SP500 Momentum — 執行入口
"""

from . import config
from .selection  import SelectionEngine
from .backtester import SP500Backtester
from . import utils


def run(start_date: str = None,
        end_date:   str = None,
        write_reports: bool = True,
        dip_ticker_map: dict = None) -> SP500Backtester:
    """
    一站式回測執行。

    Returns
    -------
    SP500Backtester（已完成 .run()，可呼叫 .get_equity_series() 或 .save_results()）
    """
    print("=" * 60)
    print("SP500 Momentum Strategy")
    print(f"  交易成本：{config.COMMISSION*100:.4f}% 單向")
    print(f"  目標持股：{config.TARGET_HOLDINGS} 支")
    print(f"  再平衡：每{'一二三四五'[config.REBALANCE_WEEKS-1]}週{'一二三四五'[config.REBALANCE_WEEKDAY]}")
    print("=" * 60)

    # 預載入 benchmark
    spy_df = utils.load_benchmark(config.ETF_DIR / "SPY.csv")
    sso_df = utils.load_benchmark(config.ETF_DIR / "SSO.csv")

    # 共用 SelectionEngine（資料只載入一次）
    selector = SelectionEngine()

    bt = SP500Backtester(
        start_date     = start_date,
        end_date       = end_date,
        initial_capital= config.INITIAL_CASH,
        compounding    = True,
        selector       = selector,
        spy_df         = spy_df,
        sso_df         = sso_df,
        write_reports  = write_reports,
        dip_ticker_map = dip_ticker_map,
    )
    bt.run()

    eq = bt.get_equity_series()
    if not eq.empty:
        import numpy as np
        ret  = eq.pct_change().dropna()
        n    = len(ret)
        cagr = (1 + ret).prod() ** (252 / n) - 1
        vol  = ret.std() * np.sqrt(252)
        dd   = (eq / eq.cummax() - 1).min()
        print(f"\n  區間：{eq.index[0].date()} → {eq.index[-1].date()}")
        print(f"  年化報酬：{cagr:+.1%}   波動：{vol:.1%}   "
              f"Sharpe：{cagr/vol:.2f}   MaxDD：{dd:.1%}")
        print(f"  最終資產：${eq.iloc[-1]:,.0f}")

    return bt


if __name__ == "__main__":
    bt = run(write_reports=True)
    bt.save_results()
