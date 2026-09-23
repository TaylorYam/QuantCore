"""Offline data checks and updater failures; only temporary CSVs and fake downloads."""
import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from data.health import audit, inspect_csv


def csv(path, dates=("2026-09-15",), **overrides):
    data = {"Date": dates, **{k: [10] * len(dates) for k in
            ("Open", "High", "Low", "Close", "Adj Close", "Volume")}}
    data.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(data).to_csv(path, index=False)


def test_audit_is_read_only_and_does_not_label_inactive_universe_stale(tmp_path):
    path = tmp_path / "stocks" / "OLD.csv"
    csv(path, ("2001-01-02",))
    csv(tmp_path / "etfs" / "SPY.csv")
    before = path.read_bytes()
    report = audit(tmp_path, "2026-09-15", ("NEW",))
    assert report["issues"] == ["required stock missing: NEW"]
    assert report["files"][0]["issues"] == []
    assert path.read_bytes() == before


def test_checks_duplicates_unsorted_invalid_and_required_freshness(tmp_path):
    path = tmp_path / "stocks" / "A.csv"
    csv(path, ("2026-09-15", "2026-09-14", "2026-09-14"),
        **{"Adj Close": [1, None, 1], "Volume": [0, -1, 1]})
    report = audit(tmp_path, "2026-09-16", ("A",))
    issues = report["files"][0]["issues"]
    assert {"duplicate_sessions": 1} in issues
    assert "unsorted_dates" in issues
    assert {"invalid_values": "Adj Close", "count": 1} in issues
    assert {"invalid_values": "Volume", "count": 1} in issues
    assert {"required_session_missing": "2026-09-16"} in issues


