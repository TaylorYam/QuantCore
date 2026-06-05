"""
測試 2：加速度修正 vs 原始排名 回測比較
──────────────────────────────────────
目的：用加速度修正後的分數重新排名，比較回測績效差異。

做法（不改動現有程式碼）：
1. 包裝原始 SelectionEngine，覆寫 scan_market 回傳加速度修正後的排名
2. 用同一套 SP500Backtester 跑回測
3. 比較不同參數組合的績效

測試參數組合：
- 加速度窗口: 5, 10, 20 天
- 加速度權重: 0.2, 0.5, 1.0
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import copy
import numpy as np
import pandas as pd
from strategies.sp500_momentum.selection import SelectionEngine
from strategies.sp500_momentum.backtester import SP500Backtester
from strategies.sp500_momentum import config, utils


class AccelSelectionEngine(SelectionEngine):
    """加速度修正版 SelectionEngine — 包裝原始引擎，不改動原始程式碼。"""

    def __init__(self, accel_gap: int = 5, accel_weight: float = 1.0,
                 data_cache: dict = None):
        super().__init__(data_cache=data_cache)
        self.accel_gap = accel_gap
        self.accel_weight = accel_weight
        self._trading_days = self._load_trading_days()

    def _load_trading_days(self) -> pd.DatetimeIndex:
        ref_path = config.REF_DIR / config.TRADING_DAYS_FILE
        df = pd.read_excel(ref_path)
        dates = pd.to_datetime(df["date"])
        return pd.DatetimeIndex(sorted(dates))

    def _get_prev_date(self, date: pd.Timestamp) -> pd.Timestamp | None:
        idx = self._trading_days.get_indexer([date], method="pad")[0]
        if idx < self.accel_gap:
            return None
        return self._trading_days[idx - self.accel_gap]

    def scan_market(self, date: pd.Timestamp, lookback: int = None) -> list[dict]:
        """加速度修正版 scan_market。"""
        if lookback is None:
            lookback = config.LOOKBACK

        # 用 cache key 包含加速度參數，避免和原始 cache 衝突
        cache_key = (date, lookback, self.accel_gap, self.accel_weight)
        if cache_key in self.scan_cache:
            return self.scan_cache[cache_key]

        # 取得原始排名
        ranked_now = super().scan_market(date, lookback)

        # 取得 gap 天前的排名
        prev_date = self._get_prev_date(date)
        if prev_date is None:
            self.scan_cache[cache_key] = ranked_now
            return ranked_now

        ranked_prev = super().scan_market(prev_date, lookback)
        prev_map = {r["ticker"]: r["adj_slope"] for r in ranked_prev}

        # 加速度修正
        adjusted = []
        for r in ranked_now:
            r_copy = dict(r)  # 不改動原始 dict
            slope_now = r["adj_slope"]
            slope_prev = prev_map.get(r["ticker"])

            if slope_prev is not None and abs(slope_now) > 1e-8 and slope_now > -900:
                delta = slope_now - slope_prev
                ratio = np.clip(delta / abs(slope_now), -1.0, 1.0)
                r_copy["adj_slope_original"] = slope_now
                r_copy["accel_ratio"] = ratio
                r_copy["adj_slope"] = slope_now * (1 + self.accel_weight * ratio)
            else:
                r_copy["adj_slope_original"] = slope_now
                r_copy["accel_ratio"] = 0.0

            adjusted.append(r_copy)

        # 重新依修正後的 adj_slope 排名
        adjusted.sort(key=lambda x: x["adj_slope"], reverse=True)
        self.scan_cache[cache_key] = adjusted
        return adjusted


def run_comparison_backtest():
    print("=" * 70)
    print("測試 2：加速度修正 vs 原始排名 回測比較")
    print("=" * 70)

    # ── 參數組合 ─────────────────────────────────────────────────
    param_grid = [
        {"accel_gap": 5,  "accel_weight": 0.2},
        {"accel_gap": 5,  "accel_weight": 0.5},
        {"accel_gap": 5,  "accel_weight": 1.0},
        {"accel_gap": 10, "accel_weight": 0.2},
        {"accel_gap": 10, "accel_weight": 0.5},
        {"accel_gap": 10, "accel_weight": 1.0},
        {"accel_gap": 20, "accel_weight": 0.2},
        {"accel_gap": 20, "accel_weight": 0.5},
        {"accel_gap": 20, "accel_weight": 1.0},
    ]

    start_date = "2019-01-01"
    end_date = None  # 到最新資料

    # ── 1. 跑原始（baseline）回測 ────────────────────────────────
    print("\n▸ 執行 Baseline 回測（原始 adj_slope 排名）...")
    selector_base = SelectionEngine()
    selector_base.preload_all_data(start_date, end_date)

    spy_df = utils.load_stock(config.ETF_DIR / "SPY.csv")
    sso_df = utils.load_stock(config.ETF_DIR / "SSO.csv")

    bt_base = SP500Backtester(
        start_date=start_date,
        end_date=end_date,
        selector=selector_base,
        spy_df=spy_df,
        sso_df=sso_df,
        write_reports=False,
    )
    base_result = bt_base.run()

    results = [{
        "label": "Baseline (原始)",
        "accel_gap": "-",
        "accel_weight": "-",
        **_extract_metrics(base_result),
    }]

    # ── 2. 跑各參數組合的加速度修正回測 ──────────────────────────
    for params in param_grid:
        gap = params["accel_gap"]
        weight = params["accel_weight"]
        label = f"gap={gap}, w={weight}"
        print(f"\n▸ 執行加速度修正回測: {label}...")

        selector_accel = AccelSelectionEngine(
            accel_gap=gap,
            accel_weight=weight,
            data_cache=selector_base.data_cache,  # 共享資料 cache 加速
        )
        # 不需要 preload，已共享 data_cache

        bt_accel = SP500Backtester(
            start_date=start_date,
            end_date=end_date,
            selector=selector_accel,
            spy_df=spy_df,
            sso_df=sso_df,
            write_reports=False,
        )
        accel_result = bt_accel.run()

        results.append({
            "label": label,
            "accel_gap": gap,
            "accel_weight": weight,
            **_extract_metrics(accel_result),
        })

    # ── 3. 輸出比較表 ────────────────────────────────────────────
    df_results = pd.DataFrame(results)
    print("\n" + "=" * 70)
    print("回測績效比較")
    print("=" * 70)
    print(df_results.to_string(index=False))

    output_dir = os.path.join(os.path.dirname(__file__), "output")
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "backtest_comparison.csv")
    df_results.to_csv(path, index=False)
    print(f"\n已輸出: {path}")


def _extract_metrics(bt: "SP500Backtester") -> dict:
    """從回測引擎物件中提取關鍵績效指標。"""
    metrics = {}

    eq_series = bt.get_equity_series()
    if eq_series.empty:
        return metrics

    initial = bt.initial_capital
    final = eq_series.iloc[-1]
    metrics["final_equity"] = round(final, 0)
    metrics["total_return"] = round((final / initial - 1), 4)

    # CAGR
    days = (eq_series.index[-1] - eq_series.index[0]).days
    if days > 0:
        metrics["cagr"] = round((final / initial) ** (365.25 / days) - 1, 4)

    # Max Drawdown
    running_max = eq_series.cummax()
    dd = (eq_series - running_max) / running_max
    metrics["max_drawdown"] = round(dd.min(), 4)

    # Sharpe (annualized, daily returns)
    daily_ret = eq_series.pct_change().dropna()
    if len(daily_ret) > 30 and daily_ret.std() > 0:
        metrics["sharpe"] = round(
            daily_ret.mean() / daily_ret.std() * np.sqrt(252), 4
        )

    # Trade stats
    metrics["total_trades"] = len(bt.trades)

    return metrics


if __name__ == "__main__":
    run_comparison_backtest()
