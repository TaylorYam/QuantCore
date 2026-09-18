"""Offline tests for the explicit, date-bounded historical aliases (#56)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from data.coverage_evidence import classify, stage, verify
from data.historical_aliases import ALIASES, alias_for
from data.session_coverage import audit
from strategies.sp500_momentum import config, utils


def _bars(folder: Path, name: str, days: list[str], close: float = 10.0, *, adj=True):
    folder.mkdir(parents=True, exist_ok=True)
    values = {c: [close] * len(days) for c in ("Open", "High", "Low", "Close", "Volume")}
    if adj:
        values["Adj Close"] = [close] * len(days)
    pd.DataFrame({"Date": days, **values}).to_csv(folder / f"{name}.csv", index=False)


def _references(root: Path, days: list[str], ticker: str):
    ref = root / "reference"
    ref.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": pd.to_datetime(days)}).to_excel(ref / "us_trading_days.xlsx", index=False)
    with pd.ExcelWriter(ref / "sp500_constituents_daily.xlsx") as writer:
        pd.DataFrame({"Date": pd.to_datetime(days), "T1": [ticker] * len(days)}).to_excel(
            writer, sheet_name="2022", index=False)


def test_manifest_has_evidence_and_strict_date_bounds():
    dispositions = json.loads(Path("data/coverage_dispositions.json").read_text(encoding="utf-8"))
    assert {a.constituent_symbol for a in ALIASES} == {
        "GPS", "FBHS", "ADS", "DPS", "WYN", "ARNC", "DISCA", "ACE"
    }
    assert all(alias.evidence for alias in ALIASES)
    for alias in ALIASES:
        repair = dispositions["symbols"][alias.constituent_symbol]["repair"]
        assert repair == {"kind": "historical_alias", "provider_symbol": alias.provider_symbol,
                          "valid_from": alias.valid_from.isoformat(),
                          "valid_to": alias.valid_to.isoformat()}
    assert alias_for("GPS", "2022-02-02").provider_symbol == "GAP"
    assert alias_for("GPS", "2022-02-03") is None
    assert alias_for("DISCK", "2022-04-08") is None


def test_stock_reader_requires_a_session_before_using_an_alias():
    assert utils.stock_csv_path("stocks", "GPS").name == "GPS.csv"
    assert utils.stock_csv_path("stocks", "GPS", "2022-02-02").name == "GAP.csv"
    assert utils.stock_csv_path("stocks", "GPS", "2022-02-03").name == "GPS.csv"


def test_selection_reader_clips_successor_history_to_alias_window(tmp_path):
    days = ["2014-01-02", "2015-01-02", "2022-02-02", "2022-02-03"]
    _references(tmp_path, ["2022-02-02"], "GPS")
    _bars(tmp_path / "stocks", "GAP", days)
    with patch.multiple(config, STOCK_DIR=tmp_path / "stocks", REF_DIR=tmp_path / "reference"):
        from strategies.sp500_momentum.selection import SelectionEngine

        engine = SelectionEngine()
        inside = engine._get_ticker_data("GPS", pd.Timestamp("2022-02-02"))
        assert inside is not None
        assert inside.index.min() == pd.Timestamp("2015-01-02")
        assert inside.index.max() == pd.Timestamp("2022-02-02")
        assert engine._get_ticker_data("GPS", pd.Timestamp("2022-02-03")) is None


def test_audit_resolves_alias_per_session_and_does_not_extend_it(tmp_path):
    days = ["2022-02-02", "2022-02-03"]
    _references(tmp_path, days, "GPS")
    _bars(tmp_path / "stocks", "GAP", days)
    _bars(tmp_path / "etfs", "SPY", days, adj=False)

    report = audit(tmp_path, days[0], days[-1])
    row = report["coverage"][0]
    assert row["files"] == ["GAP.csv", "GPS.csv"]
    assert row["missing_sessions"] == [days[1]]
    assert row["resolved_sources"][0]["file"] == "GAP.csv"
    assert row["resolved_sources"][0]["sessions"] == [days[0]]
    assert row["resolved_sources"][0]["alias"]["provider_symbol"] == "GAP"
    assert row["file_error"] and "GPS.csv" in row["file_error"]


def test_alias_staging_and_verify_are_offline_and_reconciled(tmp_path):
    days = ["2022-02-01", "2022-02-02"]
    old = tmp_path / "old"
    _references(old, days, "GPS")
    _bars(old / "etfs", "SPY", days, adj=False)

    probe_dir = tmp_path / "probe"
    _bars(probe_dir / "prices", "GAP", days)
    content = (probe_dir / "prices" / "GAP.csv").read_bytes()
    probe = {
        "created_at_utc": "test", "yfinance_version": "test", "wikipedia": {},
        "local_files": {},
        "provider": {
            "GPS": {"provider_symbol": "GPS", "rows": 0, "metadata": {}},
            "GAP": {"provider_symbol": "GAP", "rows": 2, "first": days[0], "last": days[-1],
                    "csv": "prices/GAP.csv", "sha256": hashlib.sha256(content).hexdigest(),
                    "fetched_at_utc": "test", "metadata": {"longName": "The Gap, Inc."}},
        },
    }
    reviewed = {"reviewed": "test", "symbols": {
        "GPS": {"category": "rename_or_corporate_event", "status": "repaired",
                "repair": {"kind": "historical_alias", "provider_symbol": "GAP",
                           "valid_from": "2015-01-02", "valid_to": "2022-02-02"},
                "evidence": ["synthetic evidence"]}
    }}
    (probe_dir / "probe.json").write_text(json.dumps(probe), encoding="utf-8")
    old_coverage = audit(old, days[0], days[-1])
    plan = classify(old_coverage, probe, probe_dir, reviewed, wiki=({}, {}))
    assert plan["errors"] == []
    assert plan["missing_file_status"] == {"GPS": "repaired"}

    new = tmp_path / "new"
    result = stage(old, new, plan, probe_dir, "2022-12-31")
    assert result["diff"]["added"] == ["stocks/GAP.csv"]
    new_coverage = audit(new, days[0], days[-1])
    check = verify(old_coverage, new_coverage, plan)
    assert check["ok"], check["problems"]
    assert check["new_missing_bars"] == 0