def test_unreadable_and_missing_columns(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("Date,Close\ninvalid,1\n")
    result = inspect_csv(path, adjusted=True)
    assert {"invalid_dates": 1} in result["issues"]
    assert "Adj Close" in result["issues"][0]["missing_columns"]
    assert inspect_csv(tmp_path / "missing.csv")["issues"]


def test_etf_audit_requires_adjusted_close(tmp_path):
    path = tmp_path / "etfs" / "SPY.csv"
    path.parent.mkdir(parents=True)
    pd.DataFrame({"Date": ["2026-09-15"], "Open": [1], "High": [1],
                  "Low": [1], "Close": [1], "Volume": [0]}).to_csv(path, index=False)

    report = audit(tmp_path)

    assert {"missing_columns": ["Adj Close"]} in report["files"][0]["issues"]


@pytest.fixture
def updater(tmp_path, monkeypatch):
    # Avoid collision with automation/update_data.py and fake modules in notification tests.
    path = Path(__file__).resolve().parents[2] / "data" / "update_data.py"
    spec = importlib.util.spec_from_file_location("market_data_updater", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STOCK_DIR", tmp_path / "stocks")
    monkeypatch.setattr(module, "ETF_DIR", tmp_path / "etfs")
    monkeypatch.setattr(module, "ALL_ETFS", ["SPY"])
    monkeypatch.setattr(module, "TODAY", "2026-09-17")
    monkeypatch.setattr(module.yf, "download", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(module, "_download_ohlcv", lambda *a, **k: None)
    return module


def test_partial_batch_failure_and_corrupt_csv_are_returned(updater, monkeypatch):
    csv(updater.STOCK_DIR / "A.csv")
    (updater.STOCK_DIR / "B.csv").write_text("")
    def fail(*args, **kwargs):
        raise RuntimeError("offline failure")
    monkeypatch.setattr(updater.yf, "download", fail)
    errors = updater.update_stocks()
    assert any("A: download failed" in e for e in errors)
    assert any("B: unreadable CSV" in e for e in errors)


def test_empty_response_does_not_claim_inactive_ticker_failure(updater):
    csv(updater.STOCK_DIR / "OLD.csv", ("2001-01-02",))
    assert updater.update_stocks() == []


def test_malformed_nonempty_response_is_not_success(updater, monkeypatch):
    csv(updater.STOCK_DIR / "A.csv")
    monkeypatch.setattr(updater.yf, "download", lambda *a, **k:
                        pd.DataFrame({"Open": [1]}, index=pd.to_datetime(["2026-09-16"])))
    assert "invalid download response" in updater.update_stocks()[0]


def test_successful_update_preserves_price_columns(updater, monkeypatch):
    path = updater.STOCK_DIR / "A.csv"
    csv(path)
    # the download re-reads the stored bar (#53 overlap check) plus the new one
    new = pd.DataFrame({k: [10, 11] for k in
                       ("Open", "High", "Low", "Close", "Adj Close", "Volume")},
                       index=pd.to_datetime(["2026-09-15", "2026-09-16"]))
    monkeypatch.setattr(updater.yf, "download", lambda *a, **k: new)
    assert updater.update_stocks() == []
    written = pd.read_csv(path)
    assert written["Date"].tolist() == ["2026-09-15", "2026-09-16"]
    assert written["Adj Close"].tolist() == [10, 11]


def test_current_dow_and_sndk_histories_resume_at_verified_provider_identity(
    updater, monkeypatch,
):
    for symbol in ("DOW", "SNDK"):
        csv(updater.STOCK_DIR / f"{symbol}.csv", ("2026-09-15",))

    index = pd.to_datetime(["2026-09-15", "2026-09-16"])
    daily = pd.DataFrame({
        "Open": [10, 11], "High": [10, 11], "Low": [10, 11],
        "Close": [10, 11], "Adj Close": [10, 11], "Volume": [100, 110],
        "Stock Splits": [0, 0],
    }, index=index)
    response = pd.concat({"DOW": daily, "SNDK": daily}, axis=1)
    calls = []
    monkeypatch.setattr(
        updater.yf, "download", lambda tickers, **kwargs: calls.append(tickers) or response,
    )

    assert updater.update_stocks() == []
    assert calls == ["DOW SNDK"]
    for symbol in ("DOW", "SNDK"):
        written = pd.read_csv(updater.STOCK_DIR / f"{symbol}.csv")
        assert written["Date"].tolist() == ["2026-09-15", "2026-09-16"]
        assert written["Close"].tolist() == [10, 11]


def test_legacy_sndk_history_still_fails_closed(updater, monkeypatch, capsys):
    path = updater.STOCK_DIR / "SNDK.csv"
    csv(path, ("2016-05-12", "2026-09-15"))
    before = path.read_bytes()

    def unexpected(*args, **kwargs):
        raise AssertionError("legacy SNDK history must remain identity-guarded")

    monkeypatch.setattr(updater.yf, "download", unexpected)

    assert updater.update_stocks() == []
    assert "SNDK: identity guard refuses provider append; skipped as reviewed" in (
        capsys.readouterr().out
    )
    assert path.read_bytes() == before


def test_vix_checked_even_when_other_etfs_current(updater, monkeypatch):
    csv(updater.ETF_DIR / "SPY.csv", ("2026-09-16",))
    csv(updater.ETF_DIR / "VIX.csv")
    calls = []
    def download(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("VIX offline")
    monkeypatch.setattr(updater, "_download_ohlcv", download)
    def unexpected(*args, **kwargs):
        raise AssertionError("ETF batch already current")
    monkeypatch.setattr(updater.yf, "download", unexpected)
    assert updater.update_etfs() == ["VIX: VIX offline"]
    assert calls[0][0] == "^VIX"


def test_scheduler_propagates_returned_errors(monkeypatch):
    import sys
    import types
    import update_data
    fake = types.ModuleType("data.update_data")
    fake.update_etfs = lambda: ["SPY: unreadable CSV"]
    fake.update_stocks = lambda: ["A: download failed"]
    monkeypatch.setitem(sys.modules, "data.update_data", fake)
    monkeypatch.setattr(update_data, "_load_tg_config", lambda: ("fake", "fake"))
    sent = []
    monkeypatch.setattr(update_data, "_tg", lambda *args, **kwargs: sent.append(args))
    result = update_data.main()
    assert result == ["SPY: unreadable CSV", "A: download failed"]
    assert len(sent) == 1
    assert update_data.run_outcome(result)[""][0] == "warn"
