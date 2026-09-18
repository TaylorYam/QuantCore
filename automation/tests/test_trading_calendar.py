"""Official NYSE registry + reference calendar check/build (#50). Offline, synthetic files only."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

import market_calendar as mc
from data.trading_calendar import build_calendar, calendar_frame, check_calendar, read_calendar

ROOT = Path(__file__).resolve().parents[2]
# The six dates the local us_trading_days.xlsx listed as sessions (docs/data-session-study.md).
DISPUTED = ["2022-06-20", "2023-06-19", "2024-06-19", "2025-01-09", "2025-06-19", "2026-06-19"]


def _idx(days):
    return pd.DatetimeIndex(pd.to_datetime(list(days)))


@pytest.mark.parametrize("day", DISPUTED)
def test_disputed_dates_are_official_closures_with_a_source(day):
    reason, source = mc.NYSE_FULL_DAY_CLOSURES[date.fromisoformat(day)]
    assert ("Juneteenth" in reason) or ("Carter" in reason)
    assert mc.NYSE_SOURCES[source].startswith("https://ir.theice.com/")


@pytest.mark.parametrize("day", ["2014-07-03", "2019-12-24", "2024-07-03", "2025-11-28",
                                 "2025-12-24", "2026-11-27", "2026-12-24", "2027-11-26"])
def test_half_days_are_sessions(day):
    d = date.fromisoformat(day)
    assert d in mc.NYSE_EARLY_CLOSES
    assert d in mc.official_sessions(d, d)


def test_saturday_new_year_and_mourning_days():
    # Jan 1 2022 was a Saturday: no holiday observed, Dec 31 2021 traded.
    assert date(2021, 12, 31) in mc.official_sessions(date(2021, 12, 31), date(2021, 12, 31))
    assert mc.official_sessions(date(2018, 12, 5), date(2018, 12, 5)) == []
    assert mc.official_sessions(date(2025, 1, 9), date(2025, 1, 9)) == []


def test_session_counts_per_year_match_the_announcements():
    counts = {y: len(mc.official_sessions(date(y, 1, 1), date(y, 12, 31))) for y in range(2014, 2028)}
    assert counts == {2014: 252, 2015: 252, 2016: 252, 2017: 251, 2018: 251, 2019: 252, 2020: 253,
                      2021: 252, 2022: 251, 2023: 250, 2024: 252, 2025: 250, 2026: 251, 2027: 251}


def test_live_fallback_unchanged_for_2026_2027():
    previous = {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
                date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
                date(2026, 11, 26), date(2026, 12, 25), date(2027, 1, 1), date(2027, 1, 18),
                date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31), date(2027, 6, 18),
                date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24)}
    assert {d for d in mc._HOLIDAYS_FALLBACK if d.year >= 2026} == previous


def test_registry_refuses_to_guess_outside_its_years():
    with pytest.raises(ValueError, match="registry covers"):
        mc.official_sessions(date(2027, 12, 30), date(2028, 1, 3))


def test_check_reports_listed_closure_missing_session_and_spy_disagreement():
    june = mc.official_sessions(date(2026, 6, 15), date(2026, 6, 26))
    cal = _idx([*june, date(2026, 6, 19)])          # Juneteenth listed as a session
    cal = cal[cal != pd.Timestamp("2026-06-24")]     # a real session dropped
    spy = _idx(june)
    r = check_calendar(cal, spy)
    assert [x["date"] for x in r["listed_official_closures"]] == ["2026-06-19"]
    assert r["missing_official_sessions"] == ["2026-06-24"]
    assert r["spy_cross_check"]["calendar_without_spy"] == ["2026-06-19"]
    assert r["spy_cross_check"]["spy_without_calendar"] == ["2026-06-24"]
    assert r["ok"] is False
    assert check_calendar(_idx(june), spy)["ok"] is True


def test_build_removes_only_listed_closures_and_extends_from_the_registry():
    sessions = mc.official_sessions(date(2026, 6, 1), date(2026, 12, 31))
    template = _idx([*sessions, date(2026, 6, 19)])
    frame, summary = build_calendar(template, date(2027, 12, 31))
    built = {d.date() for d in frame["date"]}
    assert date(2026, 6, 19) not in built
    assert date(2026, 11, 27) in built and date(2026, 11, 26) not in built
    assert max(built) == date(2027, 12, 31) and date(2027, 12, 24) not in built
    assert [x["date"] for x in summary["removed_closures"]] == ["2026-06-19"]
    assert summary["appended_range"] == ["2027-01-04", "2027-12-31"]


def test_build_refuses_template_that_differs_beyond_closures():
    sessions = mc.official_sessions(date(2026, 6, 1), date(2026, 6, 30))
    template = _idx([d for d in sessions if d != date(2026, 6, 24)])
    with pytest.raises(ValueError, match="2026-06-24"):
        build_calendar(template)


def test_frame_keeps_existing_schema_and_round_trips(tmp_path):
    sessions = mc.official_sessions(date(2026, 6, 17), date(2026, 6, 23))
    path = tmp_path / "cal.xlsx"
    calendar_frame(sessions).to_excel(path, index=False)
    df = pd.read_excel(path)
    assert list(df.columns) == ["date_code", "weekday", "date"]
    assert df["date_code"].tolist() == ["date(20260617)", "date(20260618)", "date(20260622)", "date(20260623)"]
    assert df["weekday"].tolist() == ["Wednesday", "Thursday", "Monday", "Tuesday"]
    assert [d.date() for d in read_calendar(path)] == sessions


def test_cli_build_never_overwrites(tmp_path):
    template = tmp_path / "cal.xlsx"
    calendar_frame(mc.official_sessions(date(2026, 6, 1), date(2026, 6, 30))).to_excel(template, index=False)
    before = template.read_bytes()
    run = subprocess.run([sys.executable, "-m", "data.trading_calendar", "build", "--template", str(template),
                          "--out", str(template)], cwd=ROOT, capture_output=True, text=True)
    assert run.returncode == 2 and "refusing" in json.loads(run.stdout)["error"]
    assert template.read_bytes() == before
