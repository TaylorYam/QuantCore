"""
QuantCore Robustness — 壓力情境分析 (Stress Test)

從 baseline_returns.csv 擷取歷史壓力情境區段，分析策略在極端市場下的表現：
- 2018/Q4 Fed 縮表恐慌
- 2020 COVID 崩盤與反彈
- 2022 升息熊市
- 2023/03 SVB 銀行危機
- 2025 年自動偵測大回撤（若有）

另分析 SP500 策略熊市機制 (SPY < MA200) 的觸發合理性。

輸出：
  robustness/output/stress_test/
    stress_summary_{ts}.csv
    bear_triggers_{ts}.csv
    equity_{scenario}_{ts}.png
    stress_summary_chart_{ts}.png
    bear_trigger_timeline_{ts}.png
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from robustness.utils import (
    compute_metrics,
    equity_curve,
    drawdown,
    setup_dark_theme,
    save_fig,
    OUTPUT_ROOT,
    timestamp,
    DARK_BG, DARK_FG, DARK_GRID,
    ACCENT, ACCENT_2, ACCENT_3, ACCENT_4,
)

# ── 輸入輸出 ──────────────────────────────────────────────────────────────────
BASELINE_CSV = OUTPUT_ROOT / "baseline_returns.csv"
SPY_CSV      = Path(__file__).resolve().parent.parent / "data" / "etfs" / "SPY.csv"
OUT_DIR      = OUTPUT_ROOT / "stress_test"
OUT_DIR.mkdir(parents=True, exist_ok=True)

STRATEGY_COLS = ["SR", "SP500", "Combo_80_50_1.3x", "SPY"]
STRATEGY_COLORS = {
    "SR":                ACCENT,
    "SP500":             ACCENT_2,
    "Combo_80_50_1.3x":  ACCENT_3,
    "SPY":               ACCENT_4,
}

# ── 壓力情境定義 ──────────────────────────────────────────────────────────────
SCENARIOS = [
    {
        "key":   "2018Q4_fed_taper",
        "name":  "2018 Q4 Fed Taper Tantrum",
        "start": "2018-10-01",
        "end":   "2018-12-31",
        "in_range": True,
    },
    {
        "key":   "2020_covid_crash",
        "name":  "2020 COVID Crash (Feb-Apr)",
        "start": "2020-02-19",
        "end":   "2020-04-07",
        "in_range": True,
    },
    {
        "key":   "2020_covid_full_year",
        "name":  "2020 COVID Full Year",
        "start": "2020-02-19",
        "end":   "2020-12-31",
        "in_range": True,
    },
    {
        "key":   "2022_rate_hike_bear",
        "name":  "2022 Rate Hike Bear Market",
        "start": "2022-01-01",
        "end":   "2022-12-31",
        "in_range": True,
    },
    {
        "key":   "2023_svb_crisis",
        "name":  "2023 SVB Banking Crisis",
        "start": "2023-03-01",
        "end":   "2023-05-31",
        "in_range": True,
    },
    # N/A: 資料不足
    {"key": "2008_gfc",          "name": "2008 GFC",          "start": "2008-09-01", "end": "2009-03-31", "in_range": False, "reason": "baseline_returns.csv 起始於 2018-07，2008 不在資料範圍"},
    {"key": "2011_downgrade",    "name": "2011 US Downgrade", "start": "2011-07-01", "end": "2011-10-31", "in_range": False, "reason": "baseline_returns.csv 起始於 2018-07，2011 不在資料範圍"},
    {"key": "2015_china_crash",  "name": "2015 China Crash",  "start": "2015-08-01", "end": "2016-02-29", "in_range": False, "reason": "baseline_returns.csv 起始於 2018-07，2015 不在資料範圍"},
]


# ── 資料載入 ──────────────────────────────────────────────────────────────────
def load_baseline() -> pd.DataFrame:
    df = pd.read_csv(BASELINE_CSV, parse_dates=["Date"]).set_index("Date").sort_index()
    # 移除重複日期（保留第一筆）
    df = df[~df.index.duplicated(keep="first")]
    return df


def load_spy_prices() -> pd.DataFrame:
    df = pd.read_csv(SPY_CSV, parse_dates=["Date"]).set_index("Date").sort_index()
    return df


# ── 情境分析 ──────────────────────────────────────────────────────────────────
def analyze_scenario(returns: pd.DataFrame, scenario: dict) -> list[dict]:
    """
    回傳每個策略在該情境下的分析結果列（list of dict）。
    """
    sd = pd.to_datetime(scenario["start"])
    ed = pd.to_datetime(scenario["end"])

    # 取子區間（若超出資料範圍則 clip）
    sub = returns.loc[(returns.index >= sd) & (returns.index <= ed)]
    if sub.empty:
        return []

    actual_start = sub.index[0]
    actual_end   = sub.index[-1]
    period_str   = f"{actual_start.date()} → {actual_end.date()}"

    # 基準期（全樣本）每個策略的年化波動，供放大係數用
    full_ann_vol = returns.std() * np.sqrt(252)

    rows = []
    for col in STRATEGY_COLS:
        r = sub[col].dropna()
        if r.empty:
            continue

        cum = (1 + r).cumprod()
        total_ret = cum.iloc[-1] - 1

        # 回撤
        dd = cum / cum.cummax() - 1
        mdd = dd.min()
        mdd_day = dd.idxmin()
        # 峰到谷
        peak_day_before_trough = cum.loc[:mdd_day].idxmax()
        peak_to_trough_days = (mdd_day - peak_day_before_trough).days

        # 復原：谷之後首次回到 peak value
        peak_val = cum.loc[peak_day_before_trough]
        post = cum.loc[mdd_day:]
        recovered = post[post >= peak_val]
        if len(recovered) > 0:
            recov_day = recovered.index[0]
            recovery_days = (recov_day - mdd_day).days
            recovered_flag = True
        else:
            recovery_days = (actual_end - mdd_day).days
            recovered_flag = False

        # 年化波動 & 放大係數
        ann_vol = r.std() * np.sqrt(252)
        vol_mult = ann_vol / full_ann_vol[col] if full_ann_vol[col] > 0 else np.nan

        # SPY 累積報酬
        spy_r = sub["SPY"].dropna()
        spy_cum = (1 + spy_r).prod() - 1 if len(spy_r) else np.nan
        excess = total_ret - spy_cum

        rows.append({
            "scenario":       scenario["key"],
            "scenario_name":  scenario["name"],
            "strategy":       col,
            "period":         period_str,
            "days":           len(r),
            "cum_ret":        total_ret,
            "mdd":            mdd,
            "peak_to_trough_days": peak_to_trough_days,
            "recovery_days":  recovery_days,
            "recovered":      recovered_flag,
            "ann_vol":        ann_vol,
            "vol_mult":       vol_mult,
            "spy_cum_ret":    spy_cum,
            "excess_ret":     excess,
        })
    return rows


def plot_scenario_equity(returns: pd.DataFrame, scenario: dict, ts: str):
    sd = pd.to_datetime(scenario["start"])
    ed = pd.to_datetime(scenario["end"])
    sub = returns.loc[(returns.index >= sd) & (returns.index <= ed)]
    if sub.empty:
        return None

    # 歸一化至起點 1.0
    eq = (1 + sub[STRATEGY_COLS]).cumprod()
    # 確保起點為 1
    eq = eq / eq.iloc[0]

    fig, ax = plt.subplots(figsize=(11, 6))
    for col in STRATEGY_COLS:
        ax.plot(eq.index, eq[col], label=col, color=STRATEGY_COLORS[col], linewidth=1.6)
    ax.axhline(1.0, color=DARK_GRID, linewidth=0.8, linestyle="--")
    ax.set_title(f"{scenario['name']}  ({eq.index[0].date()} → {eq.index[-1].date()})",
                 fontsize=12, pad=12)
    ax.set_ylabel("Equity (normalized = 1.0)")
    ax.set_xlabel("Date")
    ax.legend(loc="best", framealpha=0.3)

    out = OUT_DIR / f"equity_{scenario['key']}_{ts}.png"
    save_fig(fig, out)
    return out


def plot_summary_chart(summary_df: pd.DataFrame, ts: str):
    """各情境各策略的累積報酬 horizontal bar chart。"""
    if summary_df.empty:
        return None

    pivot = summary_df.pivot(index="scenario_name", columns="strategy", values="cum_ret")
    pivot = pivot[STRATEGY_COLS]  # 固定策略順序

    fig, ax = plt.subplots(figsize=(12, max(5, 0.7 * len(pivot) * len(STRATEGY_COLS) / 4 + 2)))

    n_strat = len(STRATEGY_COLS)
    n_scen  = len(pivot)
    bar_h   = 0.8 / n_strat
    y_base  = np.arange(n_scen)

    for i, col in enumerate(STRATEGY_COLS):
        offsets = y_base + (i - (n_strat - 1) / 2) * bar_h
        vals = pivot[col].values
        ax.barh(offsets, vals, height=bar_h, label=col,
                color=STRATEGY_COLORS[col], alpha=0.85)

    ax.set_yticks(y_base)
    ax.set_yticklabels(pivot.index, fontsize=9)
    ax.axvline(0, color=DARK_FG, linewidth=0.8)
    ax.set_xlabel("Cumulative Return")
    ax.set_title("Stress Scenarios — Cumulative Return by Strategy", fontsize=13, pad=12)
    ax.legend(loc="best", framealpha=0.3, fontsize=9)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:+.0%}"))

    out = OUT_DIR / f"stress_summary_chart_{ts}.png"
    save_fig(fig, out)
    return out


# ── 自動偵測 2025 大回撤 ──────────────────────────────────────────────────────
def detect_2025_drawdown(returns: pd.DataFrame) -> dict | None:
    """在 2025 年資料中找 SPY 最大回撤時段；若 MDD < -5% 則加入情境。"""
    spy25 = returns["SPY"].loc["2025-01-01":"2025-12-31"].dropna()
    if spy25.empty:
        return None
    cum = (1 + spy25).cumprod()
    dd = cum / cum.cummax() - 1
    mdd = dd.min()
    if mdd > -0.05:
        return None
    trough = dd.idxmin()
    peak   = cum.loc[:trough].idxmax()
    # 延伸 end 到 trough 後 30 交易日或 2025 結尾
    trough_pos = spy25.index.get_loc(trough)
    end_pos = min(trough_pos + 30, len(spy25) - 1)
    end_dt = spy25.index[end_pos]
    return {
        "key":   "2025_auto_drawdown",
        "name":  f"2025 Auto-Detected SPY Drawdown ({mdd:.1%})",
        "start": peak.strftime("%Y-%m-%d"),
        "end":   end_dt.strftime("%Y-%m-%d"),
        "in_range": True,
    }


# ── 熊市 MA200 觸發分析 ──────────────────────────────────────────────────────
def analyze_bear_triggers(returns: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    找出 SPY 跌破 MA200 的所有日期（在 2018-07 之後），評估每次觸發的合理性。

    Returns
    -------
    triggers_df : 觸發事件表
    spy_ma_df   : SPY + MA200 完整序列（供繪圖）
    """
    spy = load_spy_prices()
    spy["MA200"] = spy["Close"].rolling(200).mean()
    spy = spy.dropna(subset=["MA200"])

    # 限制在 baseline 資料範圍內
    spy = spy.loc[spy.index >= returns.index.min()]
    spy = spy.loc[spy.index <= returns.index.max()]

    above = spy["Close"] >= spy["MA200"]
    # 觸發 = 前一日 above、今日 below
    trigger_mask = (~above) & above.shift(1).fillna(False).astype(bool)
    trigger_dates = spy.index[trigger_mask]

    sp500_ret = returns["SP500"]

    records = []
    for td in trigger_dates:
        # 找 td 在 baseline 中的位置
        if td not in returns.index:
            # 取 baseline 中 >= td 的第一個日期
            idxs = returns.index[returns.index >= td]
            if len(idxs) == 0:
                continue
            anchor = idxs[0]
        else:
            anchor = td

        anchor_pos = returns.index.get_loc(anchor)

        def _fwd_ret(days: int) -> float:
            end_pos = min(anchor_pos + days, len(returns) - 1)
            seg = sp500_ret.iloc[anchor_pos:end_pos + 1]
            if seg.empty:
                return np.nan
            return (1 + seg).prod() - 1

        def _spy_fwd_ret(days: int) -> float:
            end_pos = min(anchor_pos + days, len(returns) - 1)
            seg = returns["SPY"].iloc[anchor_pos:end_pos + 1]
            if seg.empty:
                return np.nan
            return (1 + seg).prod() - 1

        sp_30 = _fwd_ret(30)
        sp_60 = _fwd_ret(60)
        sp_90 = _fwd_ret(90)
        spy_30 = _spy_fwd_ret(30)
        spy_60 = _spy_fwd_ret(60)
        spy_90 = _spy_fwd_ret(90)

        # 合理性判定：觸發後 30 天 SPY 確實下跌 → reasonable
        # 或 90 天內 SPY 持續走弱 → reasonable
        reasonable = (spy_30 < 0) or (spy_90 < 0)

        pct_below = (spy.loc[td, "Close"] / spy.loc[td, "MA200"] - 1)

        records.append({
            "trigger_date":      td.date().isoformat(),
            "spy_close":         spy.loc[td, "Close"],
            "spy_ma200":         spy.loc[td, "MA200"],
            "pct_vs_ma200":      pct_below,
            "sp500_ret_30d":     sp_30,
            "sp500_ret_60d":     sp_60,
            "sp500_ret_90d":     sp_90,
            "spy_ret_30d":       spy_30,
            "spy_ret_60d":       spy_60,
            "spy_ret_90d":       spy_90,
            "trigger_reasonable": reasonable,
        })

    triggers_df = pd.DataFrame(records)
    return triggers_df, spy


