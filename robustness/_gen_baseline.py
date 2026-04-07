"""產生基準回測每日報酬序列，供 sub-agents 使用。"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path("C:/QuantCore-master")))

import pandas as pd
from robustness.utils import (
    run_sr_backtest, run_sp500_backtest, run_combo,
    load_spy_returns, compute_metrics, OUTPUT_ROOT,
)

print("Generating baseline returns...")
ret_sr = run_sr_backtest(quiet=True)
ret_sp = run_sp500_backtest(quiet=True)
combo = run_combo(ret_sr=ret_sr, ret_sp=ret_sp, alloc_sr=0.8, alloc_sp=0.5)
ret_spy = load_spy_returns()

# 對齊到組合的日期範圍
common = combo.index
df = pd.DataFrame({
    "SR":        ret_sr.reindex(common),
    "SP500":     ret_sp.reindex(common),
    "Combo_80_50_1.3x": combo.reindex(common),
    "SPY":       ret_spy.reindex(common),
})

out_csv = OUTPUT_ROOT / "baseline_returns.csv"
df.to_csv(out_csv)
print(f"Saved: {out_csv}")
print(f"Rows: {len(df)}, Range: {df.index[0].date()} -> {df.index[-1].date()}")

# 印出基準指標
print("\nBaseline metrics:")
for col in df.columns:
    m = compute_metrics(df[col].dropna())
    print(f"  {col:<22} CAGR {m['ann_ret']:+.1%}  Sharpe {m['sharpe']:.2f}  "
          f"MaxDD {m['max_dd']:.1%}  Calmar {m['calmar']:.2f}")
