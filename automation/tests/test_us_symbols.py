"""美股代號清單（#140）：解析、搜尋、快取與下載失敗時的退路。全部離線。"""

import os

import pytest

import manual_orders
import us_symbols
from us_symbols import SymbolDirectory, load_directory, parse_symbol_files

NASDAQ = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
SKHY|SK hynix Inc. - American Depositary Shares|Q|N|N|100|N|N
SKUU|GraniteShares 2x Long SK Hynix Daily ETF|G|N|N|100|Y|N
ZXZZT|NASDAQ TEST STOCK|G|Y|N|100|N|N
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
File Creation Time: 0929202606:00|||||||
"""

OTHER = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
A|Agilent Technologies, Inc. Common Stock|N|A|N|100|N|A
BRK.B|Berkshire Hathaway Inc. New Common Stock|N|BRK.B|N|40|N|BRK.B
ABR$D|Arbor Realty Trust 6.375% Series D Preferred Stock|N|ABRpD|N|100|N|ABR-D
SKHU|ProShares Ultra SK hynix|P|SKHU|Y|100|N|SKHU
ZZZTX|TXSE Test Stk 5 Common Stock|F|ZZZTX|N|40|Y|ZZZTX
File Creation Time: 0929202606:00||||||
"""


def test_parse_keeps_orderable_listed_symbols_only():
    symbols = parse_symbol_files(NASDAQ, OTHER)
    assert symbols == {
        "SKHY": "SK hynix Inc. - American Depositary Shares",
        "SKUU": "GraniteShares 2x Long SK Hynix Daily ETF",
        "AAPL": "Apple Inc. - Common Stock",
        "A": "Agilent Technologies, Inc. Common Stock",
        "BRK.B": "Berkshire Hathaway Inc. New Common Stock",
        "SKHU": "ProShares Ultra SK hynix",
    }


def test_orderable_pattern_matches_manual_order_service():
    assert us_symbols.ORDERABLE_SYMBOL_RE.pattern == manual_orders._SYMBOL_RE.pattern


def test_parse_rejects_unexpected_columns():
    with pytest.raises(ValueError):
        parse_symbol_files("Ticker|Name\nAAPL|Apple\n", OTHER)


def test_search_exact_then_prefix_then_name():
    directory = SymbolDirectory(parse_symbol_files(NASDAQ, OTHER))
    assert [s for s, _ in directory.search("skh")] == ["SKHU", "SKHY"]
    assert directory.search("SKHY")[0] == (
        "SKHY", "SK hynix Inc. - American Depositary Shares",
    )
    # 名稱包含：打 hynix 也找得到代號
    assert [s for s, _ in directory.search("hynix")] == ["SKHU", "SKHY", "SKUU"]
    assert [s for s, _ in directory.search("a")] == ["A", "AAPL"]
    assert directory.search("SK", limit=2) == directory.search("SK")[:2]
    assert directory.search("  ") == []
    assert directory.name_for(" brk.b ") == "Berkshire Hathaway Inc. New Common Stock"
    assert directory.name_for("NOPE") is None


def _letters(i):
    return "".join("ABCDEFGHIJKLMNOPQRSTUVWXYZ"[(i // 26 ** k) % 26] for k in range(4))


def _big_files():
    """夠大的假清單（超過 MIN_SYMBOLS），另加 NASDAQ/OTHER 的 6 檔。"""
    rows = "".join(
        f"Q{_letters(i)}|Company {i}|Q|N|N|100|N|N\n"
        for i in range(us_symbols.MIN_SYMBOLS)
    )
    return {us_symbols.NASDAQ_LISTED_URL: NASDAQ + rows,
            us_symbols.OTHER_LISTED_URL: OTHER}


def test_stale_cache_is_refreshed_and_fresh_cache_is_not_downloaded(tmp_path):
    path = tmp_path / "us_symbols.json"
    files = _big_files()
    calls = []

    def download(url):
        calls.append(url)
        return files[url]

    first = load_directory(path, download=download)
    assert len(first) == us_symbols.MIN_SYMBOLS + 6
    assert path.exists() and len(calls) == 2

    again = load_directory(path, download=download)
    assert len(again) == len(first) and len(calls) == 2

    old = path.stat().st_mtime - us_symbols.MAX_AGE_SECONDS - 1
    os.utime(path, (old, old))
    load_directory(path, download=download)
    assert len(calls) == 4


def test_download_failure_keeps_old_cache_and_no_cache_returns_none(tmp_path):
    path = tmp_path / "us_symbols.json"

    def offline(_url):
        raise OSError("no network")

    assert load_directory(path, download=offline) is None

    load_directory(path, download=lambda url: _big_files()[url])
    old = path.stat().st_mtime - us_symbols.MAX_AGE_SECONDS - 1
    os.utime(path, (old, old))
    kept = load_directory(path, download=offline)
    assert kept is not None and kept.name_for("SKHY")


def test_truncated_download_does_not_overwrite_cache(tmp_path):
    path = tmp_path / "us_symbols.json"
    load_directory(path, download=lambda url: _big_files()[url])
    before = path.read_bytes()
    old = path.stat().st_mtime - us_symbols.MAX_AGE_SECONDS - 1
    os.utime(path, (old, old))
    small = {us_symbols.NASDAQ_LISTED_URL: NASDAQ, us_symbols.OTHER_LISTED_URL: OTHER}
    kept = load_directory(path, download=lambda url: small[url])
    assert path.read_bytes() == before
    assert len(kept) == us_symbols.MIN_SYMBOLS + 6