def plot_bear_trigger_timeline(spy: pd.DataFrame, triggers_df: pd.DataFrame, ts: str):
    fig, ax = plt.subplots(figsize=(13, 6))
    ax.plot(spy.index, spy["Close"], label="SPY Close", color=ACCENT_4, linewidth=1.2)
    ax.plot(spy.index, spy["MA200"], label="MA200", color=ACCENT, linewidth=1.2, linestyle="--")

    if not triggers_df.empty:
        for _, row in triggers_df.iterrows():
            td = pd.to_datetime(row["trigger_date"])
            color = ACCENT_4 if row["trigger_reasonable"] else ACCENT_2
            ax.axvline(td, color=color, alpha=0.35, linewidth=1.0)
            ax.scatter([td], [row["spy_close"]], color=color, s=40, zorder=5,
                       edgecolors=DARK_FG, linewidths=0.5)

    ax.set_title("SPY vs MA200 — Bear Market Trigger Timeline "
                 "(green = post-trigger decline confirmed, red = otherwise)",
                 fontsize=12, pad=12)
    ax.set_ylabel("SPY Price")
    ax.legend(loc="best", framealpha=0.3)

    out = OUT_DIR / f"bear_trigger_timeline_{ts}.png"
    save_fig(fig, out)
    return out


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main():
    print("=" * 70)
    print("QuantCore Robustness — 壓力情境分析")
    print("=" * 70)

    setup_dark_theme()
    ts = timestamp()

    print(f"\n載入 baseline_returns.csv ...")
    returns = load_baseline()
    print(f"  {len(returns)} rows | {returns.index.min().date()} → {returns.index.max().date()}")
    print(f"  columns: {list(returns.columns)}")

    # 自動偵測 2025 大回撤
    auto_2025 = detect_2025_drawdown(returns)
    if auto_2025 is not None:
        print(f"\n偵測到 2025 年大回撤：{auto_2025['name']}")
        SCENARIOS.insert(5, auto_2025)
    else:
        print(f"\n2025 年無顯著回撤（< -5%）")

    # ── 逐情境分析 ──────────────────────────────────────────────────────
    all_rows = []
    print("\n" + "─" * 70)
    print("壓力情境分析")
    print("─" * 70)

    for sc in SCENARIOS:
        if not sc["in_range"]:
            print(f"\n[N/A] {sc['name']}  ({sc['start']} → {sc['end']})")
            print(f"       原因：{sc.get('reason', 'out of range')}")
            continue

        print(f"\n[情境] {sc['name']}")
        print(f"       指定期間：{sc['start']} → {sc['end']}")

        rows = analyze_scenario(returns, sc)
        if not rows:
            print("       (資料為空，跳過)")
            continue

        all_rows.extend(rows)
        print(f"       實際期間：{rows[0]['period']}  ({rows[0]['days']} 天)")
        for r in rows:
            excess_tag = f"excess {r['excess_ret']:+.1%} vs SPY"
            print(f"       {r['strategy']:<18} cum {r['cum_ret']:+.1%}  "
                  f"MDD {r['mdd']:.1%}  vol×{r['vol_mult']:.2f}  {excess_tag}")

        # 繪圖
        png = plot_scenario_equity(returns, sc, ts)
        if png:
            print(f"       → {png.name}")

    summary_df = pd.DataFrame(all_rows)

    # ── 彙整 CSV ────────────────────────────────────────────────────────
    if not summary_df.empty:
        summary_csv = OUT_DIR / f"stress_summary_{ts}.csv"
        summary_df.to_csv(summary_csv, index=False, float_format="%.6f")
        print(f"\n[CSV] {summary_csv.name}  ({len(summary_df)} rows)")

        # 彙整圖
        png = plot_summary_chart(summary_df, ts)
        if png:
            print(f"[圖] {png.name}")

    # ── 熊市觸發分析 ────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("SP500 熊市機制 (SPY < MA200) 觸發分析")
    print("─" * 70)

    triggers_df, spy_ma_df = analyze_bear_triggers(returns)
    if triggers_df.empty:
        print("  無觸發事件。")
    else:
        print(f"  總觸發次數：{len(triggers_df)}")
        reasonable_ct = int(triggers_df["trigger_reasonable"].sum())
        print(f"  觸發後市場確實下跌（合理）：{reasonable_ct} / {len(triggers_df)}  "
              f"({reasonable_ct / len(triggers_df):.0%})")

        print("\n  觸發事件（前 10 筆）：")
        for _, r in triggers_df.head(10).iterrows():
            tag = "OK" if r["trigger_reasonable"] else "FP"
            print(f"    [{tag}] {r['trigger_date']}  "
                  f"SPY {r['spy_close']:.2f} ({r['pct_vs_ma200']:+.1%} vs MA200)  "
                  f"30d SPY {r['spy_ret_30d']:+.1%}  SP500 {r['sp500_ret_30d']:+.1%}")

        trig_csv = OUT_DIR / f"bear_triggers_{ts}.csv"
        triggers_df.to_csv(trig_csv, index=False, float_format="%.6f")
        print(f"\n  [CSV] {trig_csv.name}")

        png = plot_bear_trigger_timeline(spy_ma_df, triggers_df, ts)
        if png:
            print(f"  [圖] {png.name}")

    # ── 結論 ────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("關鍵發現摘要")
    print("=" * 70)
    if not summary_df.empty:
        # 各情境 Combo vs SPY 超額
        for sc_name, grp in summary_df.groupby("scenario_name", sort=False):
            combo_row = grp[grp["strategy"] == "Combo_80_50_1.3x"]
            spy_row   = grp[grp["strategy"] == "SPY"]
            if combo_row.empty or spy_row.empty:
                continue
            c = combo_row.iloc[0]
            print(f"\n  {sc_name}")
            print(f"    Combo cum {c['cum_ret']:+.1%} / MDD {c['mdd']:.1%} / "
                  f"超額 {c['excess_ret']:+.1%} vs SPY")
            # 復原
            if c["recovered"]:
                print(f"    Combo 已復原：峰→谷 {c['peak_to_trough_days']} 天，谷→復原 {c['recovery_days']} 天")
            else:
                print(f"    Combo 未於情境期內復原（谷至期末 {c['recovery_days']} 天）")

    print("\n完成。")


if __name__ == "__main__":
    main()
