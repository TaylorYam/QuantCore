"""ETF raw-price and total-return field semantics."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from data.price_semantics import extract_batched_history, total_return_ohlc


def _write_etf(path: Path, *, close=(100.0, 101.0), adjusted=(95.0, 96.0)):
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame({
        "Date": pd.to_datetime(["2026-01-02", "2026-01-05"]),
        "Open": [99.0, 100.0], "High": [102.0, 103.0],
        "Low": [98.0, 99.0], "Close": close,
        "Adj Close": adjusted, "Volume": [1000, 1100],
    })
    df.to_csv(path, index=False)


def test_total_return_ohlc_scales_open_high_low_and_uses_adj_close():
    frame = pd.DataFrame({
        "Open": [99.0], "High": [102.0], "Low": [98.0],
        "Close": [100.0], "Adj Close": [95.0], "Volume": [1000],
    })

    adjusted = total_return_ohlc(frame)

    assert adjusted.loc[0, "Open"] == pytest.approx(94.05)
    assert adjusted.loc[0, "High"] == pytest.approx(96.9)
    assert adjusted.loc[0, "Low"] == pytest.approx(93.1)
    assert adjusted.loc[0, "Close"] == 95.0
    assert frame.loc[0, "Close"] == 100.0


def test_total_return_ohlc_keeps_entire_legacy_series_when_adjusted_coverage_is_partial():
    frame = pd.DataFrame({
        "Open": [99.0, 100.0], "High": [102.0, 103.0],
        "Low": [98.0, 99.0], "Close": [100.0, 101.0],
        "Adj Close": [95.0, np.nan],
    })

    adjusted = total_return_ohlc(frame)

    assert adjusted["Close"].tolist() == [100.0, 101.0]
    assert adjusted.loc[1, "Open"] == 100.0
    assert adjusted.loc[0, "Open"] == 99.0


def test_total_return_ohlc_rejects_nonpositive_adjusted_prices():
    frame = pd.DataFrame({"Open": [10.0], "Close": [10.0], "Adj Close": [0.0]})
    with pytest.raises(ValueError, match="invalid Adj Close/Close"):
        total_return_ohlc(frame)


def test_total_return_ohlc_rejects_malformed_adjusted_price_values():
    frame = pd.DataFrame({"Open": [10.0], "Close": [10.0], "Adj Close": ["bad"]})
    with pytest.raises(ValueError, match="invalid Adj Close value"):
        total_return_ohlc(frame)


@pytest.mark.parametrize("ticker_first", [True, False])
def test_batch_history_extraction_keeps_adj_close_for_both_multiindex_layouts(ticker_first):
    fields = ["Open", "Close", "Adj Close"]
    symbols = ["SPY", "QQQ"]
    if ticker_first:
        columns = pd.MultiIndex.from_product([symbols, fields])
        values = [[101.0, 100.0, 95.0, 202.0, 200.0, 190.0]]
    else:
        columns = pd.MultiIndex.from_product([fields, symbols])
        values = [[101.0, 202.0, 100.0, 200.0, 95.0, 190.0]]
    history = pd.DataFrame(values, index=pd.to_datetime(["2026-01-02"]), columns=columns)

    extracted = extract_batched_history(history, "SPY")

    assert extracted.loc[extracted.index[0], "Adj Close"] == 95.0
    assert extracted.loc[extracted.index[0], "Close"] == 100.0


def test_sector_rotation_loader_uses_adjusted_ohlc(tmp_path, monkeypatch):
    from strategies.sector_rotation import config, data_loader

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SECTOR_TICKERS", ["XLE"])
    monkeypatch.setattr(config, "BENCHMARK_TICKER", "SPY")
    _write_etf(tmp_path / "XLE.csv")
    _write_etf(tmp_path / "SPY.csv")

    data = data_loader.load_data()

    assert data["etf_close"].loc["2026-01-02", "XLE"] == 95.0
    assert data["etf_open"].loc["2026-01-02", "XLE"] == pytest.approx(94.05)
    assert data["spy_close"].loc["2026-01-02"] == 95.0


def test_skewls_loader_returns_adjusted_prices_and_raw_commission_marks(tmp_path, monkeypatch):
    from strategies.sector_skew_ls import config, data_loader

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SECTOR_TICKERS", ["XLE"])
    monkeypatch.setattr(config, "INVERSE_TICKERS", ["ERY"])
    monkeypatch.setattr(config, "BENCHMARK_TICKER", "SPY")
    monkeypatch.setattr(config, "VIX_LOCAL_NAME", "VIX")
    for ticker in ("XLE", "ERY", "SPY", "VIX"):
        _write_etf(tmp_path / f"{ticker}.csv")

    data = data_loader.load_data()

    assert data["sector_close"].loc["2026-01-02", "XLE"] == 95.0
    assert data["sector_open"].loc["2026-01-02", "XLE"] == pytest.approx(94.05)
    assert data["sector_raw_close"].loc["2026-01-02", "XLE"] == 100.0
    assert data["inv_close"].loc["2026-01-02", "ERY"] == 95.0
    assert data["inv_raw_close"].loc["2026-01-02", "ERY"] == 100.0
    assert data["spy_close"].loc["2026-01-02"] == 95.0


def test_skewls_per_trade_commission_uses_raw_prices(monkeypatch):
    from strategies.sector_skew_ls import backtest

    dates = pd.date_range("2026-01-02", periods=2)
    long_w = pd.DataFrame({"XLK": [0.5, 0.5]}, index=dates)
    short_w = pd.DataFrame({"XLC": [-0.5, -0.5]}, index=dates)
    monkeypatch.setattr(backtest, "compute_long_short_weights", lambda *a, **k: {
        "long_w": long_w, "short_w": short_w,
        "regime": pd.DataFrame(index=dates), "score": pd.DataFrame(index=dates),
    })
    sector_adjusted = pd.DataFrame({"XLK": [50.0, 50.0], "XLC": [50.0, 50.0]}, index=dates)
    raw_sector = pd.DataFrame({"XLK": [100.0, 100.0], "XLC": [100.0, 100.0]}, index=dates)
    data = {
        "sector_close": sector_adjusted,
        "sector_open": sector_adjusted.copy(),
        "sector_raw_close": raw_sector,
        "inv_close": pd.DataFrame({"REW": [50.0, 50.0]}, index=dates),
        "inv_open": pd.DataFrame({"REW": [50.0, 50.0]}, index=dates),
        "inv_raw_close": pd.DataFrame({"REW": [100.0, 100.0]}, index=dates),
        "vix_close": pd.Series([15.0, 15.0], index=dates),
    }

    result = backtest.run_backtest_proxy(
        data, commission_model="per_trade", account_capital=100_000,
        cost_per_share=0.0035, min_per_order=0.35,
    )

    assert result["tc_drag"].iloc[0] == pytest.approx(3.5 / 100_000)


def test_market_regime_uses_spy_adjusted_close():
    from strategies.sp500_momentum.market_regime import MarketRegime

    dates = pd.bdate_range("2025-01-02", periods=205)
    spy = pd.DataFrame({"Close": 100.0, "Adj Close": 90.0}, index=dates)
    sso = pd.DataFrame({"Close": 100.0, "Adj Close": 90.0}, index=dates)

    state = MarketRegime(spy, sso).get_state(dates[-1])

    assert state["SPY_Close"] == 90.0
    assert state["SPY_MA200"] == 90.0
    assert state["SPY_ATH"] == 90.0


def test_market_regime_keeps_legacy_close_when_adjusted_history_is_partial():
    from strategies.sp500_momentum.market_regime import MarketRegime

    dates = pd.bdate_range("2025-01-02", periods=205)
    spy = pd.DataFrame({"Close": 100.0, "Adj Close": [90.0] * 204 + [np.nan]}, index=dates)
    sso = pd.DataFrame({"Close": 100.0}, index=dates)

    state = MarketRegime(spy, sso).get_state(dates[-1])

    assert state["SPY_Close"] == 100.0
    assert state["SPY_MA200"] == 100.0
    assert state["SPY_ATH"] == 100.0
