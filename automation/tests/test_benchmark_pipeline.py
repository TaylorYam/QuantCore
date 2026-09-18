"""Run the real SP500 benchmark on a small synthetic offline dataset."""
import pandas as pd
import pytest

from data.benchmark_pipeline import benchmark, measure_once


@pytest.fixture
def dataset(tmp_path):
    days = pd.bdate_range("2024-01-01", periods=300)
    for folder in ("stocks", "etfs", "reference"):
        (tmp_path / folder).mkdir()
    prices = [100 + i * .1 for i in range(len(days))]
    df = pd.DataFrame({"Date": days, **{c: prices for c in
                      ("Open", "High", "Low", "Close", "Adj Close")}, "Volume": 1000})
    for name in ("stocks/AAA.csv", "etfs/SPY.csv", "etfs/SSO.csv"):
        df.to_csv(tmp_path / name, index=False)
    with pd.ExcelWriter(tmp_path / "reference/sp500_constituents_daily.xlsx") as writer:
        pd.DataFrame({"Date": days, "T1": "AAA"}).to_excel(writer, sheet_name="2024", index=False)
    return tmp_path, str(days[-15].date()), str(days[-1].date())


def test_real_runs_repeat_and_restore_config_without_writes(dataset):
    from strategies.sp500_momentum import config
    root, start, end = dataset
    original = (config.STOCK_DIR, config.ETF_DIR, config.REF_DIR)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    report = benchmark(root, start, end, repeats=2)
    assert report["outputs_repeatable"]
    assert report["runs"][0]["loaded_tickers"] == 1
    assert report["runs"][0]["backtest_sessions"] == 15
    assert report["runs"][0]["csv_calls_during_run"] == 0
    assert report["runs"][0]["csv_failures"] == []
    assert report["runs"][0]["metric_calls"] > 0
    assert (config.STOCK_DIR, config.ETF_DIR, config.REF_DIR) == original
    assert before == {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_restore_bindings_if_backtest_raises(dataset, monkeypatch):
    from strategies.sp500_momentum import config, utils
    from strategies.sp500_momentum.backtester import SP500Backtester
    original_path, original_loader = config.STOCK_DIR, utils.load_stock
    def broken(self):
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(SP500Backtester, "run", broken)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        measure_once(*dataset)
    assert config.STOCK_DIR == original_path
    assert utils.load_stock is original_loader


def test_changed_dataset_rejected(dataset, monkeypatch):
    import data.benchmark_pipeline as module
    root, start, end = dataset
    def changed(*args):
        (root / "stocks/AAA.csv").write_text("changed")
        return {}
    monkeypatch.setattr(module, "measure_once", changed)
    with pytest.raises(ValueError, match="dataset changed"):
        benchmark(root, start, end, repeats=1)
