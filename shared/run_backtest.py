"""
QuantCore — Shared Edition 回測工具
===================================
50/50 不開槓桿策略回測，適用於複委託用戶。

用法：
    cd C:/QuantCore-master
    python -m shared.run_backtest
    python -m shared.run_backtest --commission 0.003          # 手續費 0.3%（單向）
    python -m shared.run_backtest --commission 0.003 --quantstats
    python -m shared.run_backtest --start 2020-01-01 --end 2025-12-31
    python -m shared.run_backtest --commission 0.003 --equity 500000

手續費說明：
    --commission 為「單向」交易成本（買 or 賣各收一次）。
    複委託常見費率範例：
      - 0.15% (0.0015) : 低成本券商
      - 0.20% (0.002)  : 常見折扣後
      - 0.30% (0.003)  : 一般牌告
      - 0.50% (0.005)  : 較高費率
    系統預設 IB 全成本估算：0.075% (0.00075)
"""

import argparse
import sys
import os

# ── 確保 C:/QuantCore-master 在 sys.path ────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pandas as pd
from pathlib import Path

from backtest.combo   import to_equity_curves, combine_50_50
from backtest.metrics import compute, annual_returns, print_table
from backtest.charts  import plot_equity_curves

OUTPUT_DIR = Path(ROOT) / "output" / "shared"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_COMMISSION     = 0.00075   # IB 預設 0.075%（單向）
DEFAULT_INITIAL_EQUITY = 1_000_000


# ── 策略執行（支援自訂手續費）─────────────────────────────────────────────────

def run_sr(start_date=None, end_date=None, commission=None):
    """執行 Sector Rotation MOM，可覆寫手續費。"""
    from strategies.sector_rotation.data_loader import load_data
    from strategies.sector_rotation.backtest    import run_full_backtest

    data = load_data()
    if start_date or end_date:
        sd = pd.to_datetime(start_date) if start_date else None
        ed = pd.to_datetime(end_date)   if end_date   else None
        for key in ("etf_close", "etf_open"):
            df = data[key]
            if sd: df = df[df.index >= sd]
            if ed: df = df[df.index <= ed]
            data[key] = df
        for key in ("spy_close", "spy_open"):
            s = data[key]
            if sd: s = s[s.index >= sd]
            if ed: s = s[s.index <= ed]
            data[key] = s

    net_ret, _, _ = run_full_backtest(data, tc=commission)
    net_ret.name = "SR"
    return net_ret


def run_sp500(start_date=None, end_date=None, commission=None):
    """執行 SP500 Momentum，可覆寫手續費。"""
    from strategies.sp500_momentum import config as sp_cfg

    # 暫時覆寫 config.COMMISSION
    original = sp_cfg.COMMISSION
    if commission is not None:
        sp_cfg.COMMISSION = commission

    try:
        from strategies.sp500_momentum import run
        bt     = run(start_date=start_date, end_date=end_date, write_reports=False)
        equity = bt.get_equity_series()
        ret    = equity.pct_change().dropna()
        ret.name = "SP500"
        return ret
    finally:
        sp_cfg.COMMISSION = original


def run_benchmark(start_date=None, end_date=None):
    """載入 SPY 作為基準。"""
    from strategies.sector_rotation.data_loader import load_data
    data = load_data()
    spy  = data["spy_close"]
    if start_date:
        spy = spy[spy.index >= pd.to_datetime(start_date)]
    if end_date:
        spy = spy[spy.index <= pd.to_datetime(end_date)]
    ret = spy.pct_change().dropna()
    ret.name = "SPY B&H"
    return ret


# ── 50/50 月初重設組合 ─────────────────────────────────────────────────────────
# combine_50_50 已收斂到 backtest.combo（與舊本地版數學等價：50/50 時 borrow=0、
# financing=0），於檔案頂端 import，不再本地重抄。


# ── 主流程 ──────────────────────────────────────────────────────────────────

