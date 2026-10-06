"""Offline data checks and updater failures; only temporary CSVs and fake downloads."""
import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

from data.health import audit, inspect_csv
from data.update_outcomes import outcome_errors

UPDATE_DAY = "2026-09-17"


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


def test_vix_audit_uses_close_when_adjusted_close_is_blank(tmp_path):
    path = tmp_path / "etfs" / "VIX.csv"
    csv(path, ("2026-04-02", "2026-04-03"),
        **{"Open": [26.78, 27.0], "High": [27.89, 27.5],
           "Low": [23.87, 25.0], "Close": [23.87, 25.5],
           "Volume": [0, 0], "Adj Close": [None, None]})

    report = audit(tmp_path)
    record = next(item for item in report["files"] if item["file"] == "VIX.csv")

    assert record["issues"] == []
    assert record["adjusted_close_fallback"] == "Close"


@pytest.fixture
def updater(tmp_path, monkeypatch):
    # Avoid collision with automation/update_data.py and fake modules in notification tests.
    path = Path(__file__).resolve().parents[2] / "data" / "update_data.py"
    spec = importlib.util.spec_from_file_location("market_data_updater", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._real_download_ohlcv = module._download_ohlcv
    monkeypatch.setattr(module, "STOCK_DIR", tmp_path / "stocks")
    monkeypatch.setattr(module, "ETF_DIR", tmp_path / "etfs")
    monkeypatch.setattr(module, "ALL_ETFS", ["SPY"])
    monkeypatch.setattr(module.yf, "download", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(module, "_download_ohlcv", lambda *a, **k: None)
    return module


def test_partial_batch_failure_and_corrupt_csv_are_returned(updater, monkeypatch):
    csv(updater.STOCK_DIR / "A.csv")
    (updater.STOCK_DIR / "B.csv").write_text("")
    def fail(*args, **kwargs):
        raise RuntimeError("offline failure")
    monkeypatch.setattr(updater.yf, "download", fail)
    results = {result["ticker"]: result for result in updater.update_stocks(today=UPDATE_DAY)}
    assert results["A"]["status"] == "download_failed"
    assert results["B"]["status"] == "local_data_error"
    assert len(outcome_errors(results.values())) == 2


def test_empty_response_does_not_claim_inactive_ticker_failure(updater):
    csv(updater.STOCK_DIR / "OLD.csv", ("2001-01-02",))
    result = updater.update_stocks(today=UPDATE_DAY)[0]
    assert result["ticker"] == "OLD"
    assert result["status"] == "inconclusive"
    assert outcome_errors([result]) == []


def test_current_ticker_is_reported_without_claiming_an_update(updater, monkeypatch):
    csv(updater.STOCK_DIR / "AAPL.csv", ("2026-09-16",))
    monkeypatch.setattr(updater.yf, "download",
                        lambda *a, **k: pytest.fail("current ticker should not download"))

    result = updater.update_stocks(today=UPDATE_DAY)[0]

    assert result == {
        "ticker": "AAPL", "dataset": "stocks", "status": "no_update_required",
        "detail": "local data already reaches 2026-09-16", "rows_added": 0,
    }
    assert json.loads(json.dumps(result)) == result
    assert outcome_errors([result]) == []


def test_malformed_nonempty_response_is_not_success(updater, monkeypatch):
    csv(updater.STOCK_DIR / "A.csv")
    monkeypatch.setattr(updater.yf, "download", lambda *a, **k:
                        pd.DataFrame({"Open": [1]}, index=pd.to_datetime(["2026-09-16"])))
    result = updater.update_stocks(today=UPDATE_DAY)[0]
    assert result["status"] == "invalid_response"


def test_successful_update_preserves_price_columns(updater, monkeypatch):
    path = updater.STOCK_DIR / "A.csv"
    csv(path)
    # the download re-reads the stored bar (#53 overlap check) plus the new one
    new = pd.DataFrame({k: [10, 11] for k in
                       ("Open", "High", "Low", "Close", "Adj Close", "Volume")},
                       index=pd.to_datetime(["2026-09-15", "2026-09-16"]))
    monkeypatch.setattr(updater.yf, "download", lambda *a, **k: new)
    result = updater.update_stocks(today=UPDATE_DAY)[0]
    assert result["status"] == "updated"
    assert result["rows_added"] == 1
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

    results = {result["ticker"]: result for result in updater.update_stocks(today=UPDATE_DAY)}
    assert results["DOW"]["status"] == results["SNDK"]["status"] == "updated"
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

    result = updater.update_stocks(today=UPDATE_DAY)[0]
    assert result["status"] == "identity_guard_hold"
    assert "SNDK: identity guard refuses provider append; skipped as reviewed" in (
        capsys.readouterr().out
    )
    assert path.read_bytes() == before


def test_vix_checked_even_when_other_etfs_current(updater, monkeypatch):
    csv(updater.ETF_DIR / "SPY.csv", ("2026-09-16",))
    csv(updater.ETF_DIR / "VIX.csv")
    calls = []
    def download(ticker, start, end, **kwargs):
        calls.append((ticker, start, end))
        return updater.Failure("download_failed", f"{ticker}: download failed: VIX offline")
    monkeypatch.setattr(updater, "_download_ohlcv", download)
    def unexpected(*args, **kwargs):
        raise AssertionError("ETF batch already current")
    monkeypatch.setattr(updater.yf, "download", unexpected)
    results = {result["ticker"]: result for result in updater.update_etfs(today=UPDATE_DAY)}
    assert results["VIX"]["status"] == "download_failed"
    assert outcome_errors(results.values()) == ["VIX: download failed: VIX offline"]
    assert calls[0][0] == "^VIX"


def test_vix_malformed_history_is_an_invalid_response(updater, monkeypatch):
    csv(updater.ETF_DIR / "SPY.csv", ("2026-09-16",))
    csv(updater.ETF_DIR / "VIX.csv")

    class MalformedTicker:
        def history(self, **kwargs):
            return pd.DataFrame({"Open": [25]}, index=pd.to_datetime(["2026-09-16"]))

    monkeypatch.setattr(updater, "_download_ohlcv", updater._real_download_ohlcv)
    monkeypatch.setattr(updater.yf, "Ticker", lambda ticker: MalformedTicker())
    results = {result["ticker"]: result for result in updater.update_etfs(today=UPDATE_DAY)}

    assert results["VIX"]["status"] == "invalid_response"
    assert len(outcome_errors(results.values())) == 1


def test_status_does_not_depend_on_the_wording_of_the_detail(updater, monkeypatch):
    """#189: a malformed response whose text happens to say "download failed"
    used to be re-classified from the message; the status is now returned."""
    csv(updater.ETF_DIR / "SPY.csv", ("2026-09-16",))
    csv(updater.ETF_DIR / "VIX.csv")

    class MisleadingTicker:
        def history(self, **kwargs):
            return pd.DataFrame({"Close": [25.0]}, index=["download failed"])

    monkeypatch.setattr(updater, "_download_ohlcv", updater._real_download_ohlcv)
    monkeypatch.setattr(updater.yf, "Ticker", lambda ticker: MisleadingTicker())
    results = {result["ticker"]: result for result in updater.update_etfs(today=UPDATE_DAY)}

    assert results["VIX"]["status"] == "invalid_response"


def test_update_date_defaults_to_the_call_date(updater, monkeypatch):
    from datetime import date, timedelta
    yesterday = str(date.today() - timedelta(days=1))
    csv(updater.STOCK_DIR / "AAPL.csv", (yesterday,))
    monkeypatch.setattr(updater.yf, "download",
                        lambda *a, **k: pytest.fail("current ticker should not download"))

    assert updater.update_stocks()[0]["status"] == "no_update_required"


def test_scheduler_propagates_returned_errors(monkeypatch):
    import sys
    import types
    import update_data
    fake = types.ModuleType("data.update_data")
    fake.update_etfs = lambda: [{"ticker": "SPY", "dataset": "etfs",
                                 "status": "local_data_error", "detail": "unreadable CSV",
                                 "rows_added": 0}]
    fake.update_stocks = lambda: [{"ticker": "A", "dataset": "stocks",
                                   "status": "download_failed", "detail": "download failed",
                                   "rows_added": 0}]
    monkeypatch.setitem(sys.modules, "data.update_data", fake)
    monkeypatch.setattr(update_data, "telegram_dest", lambda: {"token": "fake", "chat_id": "fake"})
    sent = []
    monkeypatch.setattr(update_data, "_tg", lambda *args, **kwargs: sent.append(args))
    result = update_data.main()
    assert result == ["SPY: unreadable CSV", "A: download failed"]
    assert len(sent) == 1
    assert update_data.run_outcome(result)[""][0] == "warn"


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [("download_failed", 1), ("inconclusive", 0), ("identity_guard_hold", 0)],
)
def test_shared_cli_exit_only_for_confirmed_failures(monkeypatch, status, expected_exit):
    import sys
    import types
    import shared.update_data as shared_update

    fake = types.ModuleType("data.update_data")
    fake.update_etfs = lambda: []
    fake.update_stocks = lambda: [{
        "ticker": "A", "dataset": "stocks", "status": status,
        "detail": "mock provider outcome", "rows_added": 0,
    }]
    monkeypatch.setitem(sys.modules, "data.update_data", fake)
    monkeypatch.setattr(sys, "argv", ["shared.update_data", "--stocks"])

    assert shared_update.main() == expected_exit
