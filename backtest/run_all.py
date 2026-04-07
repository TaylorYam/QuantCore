"""
QuantCore — 統一回測執行入口
用法：
    cd C:/QuantCore-master
    python -m backtest.run_all
    python -m backtest.run_all --alloc 0.8 0.5          # 80% SR + 50% SP500
    python -m backtest.run_all --alloc 0.8 0.5 --quantstats
    python -m backtest.run_all --start 2019-01-01
    python -m backtest.run_all --leverage 1.3            # 等比放大（舊模式）
"""

import argparse
import sys
import os

# ── 確保 C:/QuantCore-master 在 sys.path ─────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pandas as pd
from pathlib import Path

from backtest.combo    import build_all, to_equity_curves
from backtest.metrics  import compute, annual_returns, print_table
from backtest.charts   import plot_equity_curves, STYLES
from backtest.leverage import apply_leverage, leverage_summary, ib_blended_rate

OUTPUT_DIR = Path("C:/QuantCore-master/output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_LEVERAGE      = 1.3
DEFAULT_INITIAL_EQUITY = 1_000_000


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main(
    start_date    = None,
    end_date      = None,
    leverage      : float = 1.0,
    alloc         : tuple[float, float] | None = None,
    initial_equity: float = DEFAULT_INITIAL_EQUITY,
    quantstats    : bool  = False,
):
    """
    alloc=(0.8, 0.5) → 80% SR + 50% SP500，月初重設，IB 利息已內嵌。
    leverage=1.3     → 舊模式：所有組合等比放大（不改變組合內比例）。
    兩者互斥，alloc 優先。
    """
    print("\n" + "=" * 60)
    print("  QuantCore  ·  Backtest Runner")

    if alloc:
        a_sr, a_sp = alloc
        total_exp  = a_sr + a_sp
        borrow     = max(0.0, total_exp - 1.0)
        rate       = ib_blended_rate(borrow * initial_equity) if borrow > 0 else 0.0
        print(f"  Alloc: SR {a_sr:.0%} + SP500 {a_sp:.0%} = {total_exp:.0%} exposure")
        if borrow > 0:
            print(f"  Borrow: {borrow:.0%}  |  IB rate: {rate:.3%}/yr  "
                  f"|  Daily drag: {borrow*rate/252:.5%}")
    elif leverage != 1.0:
        borrow = (leverage - 1) * initial_equity
        rate   = ib_blended_rate(borrow)
        print(f"  Leverage: {leverage:.1f}x  |  IB rate: {rate:.3%}/yr  "
              f"|  Drag: {(leverage-1)*rate:.3%}/yr")
    print("=" * 60)

    # 1. 執行策略 + 組合
    extra = [alloc] if alloc else None
    combos = build_all(
        start_date=start_date, end_date=end_date,
        extra_allocs=extra, initial_equity=initial_equity,
    )

    # 2. 舊模式：等比槓桿放大（alloc 模式不走這裡，融資成本已在 combine_monthly 裡）
    if not alloc and leverage > 1.0:
        leveraged = {}
        for name, ret in combos.items():
            if name == "SPY B&H":
                leveraged[name] = ret
            else:
                leveraged[name] = apply_leverage(
                    ret, leverage=leverage, initial_equity=initial_equity
                )
        combos = leveraged

    # 3. 計算績效指標
    metrics_list = []
    for name, ret in combos.items():
        m = compute(ret, label=name)
        metrics_list.append(m)

    # 4. 終端印表
    print("\n[績效比較表]")
    print_table(metrics_list)

    # 5. 逐年報酬
    print("\n[逐年報酬]")
    ann_df = pd.DataFrame(
        {m["label"]: annual_returns(combos[m["label"]]) for m in metrics_list}
    )
    print(ann_df.map(lambda x: f"{x:+.1%}" if pd.notna(x) else "-").to_string())

    # 6. 權益曲線圖
    if alloc:
        tag  = f"_{alloc[0]:.0%}sr_{alloc[1]:.0%}sp"
        titl = f"QuantCore — {alloc[0]:.0%} SR + {alloc[1]:.0%} SP500 (Log Scale)"
    elif leverage != 1.0:
        tag  = f"_{leverage:.1f}x"
        titl = f"QuantCore — Equity Curves ({leverage:.1f}x Leverage) (Log Scale)"
    else:
        tag  = ""
        titl = "QuantCore — Equity Curves (Log Scale)"

    chart_path = OUTPUT_DIR / f"equity_curve{tag}.png"
    equity = to_equity_curves(combos)
    plot_equity_curves(equity, title=titl, save_path=chart_path)
    print(f"\n[Chart] {chart_path}")

    # 7. QuantStats（選用）
    if quantstats:
        _run_quantstats(combos, tag)

    print("\n[Done]\n")
    return combos, metrics_list, equity


# ── QuantStats 深度分析 ───────────────────────────────────────────────────────

def _run_quantstats(combos: dict, lev_tag: str = ""):
    try:
        import quantstats as qs
    except ImportError:
        print("quantstats not installed. Run: pip install quantstats")
        return

    qs.extend_pandas()

    spy_ret = combos.get("SPY B&H")
    if spy_ret is not None:
        spy_ret = spy_ret[~spy_ret.index.duplicated(keep="last")].sort_index()

    for name, ret in combos.items():
        if name == "SPY B&H":
            continue
        ret = ret[~ret.index.duplicated(keep="last")].sort_index()

        safe_name = name.replace("/", "-").replace(" ", "_")
        out_path  = OUTPUT_DIR / f"tearsheet_{safe_name}{lev_tag}.html"

        # 刪除舊報告，避免 QuantStats 拋錯
        if out_path.exists():
            out_path.unlink()

        print(f"\nQuantStats: {name} ...")
        try:
            qs.reports.html(
                ret,
                benchmark=spy_ret,
                title=f"QuantCore — {name}",
                output=str(out_path),
            )
            print(f"  saved: {out_path}")
        except Exception as e:
            print(f"  FAILED {name}: {e}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="QuantCore Backtest Runner")
    parser.add_argument("--start",      default=None,              help="回測起始日 YYYY-MM-DD")
    parser.add_argument("--end",        default=None,              help="回測結束日 YYYY-MM-DD")
    parser.add_argument("--leverage",   default=1.0,  type=float, nargs="?", const=1.3,
                                                                   help="等比槓桿倍數（預設 1.0）")
    parser.add_argument("--alloc",      default=None, type=float, nargs=2,
                                                                   metavar=("SR", "SP"),
                                                                   help="自定比例，如 --alloc 0.8 0.5")
    parser.add_argument("--equity",     default=DEFAULT_INITIAL_EQUITY, type=float,
                                                                   help="初始本金（影響 IB 分級利率）")
    parser.add_argument("--quantstats", action="store_true",       help="輸出 QuantStats HTML 報告")
    args = parser.parse_args()

    main(
        start_date     = args.start,
        end_date       = args.end,
        leverage       = args.leverage,
        alloc          = tuple(args.alloc) if args.alloc else None,
        initial_equity = args.equity,
        quantstats     = args.quantstats,
    )
