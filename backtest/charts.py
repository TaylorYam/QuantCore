"""
QuantCore — 權益曲線圖表
輸出：output/equity_curve.png
"""

import matplotlib
matplotlib.use("Agg")   # 無顯示環境也能存圖

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
import numpy as np
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ── 顏色與樣式 ────────────────────────────────────────────────────────────────

STYLES = {
    "100/0 SR":                    {"color": "#2563eb", "lw": 2.0, "ls": "-",  "zorder": 4},
    "0/100 SP500":                 {"color": "#16a34a", "lw": 2.0, "ls": "-",  "zorder": 4},
    "50/50 Monthly":               {"color": "#dc2626", "lw": 2.5, "ls": "-",  "zorder": 5},
    "80%/50% Monthly 1.3x":        {"color": "#f97316", "lw": 2.5, "ls": "-",  "zorder": 6},
    "SPY B&H":                     {"color": "#9ca3af", "lw": 1.5, "ls": "--", "zorder": 3},
}

DEFAULT_STYLE = {"color": "#6b7280", "lw": 1.5, "ls": ":", "zorder": 2}


# ── 主繪圖函式 ────────────────────────────────────────────────────────────────

def plot_equity_curves(
    equity: pd.DataFrame,
    title: str = "QuantCore — Equity Curves",
    save_path: Path = None,
    show: bool = False,
) -> Path:
    """
    繪製多策略權益曲線（Log Y 軸）並存檔。

    Parameters
    ----------
    equity    : DataFrame，columns = 策略名稱，values = 累積淨值（起始 1.0）
    title     : 圖標題
    save_path : 存檔路徑；None = OUTPUT_DIR/equity_curve.png
    show      : 是否呼叫 plt.show()（互動模式用）

    Returns
    -------
    save_path : Path
    """
    if save_path is None:
        save_path = OUTPUT_DIR / "equity_curve.png"

    fig, axes = plt.subplots(
        2, 1,
        figsize=(14, 9),
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.05},
        sharex=True,
    )

    ax_eq, ax_dd = axes

    for col in equity.columns:
        curve = equity[col].dropna()
        style = STYLES.get(col, DEFAULT_STYLE)
        ax_eq.plot(curve.index, curve.values, label=col, **style)

        # 回撤
        dd = curve / curve.cummax() - 1
        ax_dd.fill_between(dd.index, dd.values, 0,
                           alpha=0.25, color=style["color"])
        ax_dd.plot(dd.index, dd.values, color=style["color"],
                   lw=0.8, alpha=0.6)

    # ── 上圖：權益曲線 ────────────────────────────────────────────────────────
    ax_eq.set_yscale("log")
    ax_eq.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda y, _: f"{y:.1f}×")
    )
    ax_eq.set_title(title, fontsize=14, fontweight="bold", pad=12)
    ax_eq.set_ylabel("Net Asset Value (initial = 1.0)", fontsize=9)
    ax_eq.legend(loc="upper left", fontsize=9, framealpha=0.8)
    ax_eq.grid(True, which="major", ls="--", alpha=0.4)
    ax_eq.grid(True, which="minor", ls=":", alpha=0.2)
    ax_eq.axhline(1.0, color="#374151", lw=0.8, ls="-", alpha=0.5)

    # ── 下圖：回撤 ────────────────────────────────────────────────────────────
    ax_dd.set_ylabel("Drawdown", fontsize=9)
    ax_dd.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda y, _: f"{y:.0%}")
    )
    ax_dd.axhline(0, color="#374151", lw=0.8, alpha=0.5)
    ax_dd.grid(True, ls="--", alpha=0.3)
    ax_dd.set_xlabel("Date", fontsize=9)

    # ── 年份背景 ──────────────────────────────────────────────────────────────
    _add_year_bands(ax_eq, equity.index)

    plt.tight_layout()
    fig.savefig(save_path, dpi=150, bbox_inches="tight")
    if show:
        plt.show()
    plt.close(fig)

    print(f"圖表已存：{save_path}")
    return save_path


# ── 年份條紋背景 ──────────────────────────────────────────────────────────────

def _add_year_bands(ax, idx):
    """交替灰白背景，標示年份。"""
    if idx is None or len(idx) == 0:
        return
    years = sorted(set(idx.year))
    for i, yr in enumerate(years):
        yr_idx = idx[idx.year == yr]
        if len(yr_idx) == 0:
            continue
        x0 = yr_idx[0]
        x1 = yr_idx[-1]
        if i % 2 == 0:
            ax.axvspan(x0, x1, facecolor="#f3f4f6", alpha=0.4, zorder=0)
        ax.text(
            x0 + (x1 - x0) / 2, ax.get_ylim()[0],
            str(yr), ha="center", va="bottom",
            fontsize=7, color="#9ca3af", zorder=1,
        )
