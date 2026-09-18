"""Constituents symbol → provider symbol → local file naming contract (#50).

Format translation only (class-share '.' → '-'); renames, mergers and reused
tickers are never mapped. Synthetic files only.
"""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from data.session_coverage import audit
from strategies.sp500_momentum import config, utils
from strategies.sp500_momentum.selection import SelectionEngine


@pytest.mark.parametrize("symbol, provider", [
    ("BRK.B", "BRK-B"), ("BF.B", "BF-B"), ("A", "A"), ("MRSH", "MRSH"),
    # The default path stays format-only; #56 aliases require an explicit date.
    ("FB", "FB"), ("GPS", "GPS"), ("MMC", "MMC")])
def test_provider_symbol_is_format_only(symbol, provider):
    assert utils.provider_symbol(symbol) == provider
    assert utils.stock_csv_path("x", symbol).name == f"{provider}.csv"


def _bars(folder, name, days, close=10.0):
    folder.mkdir(parents=True, exist_ok=True)
    fields = {c: [close] * len(days) for c in ("Open", "High", "Low", "Close", "Adj Close", "Volume")}
    pd.DataFrame({"Date": days, **fields}).to_csv(folder / f"{name}.csv", index=False)


def _references(root, days, snapshot):
    ref = root / "reference"
    ref.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": pd.to_datetime(days)}).to_excel(ref / "us_trading_days.xlsx", index=False)
    rows = {"Date": pd.to_datetime(days)}
    rows.update({f"T{i + 1}": [t] * len(days) for i, t in enumerate(snapshot)})
    with pd.ExcelWriter(ref / "sp500_constituents_daily.xlsx") as writer:
        pd.DataFrame(rows).to_excel(writer, sheet_name="2026", index=False)


def test_reader_reads_class_share_file_but_keeps_constituents_symbol(tmp_path):
    days = ["2026-09-14", "2026-09-15"]
    _references(tmp_path, days, ["BRK.B", "A"])
    _bars(tmp_path / "stocks", "BRK-B", days, close=500.0)
    with patch.multiple(config, STOCK_DIR=tmp_path / "stocks", REF_DIR=tmp_path / "reference"):
        eng = SelectionEngine()
        df = eng._get_ticker_data("BRK.B")
        assert df is not None and df["Close"].iloc[-1] == 500.0
        assert eng._get_ticker_data("A") is None            # still a missing file, not guessed
        # #52: strategy keeps the Constituents spelling and the IB adapter
        # handles the broker spelling later in the execution path.
        assert config.CLASS_SHARES_TRADABLE is True
        assert eng.get_constituents(pd.Timestamp(days[-1])) == ["BRK.B", "A"]


def test_audit_resolves_class_share_and_flags_official_closure(tmp_path):
    days = ["2026-06-18", "2026-06-19", "2026-06-22"]           # 06-19 = Juneteenth
    _references(tmp_path, days, ["BRK.B"])
    _bars(tmp_path / "stocks", "BRK-B", [days[0], days[2]])
    _bars(tmp_path / "etfs", "SPY", [days[0], days[2]])
    r = audit(tmp_path, days[0], days[-1])
    row = r["coverage"][0]
    assert (row["ticker"], row["file"], row["file_error"]) == ("BRK.B", "BRK-B.csv", None)
    assert row["missing_sessions"] == ["2026-06-19"]
    assert row["missing_on_calendar_without_spy"] == ["2026-06-19"]
    assert r["calendar_cross_check"]["calendar_sessions_officially_closed"] == ["2026-06-19"]
    assert r["calendar_cross_check"]["official_sessions_missing_from_calendar"] == []
    assert r["schema_version"] == 2
