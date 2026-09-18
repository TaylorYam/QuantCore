"""
SP500 Momentum — 資料工具函數
資料格式已統一為 CSV：Date(index), Open, High, Low, Close, Volume
"""

import pandas as pd
from pathlib import Path

from data.historical_aliases import HistoricalAlias, alias_for


# ── 代碼命名契約 ──────────────────────────────────────────────────────────────
# Constituents symbol：S&P / Wikipedia 寫法，類股以 "." 分隔（BRK.B）。
# Provider symbol：Yahoo 寫法，類股以 "-" 分隔（BRK-B）；update_data 以檔名當下載代碼。
# 本地檔名：{provider symbol}.csv。
# 一般只做同一證券的格式轉換；#56 的明確日期別名由 historical_aliases
# 提供，改名、合併、代碼重用仍不由 provider metadata 自動推論。

def provider_symbol(symbol: str) -> str:
    """Constituents symbol → provider (Yahoo) symbol / 本地檔名 stem。"""
    return symbol.strip().replace(".", "-")


def historical_alias(symbol: str, as_of=None) -> HistoricalAlias | None:
    """Return the reviewed historical identity active on a session, if any."""
    return alias_for(symbol, as_of) if as_of is not None else None


def data_provider_symbol(symbol: str, as_of=None) -> str:
    """Resolve a provider stem for a dated read.

    With no session this remains the format-only contract.  A session is
    required before one of the explicit #56 historical aliases can apply.
    """
    alias = historical_alias(symbol, as_of)
    return alias.provider_symbol if alias else provider_symbol(symbol)


def stock_csv_path(stock_dir: str | Path, symbol: str, as_of=None) -> Path:
    """Return the local price file for a Constituents symbol.

    ``as_of`` is deliberately optional for compatibility with callers that
    only need the ordinary format conversion.  Historical aliases are used
    only when the caller supplies the session being read.
    """
    return Path(stock_dir) / f"{data_provider_symbol(symbol, as_of)}.csv"


def load_stock(path: str | Path) -> pd.DataFrame | None:
    """
    讀取股票 CSV（標準格式：Date, Open, High, Low, Close, Volume）。
    回傳 DataFrame；找不到或錯誤回傳 None。
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, index_col="Date", parse_dates=True)
        df.index = pd.to_datetime(df.index).normalize()
        df = df[~df.index.duplicated(keep="last")].sort_index()
        if df.empty or "Close" not in df.columns:
            return None
        return df
    except Exception:
        return None


def load_benchmark(path: str | Path) -> pd.DataFrame | None:
    """
    讀取基準 ETF CSV（SPY, SSO 等）。
    回傳包含 Open, Close 的 DataFrame。
    """
    return load_stock(path)


def filter_entry_eligible(ranked: list, exclude=()) -> list:
    """進場資格過濾：排除 `exclude` 與「收盤 < EXIT_EMA」的標的。

    僅在 `config.ENTRY_EMA_FILTER = True` 時被使用。WF 驗證（ADR-0005）
    否決了這個過濾 — rank 優先：高排名股同輪 EMA 賣出後買回（=續抱）
    在 OOS 勝出，故預設關閉。函式保留供未來重驗
    （robustness/wf_ema_entry_filter.py 即用此 flag 做 A/B）。

    `ranked`：scan_market 的 metric dict list（含 ticker/price/exit_ema），
    保留原排序；price 或 exit_ema 缺值（<=0）時不判 EMA、視為合格
    （與賣出側的判斷對稱）。
    """
    ex = set(exclude)
    out = []
    for x in ranked:
        if x.get("ticker") in ex:
            continue
        px, ema = x.get("price", 0), x.get("exit_ema", 0)
        if px > 0 and ema > 0 and px < ema:
            continue
        out.append(x)
    return out
