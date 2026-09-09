"""
QuantCore Robustness — SR / SP500 / SkewLS 靜態粒度分析(live log 實證)
=======================================================================
Parse automation/logs/rebalance_*.log(2026-03 至今),量化整數執行稅
在三個帳戶的實際發生值——不跑回測,直接用 live 系統自己算出的
目標 vs 理論權重對帳。

證據等級:
  SP500 : 精確(ATR 權重每次 SP500 rebalance 都落 log,0.1% 精度)
  SR    : 精確(權重每日落 log,但顯示取整至 1% → ±0.5pp 顯示噪音)
  SkewLS: 上界(權重不落 log;round() 語意 → 每腳誤差 ≤ ±0.5 股)

輸出:robustness/output/frac_sizing/static_*.csv + static_summary.txt + PNG
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from robustness.utils import (   # noqa: E402
    OUTPUT_ROOT, setup_dark_theme, save_fig,
    ACCENT, ACCENT_2, ACCENT_3, ACCENT_4,
)

import matplotlib as mpl  # noqa: E402
# 圖表含繁中標籤 → 用系統中文字型
mpl.rcParams["font.sans-serif"] = ["Microsoft JhengHei", "Microsoft YaHei",
                                   "DejaVu Sans"]
mpl.rcParams["axes.unicode_minus"] = False

LOG_DIR = PROJECT_ROOT / "automation" / "logs"
OUT_DIR = OUTPUT_ROOT / "frac_sizing"

RE_ACCOUNT = re.compile(r">>> Account: (\w+) \((U\d+)\) port=\d+ <<<")
# SkewLS 項為 optional:2026-05 之前的 Alloc 行只有 SR + SP500
RE_ALLOC   = re.compile(r"Alloc: SR (\d+)% \+ SP500 (\d+)%(?: \+ SkewLS (\d+)%)?")
RE_NAV     = re.compile(r"Step 3: NAV = \$([\d,]+\.?\d*)")
RE_SR_W    = re.compile(r"SR\s+(\w+): (\d+)%")
RE_SP_HDR  = re.compile(r"SP500 target weights")
RE_SP_W    = re.compile(r"^\s*(\w+): ([\d.]+)%\s*$")
# 價格行的 "(feed)" 等後綴為 optional(帳戶間格式不一致)
RE_PRICE   = re.compile(r"^\s*(\w+): \$([\d.]+)(?: \(\w+\))?\s*$")
RE_TARGET  = re.compile(
    r"^\s*(\w+) \[([\w\+ \(\)]+)\]: target=\s*(-?\d+)sh current=\s*(-?[\d.]+)sh")


def parse_log(fp: Path) -> list[dict]:
    """回傳該日所有 (account, symbol) 的 target/price/weight 紀錄。"""
    date = fp.stem.replace("rebalance_", "")
    rows, acct, alloc, nav = [], None, {}, None
    sr_w, sp_w, prices = {}, {}, {}
    in_sp_block = False

    for raw in fp.read_text(encoding="utf-8", errors="replace").splitlines():
        # 內容部分(去掉時間戳與 level)
        msg = raw.split("INFO", 1)[-1].strip() if "INFO" in raw else raw

        m = RE_ACCOUNT.search(raw)
        if m:
            acct = m.group(1)
            alloc, nav = {}, None
            sr_w, sp_w, prices = {}, {}, {}
            in_sp_block = False
            continue
        if acct is None:
            continue

        m = RE_ALLOC.search(raw)
        if m:
            alloc = {"sr": int(m.group(1)) / 100,
                     "sp500": int(m.group(2)) / 100,
                     "skewls": int(m.group(3) or 0) / 100}
            continue

        m = RE_NAV.search(raw)
        if m:
            nav = float(m.group(1).replace(",", ""))
            continue

        m = RE_SR_W.search(raw)
        if m:
            sr_w[m.group(1)] = int(m.group(2)) / 100
            continue

        if RE_SP_HDR.search(raw):
            in_sp_block = True
            continue
        if in_sp_block:
            m = RE_SP_W.match(msg)
            if m:
                sp_w[m.group(1)] = float(m.group(2)) / 100
                continue
            in_sp_block = False  # block 結束

        m = RE_PRICE.match(msg)
        if m:
            prices[m.group(1)] = float(m.group(2))
            continue

        m = RE_TARGET.match(msg)
        if m and nav:
            sym, tag = m.group(1), m.group(2)
            rows.append({
                "date": date, "account": acct, "symbol": sym, "tag": tag,
                "target_sh": int(m.group(3)),
                "current_sh": float(m.group(4)),
                "price": prices.get(sym, np.nan),
                "nav": nav,
                "alloc_sr": alloc.get("sr", np.nan),
                "alloc_sp500": alloc.get("sp500", np.nan),
                "alloc_skewls": alloc.get("skewls", np.nan),
                "sr_w": sr_w.get(sym, np.nan),
                "sp_w": sp_w.get(sym, np.nan),
            })
    return rows


def build_dataset() -> pd.DataFrame:
    files = sorted(LOG_DIR.glob("rebalance_*.log"))
    rows = []
    for fp in files:
        try:
            rows.extend(parse_log(fp))
        except Exception as e:              # noqa: BLE001
            print(f"  [skip] {fp.name}: {e}")
    df = pd.DataFrame(rows)
    if df.empty:
        raise SystemExit("沒有解析到任何 rebalance log")
    df["date"] = pd.to_datetime(df["date"])
    # 只保留三個已知帳戶(排除 log 雜訊誤匹配)
    df = df[df.account.isin(["Taylor", "Rosa", "Yvonne"])]
    return df


def analyze(df: pd.DataFrame):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    lines = []

    def w(s=""):
        lines.append(s)
        print(s)

    w("=" * 74)
    w("SR / SP500 / SkewLS 靜態粒度分析 — live rebalance log 實證")
    w(f"樣本:{df.date.min().date()} → {df.date.max().date()},"
      f"{df.date.nunique()} 個交易日,{len(df)} 筆 (帳戶×標的×日)")
    w("=" * 74)

    # ── A. SP500:精確理論 vs 執行 ────────────────────────────────────────────
    sp = df[(df.tag == "SP500") & df.sp_w.notna() & df.price.notna()
            & df.alloc_sp500.notna() & (df.sp_w > 0)].copy()
    sp["theory_val"] = sp.nav * sp.alloc_sp500 * sp.sp_w
    sp["exec_val"]   = sp.target_sh * sp.price
    sp["gap_val"]    = sp.exec_val - sp.theory_val
    sp["rel_err"]    = sp.exec_val / sp.theory_val - 1

    w("\n【A】SP500(精確——ATR 權重逐次落 log)")
    if len(sp):
        g = (sp.groupby(["account", "date"])
               .agg(theory=("theory_val", "sum"), execu=("exec_val", "sum"),
                    n=("symbol", "count")).reset_index())
        g["deployment"] = g.execu / g.theory
        for acct, sub in g.groupby("account"):
            zero = sp[(sp.account == acct) & (sp.target_sh == 0)]
            w(f"  {acct:<8} 部署率 mean {sub.deployment.mean():6.1%}"
              f"  min {sub.deployment.min():6.1%}"
              f"  (現金拖累 mean {(1-sub.deployment.mean())*100:4.1f}% of sleeve,"
              f" {sub.date.nunique()} 次 rebalance,漏單 {len(zero)} 筆)")
        sp_sym = (sp.assign(abs_rel=sp.rel_err.abs())
                    .groupby("symbol")
                    .agg(mean_rel_err=("rel_err", "mean"),
                         max_abs_rel=("abs_rel", "max"),
                         n=("date", "count"))
                    .sort_values("mean_rel_err"))
        w("\n  個股平均 sizing 誤差(exec/theory − 1,floor → 恆 ≤ 0):")
        for s, r in sp_sym.iterrows():
            w(f"    {s:<6} mean {r.mean_rel_err:+7.1%}   worst {r.max_abs_rel:6.1%}   n={r.n:.0f}")
        sp.to_csv(OUT_DIR / "static_sp500.csv", index=False)
        g.to_csv(OUT_DIR / "static_sp500_deployment.csv", index=False)
    else:
        w("  (無樣本)")

    # ── B. SR:精確(權重顯示取整 1% → ±0.5pp 噪音)──────────────────────────
    sr = df[(df.tag == "SR") & df.sr_w.notna() & df.price.notna()
            & df.alloc_sr.notna() & (df.sr_w > 0)].copy()
    sr["theory_val"] = sr.nav * sr.alloc_sr * sr.sr_w
    sr["exec_val"]   = sr.target_sh * sr.price
    sr["rel_err"]    = sr.exec_val / sr.theory_val - 1

    w("\n【B】SR(精確;權重顯示取整至 1%,XLV 常與 SkewLS 合併不列入)")
    if len(sr):
        gg = (sr.groupby(["account", "date"])
                .agg(theory=("theory_val", "sum"), execu=("exec_val", "sum"))
                .reset_index())
        gg["deployment"] = gg.execu / gg.theory
        for acct, sub in gg.groupby("account"):
            w(f"  {acct:<8} 部署率 mean {sub.deployment.mean():6.1%}"
              f"  min {sub.deployment.min():6.1%}"
              f"  ({sub.date.nunique()} 日,純 SR 標的)")
        sr.to_csv(OUT_DIR / "static_sr.csv", index=False)
    else:
        w("  (無樣本)")

    # ── C. SkewLS:round() 上界分析 ───────────────────────────────────────────
    sk = df[df.tag.str.contains("SkewLS") & (~df.tag.str.contains("SR"))
            & df.price.notna() & (df.target_sh != 0)].copy()
    sk["one_sh_pct_leg"] = 1 / sk.target_sh.abs()          # 一股佔該腳 %
    sk["half_sh_nav"]    = 0.5 * sk.price / sk.nav          # 半股誤差上界佔 NAV

    w("\n【C】SkewLS(上界——權重不落 log;round() → 每腳誤差 ≤ ±0.5 股)")
    if len(sk):
        for acct, sub in sk.groupby("account"):
            worst = sub.nlargest(3, "one_sh_pct_leg")
            wl = ", ".join(f"{r.symbol}±{r.one_sh_pct_leg*50:.0f}%"
                           for r in worst.itertuples())
            w(f"  {acct:<8} 腳數/日 med {sub.groupby('date').size().median():.0f}"
              f"  半股誤差上界 med {sub.half_sh_nav.median()*100:.2f}% NAV"
              f"  最粗腳(±半股/腳): {wl}")
        sk.to_csv(OUT_DIR / "static_skewls.csv", index=False)
    else:
        w("  (無樣本)")

    (OUT_DIR / "static_summary.txt").write_text(
        "\n".join(lines), encoding="utf-8")

    # ── 圖:SP500 部署率時間序列(三帳戶)────────────────────────────────────
    if len(sp):
        import matplotlib.pyplot as plt
        setup_dark_theme()
        fig, ax = plt.subplots(figsize=(11, 4.5))
        colors = {"Taylor": ACCENT_2, "Rosa": ACCENT_3, "Yvonne": ACCENT_4}
        g = (sp.groupby(["account", "date"])
               .agg(theory=("theory_val", "sum"), execu=("exec_val", "sum"))
               .reset_index())
        g["deployment"] = g.execu / g.theory
        for acct, sub in g.groupby("account"):
            sub = sub.sort_values("date")
            ax.plot(sub.date, sub.deployment * 100, "o-",
                    color=colors.get(acct, ACCENT), label=acct, ms=4, lw=1.4)
        ax.axhline(100, color="#888", ls="--", lw=1, label="理論 (碎股)")
        ax.set_ylabel("SP500 sleeve 部署率 (%)")
        ax.set_title("Live 實證:SP500 整數執行的部署率(floor → 恆 <100%)")
        ax.legend()
        save_fig(fig, OUT_DIR / "static_sp500_deployment.png")

    print(f"\n輸出:{OUT_DIR}\\static_*.csv, static_summary.txt, static_sp500_deployment.png")


if __name__ == "__main__":
    analyze(build_dataset())
