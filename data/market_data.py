"""本地行情 CSV 的讀取契約只有一份（#229）。

讀取端（策略的 data loader、SP500 選股、參考價的 CSV 後備、預演）都經過這裡：

- 資料根目錄：`data_root()`；ETF 在 `etfs/`、個股在 `stocks/`。
- 檔名：`{provider symbol}.csv`（`BRK.B` → `BRK-B`）；有 session 時才套 #56 的
  歷史別名，讀到的 bar 也只留別名有效區間內的。
- 讀法：index 是 `Date`（正規化到日）、同日重複留最後一筆、依日期排序。
  `kind="total_return"` 套 `price_semantics.total_return_ohlc`（`Close`／`Adj Close`
  語意見 `docs/etf-price-semantics.md`）。
- 缺檔、讀不到、空檔、沒有 Close 欄：回傳明確的 `BarsRead.problem`，並寫 log
  （缺檔 INFO、其餘 WARNING）。要不要 raise、改用什麼來源由呼叫端決定。

寫入端（`data/update_data.py`）與離線稽核（`price_continuity`、`health`）不經過這裡。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from data.historical_aliases import HistoricalAlias, alias_for
from data.price_semantics import total_return_ohlc

log = logging.getLogger(__name__)

DATA_ROOT = Path(__file__).resolve().parent

MISSING = "missing"
UNREADABLE = "unreadable"
EMPTY = "empty"
NO_CLOSE = "no Close column"
OUTSIDE_ALIAS = "no bars inside the historical alias window"
BAD_CLOSE = "last Close is not a positive number"


def data_root() -> Path:
    """本地行情資料的根目錄（底下是 etfs/、stocks/、reference/）。"""
    return DATA_ROOT


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


def source_symbol(symbol: str, as_of=None) -> str:
    """Resolve a provider stem for a dated read.

    With no session this remains the format-only contract.  A session is
    required before one of the explicit #56 historical aliases can apply.
    """
    alias = historical_alias(symbol, as_of)
    return alias.provider_symbol if alias else provider_symbol(symbol)


def csv_path(folder: str | Path, symbol: str, as_of=None) -> Path:
    """`folder` 裡這個代碼的行情檔；`as_of` 給了才可能套歷史別名。"""
    return Path(folder) / f"{source_symbol(symbol, as_of)}.csv"


# ── 讀檔 ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BarsRead:
    """一次讀檔的結果：`frame` 只在 `problem is None` 時有值。"""
    path: Path
    frame: pd.DataFrame | None
    problem: str | None = None
    error: Exception | None = None

    @property
    def ok(self) -> bool:
        return self.problem is None


def report(path: Path, problem: str, error: Exception | None = None) -> BarsRead:
    """寫一行 log（缺檔 INFO、其餘 WARNING），回傳這個失敗的 BarsRead。"""
    if problem == MISSING:
        log.info(f"行情 CSV 缺檔：{path}")
    elif error is not None:
        log.warning(f"行情 CSV 讀不到：{path}（{type(error).__name__}: {error}）")
    else:
        log.warning(f"行情 CSV 不能用：{path}（{problem}）")
    return BarsRead(path, None, problem, error)


def read_bars(path: str | Path, *, kind: str = "raw") -> BarsRead:
    """讀一個行情 CSV。`kind`：`"raw"`（檔案原樣）或 `"total_return"`。"""
    if kind not in ("raw", "total_return"):
        raise ValueError(f"unknown bars kind: {kind!r}")
    path = Path(path)
    if not path.exists():
        return report(path, MISSING)
    try:
        frame = pd.read_csv(path, index_col="Date", parse_dates=True)
        frame.index = pd.to_datetime(frame.index).normalize()
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    except Exception as e:
        return report(path, UNREADABLE, e)
    if frame.empty:
        return report(path, EMPTY)
    if "Close" not in frame.columns:
        return report(path, NO_CLOSE)
    if kind == "total_return":
        frame = total_return_ohlc(frame)
    return BarsRead(path, frame)


def bars(folder: str | Path, symbol: str, *, as_of=None, kind: str = "raw") -> BarsRead:
    """`folder` 裡一個代碼的行情；`as_of` 給了且有歷史別名時，只留別名有效區間的 bar。"""
    read = read_bars(csv_path(folder, symbol, as_of), kind=kind)
    if not read.ok or historical_alias(symbol, as_of) is None:
        return read
    frame = alias_window(read.frame, symbol, as_of)
    if frame.empty:
        return report(read.path, OUTSIDE_ALIAS)
    return BarsRead(read.path, frame)


def alias_window(frame: pd.DataFrame, symbol: str, as_of=None) -> pd.DataFrame:
    """歷史別名（#56）只在它的 session 界限內有效：界限外的 bar 不算這個代碼的。"""
    alias = historical_alias(symbol, as_of)
    if alias is None:
        return frame
    return frame[(frame.index >= pd.Timestamp(alias.valid_from)) &
                 (frame.index <= pd.Timestamp(alias.valid_to))]


def require_bars(folder: str | Path, symbol: str, *, kind: str = "raw") -> pd.DataFrame:
    """策略 data loader 用：讀不到就 raise（缺檔 FileNotFoundError，壞檔原例外）。"""
    read = bars(folder, symbol, kind=kind)
    if read.ok:
        return read.frame
    if read.problem == MISSING:
        raise FileNotFoundError(
            f"找不到資料檔：{read.path}\n"
            f"請先執行 `python automation/update_data.py`"
        )
    if read.error is not None:
        raise read.error
    raise ValueError(f"資料檔不能用：{read.path}（{read.problem}）")


def last_close(symbol: str, *, root: str | Path | None = None) -> float | None:
    """最近一筆收盤（`Close` 去掉空值後的最後一筆）：先找 etfs/、再找 stocks/。

    兩處都沒有檔、或找到的檔不能用 → None（已寫 log）。etfs/ 的最後收盤不是正數時
    照舊再找 stocks/。
    """
    base = Path(root) if root is not None else data_root()
    found = False
    for folder in ("etfs", "stocks"):
        path = csv_path(base / folder, symbol)
        if not path.exists():
            continue
        found = True
        read = read_bars(path)
        if not read.ok:
            return None
        closes = read.frame["Close"].dropna()
        if closes.empty:
            report(path, EMPTY)
            return None
        value = float(closes.iloc[-1])
        if math.isfinite(value) and value > 0:
            return value
        report(path, BAD_CLOSE)
    if not found:
        log.info(f"行情 CSV 缺檔：{symbol}（{base / 'etfs'}、{base / 'stocks'} 都沒有）")
    return None
