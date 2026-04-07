"""
QuantCore — 權益曲線圖表產生器
產出與截圖一致的雙面板圖表：
  上半：權益曲線 + New High 紅點
  下半：回落幅度 (MDD) 紅色填滿
"""

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib import font_manager as fm
from pathlib import Path
from datetime import date

# ── 中文字型設定（Windows: Microsoft JhengHei）──────────────────────────────
_CJK_FONTS = ['Microsoft JhengHei', 'Microsoft YaHei', 'SimSun', 'Malgun Gothic']
for _fn in _CJK_FONTS:
    if any(_fn == f.name for f in fm.fontManager.ttflist):
        plt.rcParams['font.sans-serif'] = [_fn] + plt.rcParams['font.sans-serif']
        break
plt.rcParams['axes.unicode_minus'] = False

BASE_DIR       = Path(__file__).parent
PORTFOLIO_FILE = BASE_DIR / 'portfolio_tracker.xlsx'
CHART_DIR      = BASE_DIR / 'charts'


def load_equity_series(account_name: str = "") -> pd.Series:
    """
    從 portfolio_tracker.xlsx 讀取每日 NAV，回傳 DatetimeIndex Series。
    若指定 account_name，只篩選該帳戶的資料。
    """
    if not PORTFOLIO_FILE.exists():
        return pd.Series(dtype=float)
    df = pd.read_excel(PORTFOLIO_FILE, sheet_name='Daily')
    if df.empty:
        return pd.Series(dtype=float)
    # 多帳戶篩選（若 Account 欄位存在）
    if account_name and 'Account' in df.columns:
        df = df[df['Account'] == account_name]
    elif not account_name and 'Account' in df.columns:
        # 無指定帳戶 → 用空字串的舊資料（向下相容）
        has_blank = df[df['Account'].fillna('').eq('')].shape[0] > 0
        if not has_blank:
            # 全部都有帳戶名 → 取第一個帳戶
            df = df[df['Account'] == df['Account'].iloc[0]]
    if df.empty:
        return pd.Series(dtype=float)
    nav = df.groupby('Date')['NAV'].first()
    nav.index = pd.to_datetime(nav.index)
    nav = nav.sort_index()
    return nav


def generate_equity_chart(nav: pd.Series = None, save_path: str = None,
                          account_name: str = "") -> str:
    """
    產生權益曲線 + MDD 圖表，儲存為 PNG。

    Parameters
    ----------
    nav : pd.Series, optional
        NAV 時間序列；None 則自動從 Excel 讀取。
    save_path : str, optional
        儲存路徑；None 則自動命名。
    account_name : str, optional
        帳戶名稱，用於篩選資料和檔名。

    Returns
    -------
    str : 圖片檔案路徑
    """
    if nav is None:
        nav = load_equity_series(account_name)

    if nav.empty or len(nav) < 2:
        return ""

    CHART_DIR.mkdir(exist_ok=True)
    if save_path is None:
        acct_suffix = f"_{account_name}" if account_name else ""
        save_path = str(CHART_DIR / f"equity{acct_suffix}_{date.today().strftime('%Y%m%d')}.png")

    # ── 計算指標 ──────────────────────────────────────────────
    cummax = nav.cummax()
    drawdown = (nav - cummax) / cummax  # 負值

    # New High 標記：當日 NAV == 歷史最高
    is_new_high = nav >= cummax
    # 只標記首次達到該高點（避免持平時連續標記）
    new_high_mask = is_new_high & (~is_new_high.shift(1, fill_value=False) | (nav > nav.shift(1, fill_value=0)))

    # ── 繪圖 ──────────────────────────────────────────────────
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(14, 7),
        gridspec_kw={'height_ratios': [3, 1]},
        sharex=True,
    )
    fig.patch.set_facecolor('white')

    # ── 上半：權益曲線 ────────────────────────────────────────
    ax1.plot(nav.index, nav.values, color='#4472C4', linewidth=1.2, zorder=2)

    # New High 紅點
    nh = nav[new_high_mask]
    if not nh.empty:
        ax1.scatter(nh.index, nh.values, color='red', s=30, zorder=3, label='New High')
        ax1.legend(loc='upper left', fontsize=9, framealpha=0.8)

    title_suffix = f' [{account_name}]' if account_name else ''
    ax1.set_title(f'程式策略權益曲線{title_suffix}', fontsize=14, fontweight='bold', pad=10)
    ax1.set_ylabel('NAV (USD)', fontsize=10)
    ax1.grid(True, alpha=0.3, linestyle='--')
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'${x:,.0f}'))

    # ── 下半：MDD ─────────────────────────────────────────────
    ax2.fill_between(drawdown.index, drawdown.values, 0,
                     color='#E88B8B', alpha=0.6, zorder=2)
    ax2.plot(drawdown.index, drawdown.values, color='#C0504D', linewidth=0.8, zorder=3)

    ax2.set_title('回落幅度 (MDD)', fontsize=11, fontweight='bold', pad=6)
    ax2.set_ylabel('Drawdown', fontsize=10)
    ax2.grid(True, alpha=0.3, linestyle='--')
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'{x:.1%}'))
    ax2.set_ylim(top=0.005)  # 上緣略高於 0

    # X 軸日期格式
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
    ax2.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate(rotation=30, ha='right')

    # ── 標註統計 ──────────────────────────────────────────────
    total_return = (nav.iloc[-1] / nav.iloc[0] - 1)
    max_dd = drawdown.min()
    days = (nav.index[-1] - nav.index[0]).days
    ann_return = (1 + total_return) ** (365 / max(days, 1)) - 1 if days > 0 else 0

    stats_text = (
        f"累計報酬: {total_return:+.1%}  |  "
        f"年化: {ann_return:+.1%}  |  "
        f"最大回撤: {max_dd:.1%}  |  "
        f"天數: {days}d"
    )
    fig.text(0.5, 0.01, stats_text, ha='center', fontsize=9, color='#555555')

    plt.tight_layout(rect=[0, 0.03, 1, 1])
    fig.savefig(save_path, dpi=150, bbox_inches='tight',
                facecolor='white', edgecolor='none')
    plt.close(fig)

    return save_path


if __name__ == '__main__':
    path = generate_equity_chart()
    if path:
        print(f"Chart saved: {path}")
    else:
        print("Not enough data to generate chart.")
