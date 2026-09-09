"""
停利研究附錄 — 進場濾網 2×2 因子矩陣
================================================================
比較兩道「過熱/噴出」進場濾網的獨立貢獻與交互作用:

  F0_neither   MAX_ADJ_SLOPE=None + 無 kurt 封鎖   (裸基準,歸因用)
  F1_adjslope  MAX_ADJ_SLOPE=1.5  + 無 kurt 封鎖   (= 現行 live 行為 = A0)
  F2_kurt      MAX_ADJ_SLOPE=None + kurt60>P90 封鎖
  F3_both      MAX_ADJ_SLOPE=1.5  + kurt60>P90 封鎖 (= D1_block_kurt60_P90)

MAX_ADJ_SLOPE 以 runtime 屬性翻轉(rotation_buys / _get_buy_candidates 皆
call-time 讀 config — wf_ema_entry_filter.py 前例),不修改任何檔案。
kurt60 門檻 = 關卡一同源的全樣本錨定値(anchor_thresholds.json P90);
此為 in-sample 比較,若 F2 勝出要走部署需另過 WF。
資料凍結 STUDY_END(takeprofit_common)。F1/F3 應分別複現 A0/D1 — 兼作保真。

用法: python -m robustness.takeprofit_filter_matrix
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np                                                   # noqa: E402
import pandas as pd                                                  # noqa: E402

from robustness.utils import OUTPUT_ROOT, compute_metrics            # noqa: E402
from robustness.takeprofit_common import STUDY_END                   # noqa: E402
from robustness.takeprofit_arms import (                             # noqa: E402
    TakeProfitBacktester, FactorCache, verify_source_sync, THRESHOLDS_FP,
)
from strategies.sp500_momentum import config as sp_cfg               # noqa: E402
from strategies.sp500_momentum import utils as sp_utils              # noqa: E402
from strategies.sp500_momentum.selection import SelectionEngine      # noqa: E402

OUT_DIR = OUTPUT_ROOT / "takeprofit"
MATRIX_FP = OUT_DIR / "filter_matrix.csv"


def main():
    verify_source_sync()          # 需在 MAX_ADJ_SLOPE 仍為 1.5 時執行
    # 上游 ADR-0011 flag 關閉:矩陣的 kurt 封鎖由 block_only 機制提供,
    # live flag 開著會讓 F0/F1(無 kurt 格)也被封鎖,矩陣失義。
    sp_cfg.BLOWOFF_ENTRY_BLOCK = False

    th = json.loads(THRESHOLDS_FP.read_text(encoding="utf-8"))["thresholds"]
    kurt_p90 = th["kurt60"]["P90"]
    print(f"[threshold] kurt60 P90 = {kurt_p90:.4f} (全樣本錨定,同關卡一)")

    selector = SelectionEngine()
    spy_df = sp_utils.load_benchmark(sp_cfg.ETF_DIR / "SPY.csv")
    sso_df = sp_utils.load_benchmark(sp_cfg.ETF_DIR / "SSO.csv")
    factor_cache = FactorCache(selector)

    cells = [
        ("F0_neither",  None, False),
        ("F1_adjslope", 1.5,  False),
        ("F2_kurt",     None, True),
        ("F3_both",     1.5,  True),
    ]

    orig_mas = sp_cfg.MAX_ADJ_SLOPE
    rows = []
    try:
        for name, mas, kurt_on in cells:
            sp_cfg.MAX_ADJ_SLOPE = mas
            kwargs = (dict(tp_mode="block_only", factor_key="kurt60",
                           threshold=kurt_p90, factor_cache=factor_cache)
                      if kurt_on else dict(tp_mode=None))
            print(f"[run] {name} (MAX_ADJ_SLOPE={mas}, kurt_block={kurt_on}) ...",
                  flush=True)
            bt = TakeProfitBacktester(
                end_date=STUDY_END, selector=selector,
                spy_df=spy_df, sso_df=sso_df, **kwargs)
            bt.run()
            eq = bt.get_equity_series()
            m = compute_metrics(eq.pct_change().dropna())
            rows.append({
                "name": name, "max_adj_slope": mas, "kurt_block": kurt_on,
                "cagr": round(float(m["ann_ret"]), 5),
                "sharpe": round(float(m["sharpe"]), 4),
                "max_dd": round(float(m["max_dd"]), 5),
                "calmar": round(float(m["calmar"]), 4),
                "final_equity": round(float(eq.iloc[-1]), 0),
                "n_trades": len(bt.trades),
                "kurt_blocks": len(bt.block_events),
            })
            r = rows[-1]
            print(f"    CAGR={r['cagr']*100:.2f}%  Sharpe={r['sharpe']:.3f}  "
                  f"MDD={r['max_dd']*100:.2f}%  kurt_blocks={r['kurt_blocks']}",
                  flush=True)
            if bt.block_events:
                pd.DataFrame(bt.block_events).to_csv(
                    OUT_DIR / f"blocks_{name}.csv", index=False)
    finally:
        sp_cfg.MAX_ADJ_SLOPE = orig_mas

    df = pd.DataFrame(rows)
    df.to_csv(MATRIX_FP, index=False)
    print("\n──進場濾網 2×2 矩陣──")
    print(df.to_string(index=False))

    f = {r["name"]: r for r in rows}
    print("\n──邊際貢獻(Sharpe)──")
    print(f"  adj_slope 單獨: {f['F1_adjslope']['sharpe'] - f['F0_neither']['sharpe']:+.4f} "
          f"(F1-F0) / 在 kurt 之上: {f['F3_both']['sharpe'] - f['F2_kurt']['sharpe']:+.4f} (F3-F2)")
    print(f"  kurt 封鎖 單獨: {f['F2_kurt']['sharpe'] - f['F0_neither']['sharpe']:+.4f} "
          f"(F2-F0) / 在 adj_slope 之上: {f['F3_both']['sharpe'] - f['F1_adjslope']['sharpe']:+.4f} (F3-F1)")
    print(f"\n[done] -> {MATRIX_FP}")


if __name__ == "__main__":
    main()
