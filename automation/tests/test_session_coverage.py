"""Exact membership and session semantics, using only synthetic local files."""
from pathlib import Path

import pandas as pd
import pytest

from data.session_coverage import audit, load_calendar, load_constituents


def references(root, dates, snapshots):
    ref = root / "reference"
    ref.mkdir(exist_ok=True)
    pd.DataFrame({"date_code": ["not a date"] * len(dates), "date": pd.to_datetime(dates)}).to_excel(
        ref / "us_trading_days.xlsx", index=False)
    with pd.ExcelWriter(ref / "sp500_constituents_daily.xlsx") as writer:
        pd.DataFrame({"ignored": [1]}).to_excel(writer, sheet_name="Summary", index=False)
        pd.DataFrame(snapshots).to_excel(writer, sheet_name="2026", index=False)


def bars(root, ticker, dates, category="stocks", **overrides):
    folder = root / category
    folder.mkdir(exist_ok=True)
    fields = {c: [10.] * len(dates) for c in ("Open", "High", "Low", "Close", "Adj Close", "Volume")}
    pd.DataFrame({"Date": dates, **fields, **overrides}).to_csv(folder / f"{ticker}.csv", index=False)


def test_exact_membership_not_universe_or_forward_fill(tmp_path):
    days = ["2026-09-14", "2026-09-15", "2026-09-16"]
    references(tmp_path, days, {"Date": days[:2], "T1": ["OLD.txt", "NEW"]})
    bars(tmp_path, "OLD", days[:1])
    bars(tmp_path, "NEW", days[1:2])
    bars(tmp_path, "SPY", days, "etfs")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    r = audit(tmp_path, days[0], days[-1])
    assert r["unknown_constituent_sessions"] == [days[-1]]
    assert r["totals"]["required_bars"] == 2
    assert r["totals"]["missing_bars"] == 0
    assert before == {p: p.read_bytes() for p in before}


def test_internal_gaps_bounds_invalid_and_missing_file(tmp_path):
    days = ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"]
    references(tmp_path, days, {"Date": days, "T1": ["A"] * 5, "T2": ["MISSING"] * 5})
    bars(tmp_path, "A", [days[1], days[3]], **{"Adj Close": [None, 10]})
    bars(tmp_path, "SPY", days, "etfs")
    r = audit(tmp_path, days[0], days[-1])
    a, missing = r["coverage"]
    assert a["before_first_observed"] == days[:1]
    assert a["internal_missing"] == days[2:3]
    assert a["after_last_observed"] == days[4:]
    assert a["invalid_sessions"] == days[1:2]
    assert missing["missing_bars"] == 5 and missing["file_error"]
    assert r["totals"]["required_bars"] == (r["totals"]["valid_bars"] +
        r["totals"]["missing_bars"] + r["totals"]["invalid_bars"])


def test_calendar_dispute_does_not_become_confirmed_stock_gap(tmp_path):
    days = ["2026-06-18", "2026-06-19", "2026-06-22"]
    references(tmp_path, days, {"Date": days, "T1": ["A"] * 3})
    bars(tmp_path, "A", [days[0]])
    bars(tmp_path, "SPY", [days[0], days[2]], "etfs")
    r = audit(tmp_path, days[0], days[-1])
    assert r["calendar_cross_check"]["calendar_sessions_without_spy"] == days[1:2]
    assert r["coverage"][0]["missing_on_calendar_without_spy"] == days[1:2]
    assert r["coverage"][0]["missing_on_spy_sessions"] == days[2:]


def test_duplicate_snapshot_date_is_ambiguous_not_union(tmp_path):
    day = "2026-09-14"
    references(tmp_path, [day], {"Date": [day, day], "T1": ["A", "B"]})
    bars(tmp_path, "SPY", [day], "etfs")
    r = audit(tmp_path, day, day)
    assert r["required_tickers"] == 0
    assert r["unknown_constituent_sessions"] == [day]
    assert any(i["kind"] == "duplicate_constituent_dates" for i in r["input_issues"])


def test_duplicate_bars_and_nan_do_not_count_as_valid(tmp_path):
    days = ["2026-09-14", "2026-09-15"]
    references(tmp_path, days, {"Date": days, "T1": ["A", "A"]})
    bars(tmp_path, "A", [days[0], days[0], days[1]], Close=[10, 10, float("inf")])
    bars(tmp_path, "SPY", days, "etfs")
    r = audit(tmp_path, days[0], days[-1])
    assert r["totals"]["invalid_bars"] == 2
    assert r["totals"]["valid_bars"] == 0


def test_unsafe_symbol_is_not_followed_as_path(tmp_path):
    day = "2026-09-14"
    references(tmp_path, [day], {"Date": [day], "T1": ["../secret"]})
    bars(tmp_path, "SPY", [day], "etfs")
    r = audit(tmp_path, day, day)
    assert r["required_tickers"] == 0
    assert r["unknown_constituent_sessions"] == [day]


def test_calendar_range_and_extra_spy_sessions_are_reported(tmp_path):
    days = ["2026-09-14", "2026-09-15"]
    references(tmp_path, days[:1], {"Date": days, "T1": ["A", "A"]})
    bars(tmp_path, "A", days)
    bars(tmp_path, "SPY", days, "etfs")
    r = audit(tmp_path, days[0], days[-1])
    assert r["calendar_cross_check"]["spy_sessions_outside_calendar"] == days[1:]
    assert r["constituent_sessions_outside_calendar"] == days[1:]
    assert any(i["kind"] == "calendar_range_uncovered" for i in r["input_issues"])


def test_invalid_range_rejected_before_reading(tmp_path):
    with pytest.raises(ValueError, match="start"):
        audit(tmp_path, "2026-09-15", "2026-09-14")


def test_calendar_duplicates_and_invalid_dates_are_findings(tmp_path):
    path = tmp_path / "calendar.xlsx"
    pd.DataFrame({"Date": ["2026-09-14", "bad", "2026-09-14"]}).to_excel(path, index=False)
    issues = []
    dates = load_calendar(path.read_bytes(), issues)
    assert len(dates) == 1
    assert {i["kind"] for i in issues} == {"invalid_dates", "duplicate_calendar_sessions"}
