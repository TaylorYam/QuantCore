"""Offline regression tests for price-file identity repair (#55)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

from data.identity_audit import (
    audit_data,
    check_provider_identity,
    identity_guard_reason,
    load_manifest,
    stage_repair,
)


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def _row(day: str, close: float, volume: float, *, open_: float | None = None,
         high: float | None = None, low: float | None = None) -> dict:
    open_ = close if open_ is None else open_
    high = close if high is None else high
    low = close if low is None else low
    return {
        "Date": day,
        "Open": open_,
        "High": high,
        "Low": low,
        "Close": close,
        "Adj Close": close,
        "Volume": volume,
    }


def _manifest(path: Path, rules: list[dict]) -> Path:
    for rule in rules:
        rule.setdefault("reason", "test fixture")
    path.write_text(json.dumps({"schema_version": 1, "rules": rules}),
                    encoding="utf-8")
    return path


def test_audit_reports_price_volume_provider_and_member_overlap(tmp_path):
    root = tmp_path / "data"
    _write_csv(root / "stocks" / "COL.csv", [
        _row("2015-01-02", 0.40, 300),
        _row("2018-11-30", 0.05, 100),
    ])
    manifest = _manifest(tmp_path / "manifest.json", [{
        "symbol": "COL",
        "action": "quarantine",
        "append": "refuse",
        "member_period": {"start": "2015-01-02", "end": "2018-11-30"},
        "price_volume_sanity": {
            "close_max": 1.0,
            "volume_max": 1000,
            "minimum_fraction": 1.0,
        },
        "provider_identity": {
            "name": "Rockwell Collins, Inc.",
            "first_trade": "2015-01-02",
        },
    }])

    report = audit_data(
        root,
        manifest,
        provider_identities={"COL": {
            "name": "Unrelated Security",
            "first_trade": "2014-01-02",
        }},
    )

    record = report["symbols"][0]
    assert "price_volume_identity_suspect" in record["issues"]
    assert "provider_name_mismatch" in record["issues"]
    assert record["member_overlap"]["overlap_rows"] == 2
    assert report["clean"] is False


def test_audit_detects_post_delisting_flat_zero_volume_bar(tmp_path):
    root = tmp_path / "data"
    _write_csv(root / "stocks" / "TWX.csv", [
        _row("2018-06-14", 98.77, 28205816, open_=98.41, high=99.65,
             low=98.39),
        _row("2018-06-15", 98.77, 0),
    ])
    manifest = _manifest(tmp_path / "manifest.json", [{
        "symbol": "TWX",
        "action": "truncate",
        "append": "refuse",
        "valid_through": "2018-06-14",
        "member_period": {"end": "2018-06-20"},
    }])

    record = audit_data(root, manifest)["symbols"][0]

    assert record["flat_zero_volume_dates"] == ["2018-06-15"]
    assert record["invalid_after_cutoff"] == ["2018-06-15"]
    assert "post_cutoff_rows" in record["issues"]


def test_provider_identity_requires_matching_name_and_first_trade():
    rule = {
        "provider_identity": {
            "name": "Example Corp",
            "first_trade": "2020-01-02",
        }
    }

    assert check_provider_identity(rule, {
        "name": "Example Corp",
        "first_trade": "2020-01-02",
    }) == []
    assert check_provider_identity(rule, {
        "name": "Other Corp",
        "first_trade": "2020-01-03",
    }) == ["provider_name_mismatch", "provider_first_trade_mismatch"]


def test_default_manifest_reconciles_reviewed_identity_findings():
    rules = load_manifest()["rules"]
    assert {rule["symbol"] for rule in rules} == {
        "COL", "CSRA", "NFX", "AVB", "TWX", "AET",
        "STI", "INFO", "CA", "DOW", "SE", "EMC", "TE", "CCE",
        "SNDK", "ADT", "CAM", "POM", "PCL", "NE", "TEG",
    }
    assert {rule["symbol"] for rule in rules
            if rule["action"] == "quarantine"} == {"COL"}
    assert {rule["symbol"] for rule in rules
            if rule["action"] == "truncate"} == {
                "CSRA", "NFX", "AVB", "TWX", "AET"
            }
    assert all(rule["append"] == "refuse" for rule in rules)
    assert {rule["symbol"] for rule in rules
            if rule.get("append_after_provider_first_trade")} == {"DOW", "SNDK"}


def test_issue82_csra_reuse_is_truncated_and_future_appends_are_refused(tmp_path):
    rule = next(rule for rule in load_manifest()["rules"]
                if rule["symbol"] == "CSRA")
    assert rule["action"] == "truncate"
    assert rule["valid_through"] == "2018-04-03"
    assert rule["provider_identity"] == {
        "name": "Cohen & Steers Real Assets Active ETF",
        "first_trade": "2026-08-12",
    }
    assert identity_guard_reason(
        "CSRA", local_first_date="2015-11-16"
    ) == "identity guard refuses provider append"

    source = tmp_path / "source"
    legacy = _row("2018-04-03", 41.23, 17803828, open_=41.26,
                  high=41.26, low=41.23)
    reused = _row("2026-08-12", 25.13, 0, open_=25.14, high=0, low=0)
    _write_csv(source / "stocks" / "CSRA.csv", [legacy, reused])
    before = (source / "stocks" / "CSRA.csv").read_bytes()
    manifest = _manifest(tmp_path / "manifest.json", [rule])

    output = tmp_path / "staging"
    report = stage_repair(source, output, manifest)

    staged = pd.read_csv(output / "stocks" / "CSRA.csv")
    assert staged["Date"].tolist() == ["2018-04-03"]
    assert report["actions"][0]["removed_rows"] == 1
    assert report["after"]["clean"] is True
    assert (source / "stocks" / "CSRA.csv").read_bytes() == before


@pytest.mark.parametrize(
    ("symbol", "first_trade"),
    [("DOW", "2019-03-20"), ("SNDK", "2025-02-13")],
)
def test_spin_off_symbol_append_requires_local_history_to_start_at_provider_identity(
    symbol, first_trade,
):
    # The original constituent histories remain unresolved. Only files whose
    # earliest local bar is inside the verified provider identity may continue.
    assert identity_guard_reason(symbol) == "identity guard refuses provider append"
    assert identity_guard_reason(
        symbol, local_first_date="2015-01-02"
    ) == "identity guard refuses provider append"
    assert identity_guard_reason(
        symbol, local_first_date=first_trade
    ) is None
    assert identity_guard_reason(
        symbol, local_first_date="2026-09-17"
    ) is None


def test_other_reused_symbol_append_remains_unconditionally_refused():
    assert identity_guard_reason("SE", local_first_date="2026-09-17") == (
        "identity guard refuses provider append"
    )


def test_stage_repair_quarantines_and_truncates_without_mutating_source(tmp_path):
    source = tmp_path / "source"
    _write_csv(source / "stocks" / "COL.csv", [
        _row("2015-01-02", 0.40, 300),
    ])
    _write_csv(source / "stocks" / "NFX.csv", [
        _row("2019-02-13", 17.07, 6651548),
        _row("2026-07-07", 24.60, 106),
    ])
    before = {path: path.read_bytes() for path in source.rglob("*") if path.is_file()}
    manifest = _manifest(tmp_path / "manifest.json", [
        {"symbol": "COL", "action": "quarantine", "append": "refuse"},
        {"symbol": "NFX", "action": "truncate", "append": "refuse",
         "valid_through": "2019-02-13"},
    ])

    output = tmp_path / "staging"
    report = stage_repair(source, output, manifest)

    assert {path: path.read_bytes() for path in source.rglob("*") if path.is_file()} == before
    assert not (output / "stocks" / "COL.csv").exists()
    assert (output / "quarantine" / "stocks" / "COL.csv").exists()
    staged_nfx = pd.read_csv(output / "stocks" / "NFX.csv")
    assert staged_nfx["Date"].tolist() == ["2019-02-13"]
    assert report["after"]["clean"] is True
    assert report["rollback"].startswith("Delete the staging directory")


@pytest.fixture
def updater(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "data" / "update_data.py"
    spec = importlib.util.spec_from_file_location("market_data_identity_updater", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STOCK_DIR", tmp_path / "stocks")
    monkeypatch.setattr(module, "TODAY", "2026-09-17")
    monkeypatch.setattr(module, "_download_ohlcv",
                        lambda *a, **k: pytest.fail("offline test reached the network (#53 refetch)"))
    return module


def test_updater_silently_refuses_guarded_append_and_updates_unlisted_symbol(
    updater, tmp_path, monkeypatch, capsys,
):
    _write_csv(updater.STOCK_DIR / "NFX.csv", [_row("2019-02-13", 17.07, 100)])
    _write_csv(updater.STOCK_DIR / "CSRA.csv", [
        _row("2018-04-03", 41.23, 17803828, open_=41.26,
             high=41.26, low=41.23),
    ])
    _write_csv(updater.STOCK_DIR / "A.csv", [_row("2026-09-15", 10, 100)])
    csra_rule = next(rule for rule in load_manifest()["rules"]
                     if rule["symbol"] == "CSRA")
    manifest = _manifest(tmp_path / "manifest.json", [
        {"symbol": "NFX", "action": "truncate", "append": "refuse",
         "valid_through": "2019-02-13"},
        csra_rule,
    ])
    monkeypatch.setattr(updater, "IDENTITY_MANIFEST_PATH", manifest)

    # the download re-reads the stored bar (#53 overlap check) plus the new one
    new = pd.DataFrame({key: [10, 11] for key in (
        "Open", "High", "Low", "Close", "Adj Close", "Volume",
    )}, index=pd.to_datetime(["2026-09-15", "2026-09-16"]))
    monkeypatch.setattr(updater.yf, "download", lambda *args, **kwargs: new)

    nfx_before = (updater.STOCK_DIR / "NFX.csv").read_bytes()
    csra_before = (updater.STOCK_DIR / "CSRA.csv").read_bytes()
    results = {result["ticker"]: result
               for result in updater.update_stocks(batch_size=1)}

    assert results["NFX"]["status"] == "identity_guard_hold"
    assert results["CSRA"]["status"] == "identity_guard_hold"
    assert results["A"]["status"] == "updated"
    assert (updater.STOCK_DIR / "NFX.csv").read_bytes() == nfx_before
    assert (updater.STOCK_DIR / "CSRA.csv").read_bytes() == csra_before
    output = capsys.readouterr().out
    assert "NFX: identity guard refuses provider append; skipped as reviewed" in output
    assert "CSRA: identity guard refuses provider append; skipped as reviewed" in output
    assert pd.read_csv(updater.STOCK_DIR / "A.csv")["Date"].tolist() == [
        "2026-09-15", "2026-09-16"
    ]
