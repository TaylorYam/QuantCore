"""Panel Dashboard 的資料讀取（#148）：值班下次時間、今日排程、資料日期。

純函式，不需要 PySide6；全部用 tmp_path 與注入的讀取函式，不碰真實 logs／data。
"""
from __future__ import annotations

import os
from datetime import datetime

from qt_panel import dashboard_data as dd


# ── 值班 ─────────────────────────────────────────────────────────────

def test_watch_items_list_the_three_watches_with_their_next_run():
    now = datetime(2026, 9, 29, 20, 10)                          # 週二晚上
    items = dd.watch_items(now)
    assert [i.name for i in items] == ["Rosa poll", "TY check", "Morning check"]
    rosa, ty, morning = items
    assert rosa.next_run == datetime(2026, 9, 29, 21, 0)
    assert ty.next_run == datetime(2026, 9, 30, 20, 5)            # 今天 20:05 已過 → 週三
    assert morning.next_run == datetime(2026, 9, 30, 7, 0)
    assert all(i.schedule for i in items)


def test_format_next_run_says_today_tomorrow_or_date():
    now = datetime(2026, 9, 29, 20, 10)
    assert dd.format_next_run(now, datetime(2026, 9, 29, 21, 0)) == "今天 21:00"
    assert dd.format_next_run(now, datetime(2026, 9, 30, 7, 0)) == "明天 07:00"
    assert dd.format_next_run(now, datetime(2026, 10, 5, 7, 0)) == "10-05（一） 07:00"


# ── 資料日期 ─────────────────────────────────────────────────────────

def test_data_date_is_the_last_row_of_spy(tmp_path):
    csv = tmp_path / "SPY.csv"
    csv.write_text("Date,Open,Close\n2026-09-25,1,2\n2026-09-28,3,4\n\n", encoding="utf-8")
    assert dd.read_data_date(csv) == "2026-09-28"


def test_data_date_of_a_big_file_reads_only_the_tail(tmp_path):
    csv = tmp_path / "SPY.csv"
    rows = "".join(f"2015-01-{i % 28 + 1:02d},1.000000,2.000000\n" for i in range(5000))
    csv.write_text("Date,Open,Close\n" + rows + "2026-09-28,3,4\n", encoding="utf-8")
    assert dd.read_data_date(csv) == "2026-09-28"


def test_data_date_missing_or_empty_is_a_dash(tmp_path):
    assert dd.read_data_date(tmp_path / "nope.csv") == "—"
    empty = tmp_path / "SPY.csv"
    empty.write_text("Date,Open,Close\n", encoding="utf-8")
    assert dd.read_data_date(empty) == "—"


# ── 今日排程 ─────────────────────────────────────────────────────────

ACCOUNTS = ["Taylor", "Rosa", "Yvonne"]


def _record(started, finished="auto", outcomes=None, error=None):
    if finished == "auto":
        finished = started
    return {"started": started, "finished": finished,
            "outcomes": outcomes or {}, "error": error}


def _jobs(now, records, tmp_path, moo_log=None):
    if moo_log is not None:
        log = tmp_path / f"moo_fill_{now:%Y%m%d}.log"
        log.write_text("x", encoding="utf-8")
        os.utime(log, (moo_log.timestamp(), moo_log.timestamp()))
    return dd.today_jobs(now, ACCOUNTS, load=lambda key, day: records.get(key),
                         log_dir=tmp_path)


def test_weekday_lists_the_five_scheduled_jobs_in_order(tmp_path):
    rows = _jobs(datetime(2026, 9, 29, 3, 0), {}, tmp_path)
    assert [(r.at, r.name) for r in rows] == [
        ("04:30", "資料更新"), ("04:40", "每日報告"), ("16:00", "日度風控"),
        ("21:00", "調倉"), ("21:31", "成交回報")]
    assert {r.state for r in rows} == {"pending"}


def test_finished_quiet_jobs_are_ok_and_problems_show_the_reason(tmp_path):
    records = {
        "update": _record("2026-09-29T04:30:05"),
        "daily": _record("2026-09-29T04:40:02", outcomes={
            "Rosa": ["error", "每日報告失敗"], "Taylor": ["ok", None]}),
        "daily_check": _record("2026-09-29T16:00:01", outcomes={
            "Taylor": ["ok", "dip-buy 送單"]}),
    }
    rows = {r.key: r for r in _jobs(datetime(2026, 9, 29, 17, 0), records, tmp_path)}
    assert rows["update"].state == "ok"
    assert rows["daily"].state == "problem"
    assert "Rosa" in rows["daily"].detail and "失敗" in rows["daily"].detail
    assert rows["daily_check"].state == "ok"
    assert "dip-buy" in rows["daily_check"].detail
    assert rows["rebalance"].state == "pending"


def test_job_past_its_time_without_a_record_is_missing(tmp_path):
    rows = {r.key: r for r in _jobs(datetime(2026, 9, 29, 17, 0), {}, tmp_path)}
    assert rows["update"].state == "missing"
    assert rows["daily_check"].state == "missing"


def test_job_just_due_gets_a_few_minutes_before_it_counts_as_missing(tmp_path):
    rows = {r.key: r for r in _jobs(datetime(2026, 9, 29, 21, 2), {}, tmp_path)}
    assert rows["rebalance"].state == "pending"


def test_started_but_not_finished_is_unfinished(tmp_path):
    records = {"rebalance": _record("2026-09-29T21:00:03", finished=None)}
    rows = {r.key: r for r in _jobs(datetime(2026, 9, 29, 21, 10), records, tmp_path)}
    assert rows["rebalance"].state == "unfinished"


def test_program_error_counts_as_a_problem(tmp_path):
    records = {"update": _record("2026-09-29T04:30:05", error="RuntimeError: a < b & c")}
    rows = {r.key: r for r in _jobs(datetime(2026, 9, 29, 5, 0), records, tmp_path)}
    assert rows["update"].state == "problem"
    assert "a < b & c" in rows["update"].detail                 # 畫面是純文字，不是 Telegram HTML
    assert not rows["update"].detail.startswith("資料更新")        # 名稱已在旁邊那欄


def test_a_manual_run_before_the_scheduled_time_does_not_count(tmp_path):
    records = {"daily_check": _record("2026-09-29T11:00:00")}      # 白天手動跑
    rows = {r.key: r for r in _jobs(datetime(2026, 9, 29, 17, 0), records, tmp_path)}
    assert rows["daily_check"].state == "missing"


def test_fill_report_only_shows_whether_it_ran(tmp_path):
    now = datetime(2026, 9, 29, 22, 0)
    rows = {r.key: r for r in _jobs(now, {}, tmp_path, moo_log=datetime(2026, 9, 29, 21, 31, 40))}
    assert rows["moo_fill"].state == "ran"
    rows = {r.key: r for r in _jobs(now, {}, tmp_path / "x")}
    assert rows["moo_fill"].state == "missing"


def test_fill_report_log_from_a_daytime_manual_run_does_not_count(tmp_path):
    now = datetime(2026, 9, 29, 22, 0)
    rows = {r.key: r for r in _jobs(now, {}, tmp_path, moo_log=datetime(2026, 9, 29, 10, 0))}
    assert rows["moo_fill"].state == "missing"


def test_weekend_only_has_the_every_day_jobs(tmp_path):
    rows = _jobs(datetime(2026, 10, 3, 12, 0), {}, tmp_path)       # 週六
    assert [r.key for r in rows] == ["update", "daily"]


def test_every_state_has_display_text():
    assert set(dd.JOB_STATE_TEXT) >= {"pending", "missing", "unfinished", "ok", "problem", "ran"}
