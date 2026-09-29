"""
QuantCore — 手動下單的美股代號清單（#140）

來源是 Nasdaq Trader 公開的 Symbol Directory（``nasdaqlisted.txt`` +
``otherlisted.txt``）：全美上市股票與 ETF，只有英文名稱。清單只用來在
Panel 提示「這個代號是哪家公司」，不是下單資格——最終仍由 IB 合約檢查決定，
所以下載失敗或清單沒有某個代號都只會少了提示，不會擋單。

快取存在 ``automation/us_symbols.json``（.gitignore），超過一天才重新下載。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
DEFAULT_CACHE_PATH = Path(__file__).resolve().parent / "us_symbols.json"
MAX_AGE_SECONDS = 24 * 60 * 60
# 完整清單約 1.2 萬檔；少於這個數量代表下載或格式出錯，不覆蓋舊快取。
MIN_SYMBOLS = 5000
# 與 manual_orders._SYMBOL_RE 相同：下單服務不收的代號（特別股 ABR$D 等）不列入。
ORDERABLE_SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z])?$")


def _parse_directory(text: str, symbol_column: str) -> dict[str, str]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return {}
    header = lines[0].split("|")
    try:
        symbol_at = header.index(symbol_column)
        name_at = header.index("Security Name")
        test_at = header.index("Test Issue")
    except ValueError as exc:
        raise ValueError(f"代號清單欄位不符：{header}") from exc
    symbols = {}
    for line in lines[1:]:
        if line.startswith("File Creation Time"):
            continue
        fields = line.split("|")
        if len(fields) < len(header) or fields[test_at].strip() == "Y":
            continue
        symbol = fields[symbol_at].strip().upper()
        if ORDERABLE_SYMBOL_RE.fullmatch(symbol):
            symbols[symbol] = fields[name_at].strip()
    return symbols


def parse_symbol_files(nasdaq_listed: str, other_listed: str) -> dict[str, str]:
    """兩個 Nasdaq Trader 檔案 → {代號: 英文名稱}，排除 Test Issue 與不可下單格式。"""
    symbols = _parse_directory(other_listed, "ACT Symbol")
    symbols.update(_parse_directory(nasdaq_listed, "Symbol"))
    return symbols


class SymbolDirectory:
    """代號 → 名稱的查詢；搜尋順序：完全相符 → 代號開頭相符 → 名稱包含。"""

    def __init__(self, symbols: dict[str, str]):
        self._symbols = dict(symbols)
        self._sorted = sorted(self._symbols, key=lambda s: (len(s), s))

    def __len__(self) -> int:
        return len(self._symbols)

    def name_for(self, symbol: str) -> str | None:
        return self._symbols.get(str(symbol or "").strip().upper())

    def search(self, query: str, limit: int = 8) -> list[tuple[str, str]]:
        text = str(query or "").strip()
        if not text:
            return []
        upper = text.upper()
        found = [s for s in self._sorted if s.startswith(upper)][:limit]
        if len(found) < limit and len(text) >= 2:
            lower = text.casefold()
            seen = set(found)
            for symbol in self._sorted:
                if symbol not in seen and lower in self._symbols[symbol].casefold():
                    found.append(symbol)
                    if len(found) == limit:
                        break
        return [(symbol, self._symbols[symbol]) for symbol in found]


def _download(url: str, timeout: float = 20) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "QuantCore"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _read_cache(path: Path) -> dict[str, str] | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(k): str(v) for k, v in data["symbols"].items()}
    except FileNotFoundError:
        return None
    except Exception:
        log.warning("美股代號快取無法讀取：%s", path, exc_info=True)
        return None


def _write_cache(path: Path, symbols: dict[str, str]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"symbols": symbols}, fh, ensure_ascii=False)
    os.replace(tmp, path)


def load_directory(path: str | Path = DEFAULT_CACHE_PATH, *,
                   download=_download, now=time.time,
                   max_age: float = MAX_AGE_SECONDS) -> SymbolDirectory | None:
    """回傳代號清單；快取超過 max_age 才下載。下載失敗沿用舊快取，都沒有回 None。"""
    path = Path(path)
    cached = _read_cache(path)
    try:
        fresh = cached is not None and now() - path.stat().st_mtime < max_age
    except OSError:
        fresh = False
    if fresh:
        return SymbolDirectory(cached)
    try:
        symbols = parse_symbol_files(
            download(NASDAQ_LISTED_URL), download(OTHER_LISTED_URL),
        )
        if len(symbols) < MIN_SYMBOLS:
            raise ValueError(f"代號清單只有 {len(symbols)} 檔，疑似下載不完整")
        _write_cache(path, symbols)
        return SymbolDirectory(symbols)
    except Exception:
        log.warning("美股代號清單更新失敗，沿用舊快取", exc_info=True)
        return SymbolDirectory(cached) if cached else None