def main(
    start_date=None,
    end_date=None,
    commission: float = DEFAULT_COMMISSION,
    initial_equity: float = DEFAULT_INITIAL_EQUITY,
    quantstats: bool = False,
):
    print("\n" + "=" * 60)
    print("  QuantCore — Shared Edition (50/50 No Leverage)")
    print(f"  手續費（單向）: {commission*100:.4f}%")
    print("=" * 60)

    # 1. 執行策略
    print("Step 1/4  SR MOM...")
    ret_sr = run_sr(start_date, end_date, commission=commission)

    print("Step 2/4  SP500 MOM...")
    ret_sp = run_sp500(start_date, end_date, commission=commission)

    print("Step 3/4  SPY benchmark...")
    ret_spy = run_benchmark(start_date, end_date)

    print("Step 4/4  Combining...")
    common = ret_sr.index.intersection(ret_sp.index)
    ret_sr = ret_sr.loc[common]
    ret_sp = ret_sp.loc[common]

    c5050 = combine_50_50(ret_sr, ret_sp)

    combos = {
        "100/0 SR":     ret_sr.rename("100/0 SR"),
        "0/100 SP500":  ret_sp.rename("0/100 SP500"),
        "50/50 Monthly": c5050,
        "SPY B&H":      ret_spy,
    }
    print("Done.\n")

    # 2. 績效指標
    metrics_list = [compute(ret, label=name) for name, ret in combos.items()]

    print("[績效比較表]")
    print_table(metrics_list)

    # 3. 逐年報酬
    print("\n[逐年報酬]")
    ann_df = pd.DataFrame(
        {m["label"]: annual_returns(combos[m["label"]]) for m in metrics_list}
    )
    print(ann_df.map(lambda x: f"{x:+.1%}" if pd.notna(x) else "-").to_string())

    # 4. 權益曲線圖
    tc_tag = f"_tc{commission*10000:.0f}bps"
    chart_path = OUTPUT_DIR / f"equity_curve_5050{tc_tag}.png"
    equity = to_equity_curves(combos)
    plot_equity_curves(
        equity,
        title=f"QuantCore 50/50 — No Leverage (TC={commission*100:.3f}%)",
        save_path=chart_path,
    )
    print(f"\n[Chart] {chart_path}")

    # 5. QuantStats
    if quantstats:
        _run_quantstats(combos, tc_tag)

    print("\n[Done]\n")
    return combos, metrics_list, equity


# ── QuantStats 深度分析 ────────────────────────────────────────────────────

def _run_quantstats(combos: dict, tag: str = ""):
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
        out_path  = OUTPUT_DIR / f"tearsheet_{safe_name}{tag}.html"

        if out_path.exists():
            out_path.unlink()

        print(f"\nQuantStats: {name} ...")
        try:
            qs.reports.html(
                ret,
                benchmark=spy_ret,
                title=f"QuantCore 50/50 — {name}",
                output=str(out_path),
            )
            print(f"  saved: {out_path}")
        except Exception as e:
            print(f"  FAILED {name}: {e}")


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="QuantCore Shared Edition — 50/50 No Leverage Backtest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
手續費範例（單向）：
  --commission 0.00075   IB Pro（預設）
  --commission 0.0015    複委託低費率
  --commission 0.002     複委託折扣後
  --commission 0.003     複委託牌告
  --commission 0.005     較高費率
        """,
    )
    parser.add_argument("--start",      default=None,              help="回測起始日 YYYY-MM-DD")
    parser.add_argument("--end",        default=None,              help="回測結束日 YYYY-MM-DD")
    parser.add_argument("--commission", default=DEFAULT_COMMISSION, type=float,
                        help=f"單向手續費率（預設 {DEFAULT_COMMISSION}）")
    parser.add_argument("--equity",     default=DEFAULT_INITIAL_EQUITY, type=float,
                        help="初始本金（預設 1,000,000）")
    parser.add_argument("--quantstats", action="store_true",       help="輸出 QuantStats HTML 報告")
    args = parser.parse_args()

    main(
        start_date     = args.start,
        end_date       = args.end,
        commission     = args.commission,
        initial_equity = args.equity,
        quantstats     = args.quantstats,
    )
