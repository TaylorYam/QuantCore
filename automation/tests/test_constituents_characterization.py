"""Characterization matrix uses isolated state and restores every byte."""
import pandas as pd

from data.characterize_constituents import capture


def test_capture_three_accounts_five_scenarios_and_restore_state(tmp_path):
    root = tmp_path / "data"
    for folder in ("stocks", "etfs", "reference"):
        (root / folder).mkdir(parents=True)
    days = pd.bdate_range("2019-01-02", "2025-04-11")
    prices = pd.Series(range(len(days)), dtype=float) * 0.05 + 100
    bars = pd.DataFrame({"Date": days, "Open": prices, "High": prices + 1,
                         "Low": prices - 1, "Close": prices,
                         "Adj Close": prices, "Volume": 1000})
    for ticker in ("AAPL", "MSFT", "NVDA"):
        bars.to_csv(root / "stocks" / f"{ticker}.csv", index=False)
    for ticker in ("SPY", "SSO"):
        bars.to_csv(root / "etfs" / f"{ticker}.csv", index=False)
    with pd.ExcelWriter(root / "reference" / "sp500_constituents_daily.xlsx") as writer:
        for year, group in pd.DataFrame({"Date": days, "T1": "AAPL", "T2": "MSFT",
                                         "T3": "NVDA"}).groupby(days.year):
            group.to_excel(writer, sheet_name=str(year), index=False)

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    original = b"keep these exact bytes\r\n"
    (state_dir / "sp500_state_Taylor.json").write_bytes(original)
    result = capture(root, state_dir)
    assert len(result["matrix"]) == 15
    assert len(result["constituents"]["daily_sha256"]) == len(days)
    assert (state_dir / "sp500_state_Taylor.json").read_bytes() == original
    assert sorted(p.name for p in state_dir.iterdir()) == ["sp500_state_Taylor.json"]
