"""
本地行情 CSV 的讀取契約（data/market_data，#229）.

未排序、同日重複、NaN 尾、缺檔、壞檔、類股檔名、歷史別名界限內外、raw 與
total_return 各在這裡考一次；讀取端（策略 loader、SP500 選股、CSV 參考價、預演）
只是 caller。No IB, no network.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytest

from data import market_data as md

HEADER = "Date,Open,High,Low,Close,Adj Close,Volume\n"


def _write(folder: Path, stem: str, rows: list[str]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.csv"
    path.write_text(HEADER + "".join(r + "\n" for r in rows), encoding="utf-8")
    return path


def test_data_root_is_the_data_package_and_the_strategy_folders_hang_off_it():
    from strategies.sector_rotation import config as sr
    from strategies.sector_skew_ls import config as sk
    from strategies.sp500_momentum import config as sp

    root = md.data_root()
    assert root == Path(md.__file__).resolve().parent
    assert sr.DATA_DIR == sk.DATA_DIR == sp.ETF_DIR == root / "etfs"
    assert sp.STOCK_DIR == root / "stocks" and sp.REF_DIR == root / "reference"


@pytest.mark.parametrize("symbol, as_of, stem", [
    ("BRK.B", None, "BRK-B"),
    (" XLK ", None, "XLK"),
    ("GPS", None, "GPS"),                      # 沒給 session：只做格式轉換
    ("GPS", "2022-02-02", "GAP"),              # 別名界限內
    ("GPS", "2022-02-03", "GPS"),              # 界限外
])
def test_file_names_follow_the_provider_contract_and_dated_aliases(symbol, as_of, stem):
    assert md.csv_path("stocks", symbol, as_of) == Path("stocks") / f"{stem}.csv"


def test_bars_are_sorted_deduplicated_and_normalized(tmp_path):
    _write(tmp_path, "ZZZ", ["2026-10-06,1,1,1,12,12,1",
                            "2026-10-05,1,1,1,10,10,1",
                            "2026-10-06,1,1,1,13,13,1"])
    read = md.bars(tmp_path, "ZZZ")
    assert read.ok and read.path == tmp_path / "ZZZ.csv"
    assert list(read.frame.index) == [pd.Timestamp("2026-10-05"), pd.Timestamp("2026-10-06")]
    assert list(read.frame["Close"]) == [10, 13]          # 同日留最後一筆


def test_total_return_kind_uses_adjusted_ohlc(tmp_path):
    _write(tmp_path, "ZZZ", ["2026-01-02,100,110,90,100,95,1"])
    raw = md.bars(tmp_path, "ZZZ").frame
    tr = md.bars(tmp_path, "ZZZ", kind="total_return").frame
    assert raw.loc["2026-01-02", "Close"] == 100
    assert tr.loc["2026-01-02", "Close"] == 95
    assert tr.loc["2026-01-02", "Open"] == pytest.approx(95)
    with pytest.raises(ValueError):
        md.bars(tmp_path, "ZZZ", kind="adjusted")


@pytest.mark.parametrize("content, problem, level", [
    (None, md.MISSING, "INFO"),
    ("not,a\ncsv", md.UNREADABLE, "WARNING"),
    (HEADER, md.EMPTY, "WARNING"),
    ("Date,Open\n2026-10-06,1\n", md.NO_CLOSE, "WARNING"),
])
def test_unusable_files_are_explicit_and_logged(tmp_path, caplog, content, problem, level):
    if content is not None:
        (tmp_path / "ZZZ.csv").write_text(content, encoding="utf-8")
    with caplog.at_level(logging.INFO, logger=md.log.name):
        read = md.bars(tmp_path, "ZZZ")
    assert not read.ok and read.frame is None and read.problem == problem
    assert [r.levelname for r in caplog.records] == [level]
    assert "ZZZ.csv" in caplog.text


def test_alias_window_keeps_only_the_reviewed_sessions(tmp_path, caplog):
    _write(tmp_path, "GAP", ["2014-12-31,1,1,1,1,1,1", "2015-01-02,1,1,1,2,2,1",
                            "2022-02-02,1,1,1,3,3,1", "2022-02-03,1,1,1,4,4,1"])
    inside = md.bars(tmp_path, "GPS", as_of="2022-02-02")
    assert inside.ok and list(inside.frame["Close"]) == [2, 3]
    assert md.bars(tmp_path, "GAP").frame["Close"].tolist() == [1, 2, 3, 4]

    _write(tmp_path, "GAP", ["2023-01-03,1,1,1,9,9,1"])     # 檔案只有界限外的 bar
    with caplog.at_level(logging.WARNING, logger=md.log.name):
        outside = md.bars(tmp_path, "GPS", as_of="2022-02-02")
    assert outside.problem == md.OUTSIDE_ALIAS and "GAP.csv" in caplog.text


def test_require_bars_raises_like_the_strategy_loaders(tmp_path):
    with pytest.raises(FileNotFoundError, match="找不到資料檔"):
        md.require_bars(tmp_path, "XLK")
    (tmp_path / "BAD.csv").write_text("not,a\ncsv", encoding="utf-8")
    with pytest.raises(ValueError):                       # 原本 read_csv 的例外
        md.require_bars(tmp_path, "BAD")
    _write(tmp_path, "XLK", ["2026-10-06,1,1,1,5,5,1"])
    assert md.require_bars(tmp_path, "XLK")["Close"].iloc[-1] == 5


def test_last_close_skips_a_nan_tail_and_prefers_etfs(tmp_path):
    _write(tmp_path / "etfs", "XLK", ["2026-10-05,1,1,1,10,10,1", "2026-10-06,1,1,1,,,1"])
    _write(tmp_path / "stocks", "BRK-B", ["2026-10-06,1,1,1,500,500,1"])
    _write(tmp_path / "stocks", "XLK", ["2026-10-06,1,1,1,99,99,1"])
    assert md.last_close("XLK", root=tmp_path) == 10.0
    assert md.last_close("BRK.B", root=tmp_path) == 500.0


def test_last_close_missing_everywhere_or_all_nan_is_none_and_logged(tmp_path, caplog):
    _write(tmp_path / "etfs", "NAN", ["2026-10-06,1,1,1,,,1"])
    with caplog.at_level(logging.INFO, logger=md.log.name):
        assert md.last_close("BTC", root=tmp_path) is None
        assert md.last_close("NAN", root=tmp_path) is None
    assert [r.levelname for r in caplog.records] == ["INFO", "WARNING"]
    assert "BTC" in caplog.records[0].getMessage()
    assert "NAN.csv" in caplog.records[1].getMessage()


def test_last_close_falls_through_to_stocks_when_the_etf_close_is_not_positive(tmp_path, caplog):
    _write(tmp_path / "etfs", "ZZZ", ["2026-10-06,1,1,1,0,0,1"])
    _write(tmp_path / "stocks", "ZZZ", ["2026-10-06,1,1,1,42,42,1"])
    with caplog.at_level(logging.WARNING, logger=md.log.name):
        assert md.last_close("ZZZ", root=tmp_path) == 42.0
    assert md.BAD_CLOSE in caplog.text
    (tmp_path / "stocks" / "ZZZ.csv").unlink()
    assert md.last_close("ZZZ", root=tmp_path) is None


def test_sp500_selection_reads_and_logs_a_missing_file_once(tmp_path, monkeypatch, caplog):
    """回測每個 session 都會問同一檔：缺檔只讀一次、只記一行（#229 review）。"""
    from strategies.sp500_momentum import config, utils
    from strategies.sp500_momentum.selection import SelectionEngine

    monkeypatch.setattr(config, "STOCK_DIR", tmp_path)
    engine = SelectionEngine.__new__(SelectionEngine)
    engine.data_cache, engine._unusable = {}, {}
    reads = []
    monkeypatch.setattr(utils, "load_stock", lambda p: reads.append(p) or md.read_bars(p).frame)
    with caplog.at_level(logging.INFO, logger=md.log.name):
        assert engine._get_ticker_data("ZZZ") is None
        assert engine._get_ticker_data("ZZZ") is None
    assert len(reads) == 1 and len(caplog.records) == 1 and engine.data_cache == {}


def test_sp500_selection_logs_bars_outside_the_alias_window(tmp_path, monkeypatch, caplog):
    from strategies.sp500_momentum import config
    from strategies.sp500_momentum.selection import SelectionEngine

    _write(tmp_path, "GAP", ["2023-01-03,1,1,1,9,9,1"])
    monkeypatch.setattr(config, "STOCK_DIR", tmp_path)
    engine = SelectionEngine.__new__(SelectionEngine)
    engine.data_cache, engine._unusable = {}, {}
    with caplog.at_level(logging.WARNING, logger=md.log.name):
        assert engine._get_ticker_data("GPS", as_of="2022-02-02") is None
    assert md.OUTSIDE_ALIAS in caplog.text and "GAP.csv" in caplog.text


def test_last_close_defaults_to_data_root(tmp_path, monkeypatch):
    _write(tmp_path / "etfs", "ZZZ", ["2026-10-06,1,1,1,7,7,1"])
    monkeypatch.setattr(md, "DATA_ROOT", tmp_path)
    assert md.last_close("ZZZ") == 7.0
