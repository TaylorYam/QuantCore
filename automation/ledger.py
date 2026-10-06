"""交易紀錄的讀取端（#209）：每日 NAV 與最近成交。

寫端是 `excel_logger`；欄位名稱用它的常數，這裡和寫端以外的地方都不直接寫欄位名稱。
來源目前是 Excel 檔（portfolio_tracker.xlsx／trade_log.xlsx；SQLite 後端時是它匯出的
相容投影）。之後讀端要改讀 SQLite，只改這個 module。

只讀檔、不連 IB、沒有 GUI。檔案不存在 → 空結果；檔案壞掉、被鎖住 → 例外照丟，
由呼叫端決定怎麼顯示。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

import excel_logger as xl


@dataclass(frozen=True)
class TradeRow:
    """trade_log.xlsx 的一筆委託；strategy 是寫入時記的 Strategy 欄，沒有再判讀。"""
    datetime: str
    symbol: str
    action: str
    qty: int
    fill_price: float           # 沒成交是 NaN；讀不懂的值是 0.0
    strategy: str
    status: str
    dry_run: bool


def daily_nav(account: str, *, path: Path | None = None) -> pd.Series:
    """某帳戶的每日 NAV（DatetimeIndex，舊到新，一天一個值；同一天多筆取第一筆）。"""
    path = xl.PORTFOLIO_FILE if path is None else path
    if not path.exists():
        return pd.Series(dtype=float)
    df = pd.read_excel(path, sheet_name=xl.PORT_SHEET)
    if xl.ACCOUNT in df.columns:
        df = df[df[xl.ACCOUNT] == account]
    if df.empty:
        return pd.Series(dtype=float)
    nav = df.groupby(xl.DATE)[xl.NAV].first()
    nav.index = pd.to_datetime(nav.index)
    nav = nav.sort_index()
    # Excel 讀入可能混合字串（空白 cell、舊資料）；object dtype 會讓下游比較丟 TypeError
    return pd.to_numeric(nav, errors="coerce").dropna()


def recent_trades(account: str, days: int = 7, *, path: Path | None = None) -> list[TradeRow]:
    """某帳戶最近 days 天（含今天往回第 days 天）的委託，新到舊。"""
    path = xl.TRADE_FILE if path is None else path
    if not path.exists():
        return []
    df = pd.read_excel(path, sheet_name=xl.TRADE_SHEET)
    if xl.ACCOUNT in df.columns:
        df = df[df[xl.ACCOUNT] == account]
    if df.empty:
        return []
    dates = pd.to_datetime(df[xl.DATE], errors="coerce")
    cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=days)
    recent = df[dates >= cutoff].iloc[::-1]
    return [_trade(row) for _, row in recent.iterrows()]


def _trade(row: pd.Series) -> TradeRow:
    try:
        fill_price = float(row.get(xl.FILL_PRICE, 0))
    except (ValueError, TypeError):
        fill_price = 0.0
    return TradeRow(
        datetime=str(row.get(xl.DATETIME, "")),
        symbol=str(row[xl.SYMBOL]),
        action=str(row[xl.ACTION]),
        qty=int(row[xl.QTY]),
        fill_price=fill_price,
        strategy=str(row.get(xl.STRATEGY, "")),
        status=str(row.get(xl.FILL_STATUS, "")),
        dry_run=bool(row.get(xl.DRY_RUN, True)),
    )
