"""
排程執行紀錄 + 當日排程總結（#38：沒事的日子零訊息，分不出是沒事還是壞掉）.

平日 21:31 成交回報最後附一行當日總結：沒事只有這一行；排程沒跑、沒跑完、
出錯都會列出來。這個檔案的每個測試各用一份乾淨的暫存紀錄目錄。

No IB, no network.
"""

from __future__ import annotations

import json
import logging
import os
import runpy
import sys
from datetime import date, datetime

import pytest

@pytest.fixture(autouse=True)
def _clean_status_dir(run_status_dir):
    """紀錄目錄每個測試各一份：「上線第一天」看的是目錄裡有沒有更早的紀錄。"""
    return run_status_dir


ACCOUNTS = ["Taylor", "Rosa", "Yvonne"]
DAY = date(2026, 9, 15)                       # 週二
AT_2131 = datetime(2026, 9, 15, 21, 31, 5)
QUIET = "✅ 9/15 排程都跑完：04:30 資料更新 · 04:40 每日報告 · 16:00 無動作 · 21:00 無需調倉 · 21:31 無 MOO 單"


def ok_all(names=ACCOUNTS):
    return {n: ("ok", None) for n in names}


def record(at: str, outcomes=None, *, finished: str | None = "auto", error=None):
    started = f"2026-09-15T{at}"
    return {
        "started": started,
        "finished": started if finished == "auto" else finished,
        "outcomes": {n: list(o) for n, o in (outcomes or {}).items()},
        "error": error,
    }


def quiet_records(**override):
    recs = {
        "update": record("04:30:01", {"": ("ok", None)}),
        "daily": record("04:40:01", ok_all()),
        "daily_check": record("16:00:01", ok_all()),
        "rebalance": record("21:00:01", ok_all()),
    }
    recs.update(override)
    return recs


def summary(records=None, moo=None, *, first_day=False, day=DAY, now=AT_2131):
    from run_status import build_summary
    return build_summary(day, ACCOUNTS, quiet_records() if records is None else records,
                         ok_all() if moo is None else moo, first_day=first_day, now=now)


# ── 總結內容 ─────────────────────────────────────────────────────────

def test_quiet_day_is_one_line():
    assert summary() == (QUIET, "ok")


def test_order_day_stays_short():
    """有單的日子細節在 21:00 / 21:31 訊息裡，總結只標「送單」「成交」。"""
    recs = quiet_records(rebalance=record("21:00:01", {
        "Taylor": ("ok", "送出 7 筆 MOO"), "Rosa": ("ok", "送出 6 筆 MOO"), "Yvonne": ("ok", None)}))
    moo = {"Taylor": ("ok", "7/7"), "Rosa": ("ok", "6/6"), "Yvonne": ("ok", None)}
    assert summary(recs, moo) == (
        "✅ 9/15 排程都跑完：04:30 資料更新 · 04:40 每日報告 · 16:00 無動作 · 21:00 送單 · 21:31 成交",
        "ok")


def test_all_accounts_failing_omits_names():
    """08-17 情境：16:00 三帳戶都連不上 IB，其他排程正常且沒單。"""
    recs = quiet_records(daily_check=record("16:00:01", {
        n: ("error", "❌ IB 連線失敗") for n in ACCOUNTS}))
    assert summary(recs) == (
        "❌ 9/15 排程有狀況：04:30 資料更新 · 04:40 每日報告 · 16:00 日度風控 ❌ IB 連線失敗"
        " · 21:00 無需調倉 · 21:31 無 MOO 單",
        "error")


def test_only_problem_accounts_are_listed():
    recs = quiet_records(rebalance=record("21:00:01", {
        "Taylor": ("error", "❌ 失敗"), "Rosa": ("warn", "沒有送單"), "Yvonne": ("ok", "送出 1 筆 MOO")}))
    line, level = summary(recs)
    assert "21:00 調倉 Taylor ❌ 失敗，Rosa ⚠️ 沒有送單 · " in line
    assert "Yvonne" not in line
    assert level == "error"


def test_job_name_is_not_repeated_in_the_reason():
    """08-03、08-10 04:40 三帳戶都連不上 IB、16:00 資料錯位 — 標題裡的排程名稱不重複。"""
    recs = quiet_records(
        daily=record("04:40:01", {n: ("error", "❌ 每日報告失敗") for n in ACCOUNTS}),
        daily_check=record("16:00:01", {n: ("warn", "⚠️ 日度風控中止") for n in ACCOUNTS}))
    line = summary(recs)[0]
    assert "04:40 每日報告 ❌ 失敗 · 16:00 日度風控 ⚠️ 中止 · " in line


def test_headline_icon_is_not_doubled():
    recs = quiet_records(rebalance=record("21:00:01", {
        "Taylor": ("error", "送出 3 筆 MOO，❌ 之後失敗"), "Rosa": ("ok", None), "Yvonne": ("ok", None)}))
    assert "21:00 調倉 Taylor 送出 3 筆 MOO，❌ 之後失敗 · " in summary(recs)[0]


def test_low_cushion_alert_level():
    recs = quiet_records(daily=record("04:40:01", {
        "Taylor": ("ok", None), "Rosa": ("alert", "Margin Cushion <b>18.5%</b>（低於 25%）"),
        "Yvonne": ("ok", None)}))
    line, level = summary(recs)
    assert line.startswith("🔴 9/15 排程有狀況：")
    assert "04:40 每日報告 Rosa 🔴 Margin Cushion <b>18.5%</b>（低於 25%） · " in line
    assert level == "alert"


def test_data_update_partial_failure():
    recs = quiet_records(update=record("04:30:01", {"": ("warn", "⚠️ 部分失敗")}))
    line, level = summary(recs)
    assert line.startswith("⚠️ 9/15 排程有狀況：04:30 資料更新 ⚠️ 部分失敗 · 04:40 每日報告")
    assert level == "warn"


def test_missing_record_means_the_job_did_not_run():
    recs = quiet_records(daily_check=None)
    line, level = summary(recs)
    assert "16:00 日度風控 ❌ 沒有執行紀錄" in line
    assert level == "error"


def test_started_but_not_finished():
    recs = quiet_records(rebalance=record("21:00:01", finished=None))
    assert "21:00 調倉 ❌ 沒跑完" in summary(recs)[0]


def test_manual_run_before_schedule_does_not_count():
    """白天手動跑了一次、16:00 排程卻沒跑 → 仍是沒有執行紀錄。"""
    recs = quiet_records(daily_check=record("10:12:00", ok_all()))
    assert "16:00 日度風控 ❌ 沒有執行紀錄" in summary(recs)[0]


def test_late_start_still_counts():
    """電腦睡著、排程晚跑也算有跑（晚了多少看 log）。"""
    recs = quiet_records(daily_check=record("17:20:00", ok_all()))
    assert summary(recs) == (QUIET, "ok")


def test_crash_is_reported_with_partial_results():
    recs = quiet_records(rebalance=record("21:00:01", {
        "Taylor": ("ok", None), "Rosa": ("warn", "⚠️ 未執行"), "Yvonne": ("warn", "⚠️ 未執行")},
        error="KeyboardInterrupt"))
    assert "21:00 調倉 Rosa、Yvonne ⚠️ 未執行，❌ 程式錯誤：KeyboardInterrupt · " in summary(recs)[0]


def test_dynamic_text_is_escaped():
    recs = quiet_records(rebalance=record("21:00:01", {"A<B": ("error", "❌ 失敗")},
                                          error="ValueError: <bad>"))
    line = summary(recs)[0]
    assert "A&lt;B ❌ 失敗" in line
    assert "❌ 程式錯誤：ValueError: &lt;bad&gt;" in line


def test_moo_problems_are_listed():
    moo = {"Taylor": ("warn", "1/2"), "Rosa": ("ok", None), "Yvonne": ("error", "❌ IB 連線失敗")}
    line, level = summary(moo=moo)
    assert line.endswith("21:31 成交回報 Taylor ⚠️ 1/2，Yvonne ❌ IB 連線失敗")
    assert level == "error"


def test_first_day_marks_jobs_before_launch():
    """上線當天：上線前就跑完的排程沒有紀錄，標「上線前」而不是 ❌。"""
    recs = quiet_records(update=None, daily=None)
    assert summary(recs, first_day=True) == (
        "✅ 9/15 排程總結：04:30 資料更新 上線前 · 04:40 每日報告 上線前 · 16:00 無動作"
        " · 21:00 無需調倉 · 21:31 無 MOO 單",
        "ok")
    assert "❌ 沒有執行紀錄" in summary(recs, first_day=False)[0]


def test_first_day_without_any_record_yet():
    line, _ = summary({k: None for k in ("update", "daily", "daily_check", "rebalance")},
                      first_day=True)
    assert line == ("✅ 9/15 排程總結：04:30 資料更新 上線前 · 04:40 每日報告 上線前"
                    " · 16:00 日度風控 上線前 · 21:00 調倉 上線前 · 21:31 無 MOO 單")


def test_summary_due_only_on_weekday_evenings():
    from run_status import summary_due
    assert summary_due(datetime(2026, 9, 15, 21, 31))
    assert summary_due(datetime(2026, 9, 18, 23, 50))           # 週五晚跑
    assert not summary_due(datetime(2026, 9, 15, 20, 59))       # 白天手動跑
    assert not summary_due(datetime(2026, 9, 19, 21, 31))       # 週六


def test_summary_jobs_match_the_task_schedule():
    """總結涵蓋的排程 = install_all_tasks 的平日排程（成分股更新除外）。"""
    from install_all_tasks import WEEKDAYS, task_specs
    import run_status
    tracked = sorted((j.at, j.script) for j in (*run_status.SUMMARY_JOBS, run_status.MOO_JOB))
    for day in WEEKDAYS:
        scheduled = sorted({(s.at, os.path.basename(s.script)) for s in task_specs()
                            if (s.days is None or day in s.days)
                            and os.path.basename(s.script) != "refresh_constituents.py"})
        assert scheduled == tracked, day


# ── 紀錄檔 ────────────────────────────────────────────────────────────

def clock(*times):
    it = iter(datetime.fromisoformat(t) for t in times)
    return lambda: next(it)


def read_record(status_dir, name):
    return json.loads((status_dir / name).read_text(encoding="utf-8"))


def test_recording_writes_start_then_outcomes(run_status_dir):
    import run_status
    names = []
    with run_status.recording("rebalance", lambda: {n: ("ok", None) for n in names},
                              clock=clock("2026-09-15T21:00:01", "2026-09-15T21:03:15")):
        mid = read_record(run_status_dir, "rebalance_20260915.json")
        names.extend(ACCOUNTS)
    assert mid["started"] == "2026-09-15T21:00:01" and mid["finished"] is None
    done = read_record(run_status_dir, "rebalance_20260915.json")
    assert done == {"job": "rebalance", "started": "2026-09-15T21:00:01",
                    "finished": "2026-09-15T21:03:15",
                    "outcomes": {n: ["ok", None] for n in ACCOUNTS}, "error": None}


def test_recording_keeps_error_and_reraises(run_status_dir):
    import run_status
    with pytest.raises(RuntimeError):
        with run_status.recording("daily", lambda: {"Taylor": ("error", "❌ 每日報告失敗")},
                                  clock=clock("2026-09-15T04:40:01", "2026-09-15T04:40:03")):
            raise RuntimeError("xlsx locked")
    rec = read_record(run_status_dir, "daily_20260915.json")
    assert rec["error"] == "RuntimeError: xlsx locked"
    assert rec["outcomes"] == {"Taylor": ["error", "❌ 每日報告失敗"]}
    assert rec["finished"] == "2026-09-15T04:40:03"


def test_recording_exit_codes(run_status_dir):
    import run_status
    for code, expected in ((0, None), (1, "SystemExit: 1")):
        with pytest.raises(SystemExit):
            with run_status.recording("update", lambda: {"": ("ok", None)}):
                sys.exit(code)
        [path] = run_status_dir.glob("update_*.json")
        assert json.loads(path.read_text(encoding="utf-8"))["error"] == expected
    with pytest.raises(KeyboardInterrupt):
        with run_status.recording("update", lambda: {}):
            raise KeyboardInterrupt
    assert json.loads(path.read_text(encoding="utf-8"))["error"] == "KeyboardInterrupt"


def test_recording_survives_broken_outcomes(run_status_dir):
    import run_status

    def broken():
        raise KeyError("names")

    with run_status.recording("daily_check", broken):
        pass
    [path] = run_status_dir.glob("daily_check_*.json")
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["outcomes"] == {} and rec["error"].startswith("結果整理失敗")


def test_recording_disabled_writes_nothing(run_status_dir):
    """手動只跑單一帳戶：不寫紀錄，不會蓋掉三帳戶的結果。"""
    import run_status
    with run_status.recording("rebalance", lambda: ok_all(), enabled=False):
        pass
    assert not run_status_dir.exists()


def test_write_failure_never_breaks_the_job(tmp_path, monkeypatch, caplog):
    import run_status
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    monkeypatch.setattr(run_status, "STATUS_DIR", blocker / "run_status")
    ran = []
    with caplog.at_level(logging.WARNING):
        with run_status.recording("update", lambda: {"": ("ok", None)}):
            ran.append(True)
    assert ran == [True]
    assert "run status write failed" in caplog.text


def write_day(day: date, **override):
    """用 recording 寫一整天：每個排程準時跑完、各帳戶沒事（override 換結果）。"""
    import run_status
    for job in run_status.SUMMARY_JOBS:
        outcomes = override.get(job.key, {"": ("ok", None)} if job.key == "update" else ok_all())
        t = f"{day.isoformat()}T{job.at}:01"
        with run_status.recording(job.key, lambda o=outcomes: o, clock=clock(t, t)):
            pass


def test_daily_summary_reads_the_day_records():
    from run_status import daily_summary
    write_day(date(2026, 9, 14))
    write_day(DAY)
    assert daily_summary(AT_2131, ACCOUNTS, ok_all()) == (QUIET, "ok")


def test_daily_summary_first_day_detection():
    from run_status import daily_summary
    write_day(DAY)                               # 沒有前一天的紀錄 = 上線第一天
    assert daily_summary(AT_2131, ACCOUNTS, ok_all())[0] == QUIET
    import run_status
    (run_status.STATUS_DIR / "daily_check_20260915.json").unlink()
    line, level = daily_summary(AT_2131, ACCOUNTS, ok_all())
    # 上線當天 04:30 就有紀錄：只有更早的排程能算「上線前」，16:00 沒紀錄就是沒跑
    assert "16:00 日度風控 ❌ 沒有執行紀錄" in line and level == "error"


def test_attach_daily_summary_footer_only_on_quiet_day(fake_config):
    from notify import Digest
    from run_status import attach_daily_summary
    write_day(date(2026, 9, 14))
    write_day(DAY)
    d = Digest("MOO 成交 2026-09-15", icon="✅")
    attach_daily_summary(d, ACCOUNTS, now=AT_2131)
    assert d.render() == QUIET
    assert d.destinations() == [("FAKE_TOKEN", "FAKE_CHAT")]


def test_attach_daily_summary_skips_when_not_due(fake_config):
    from notify import Digest
    from run_status import attach_daily_summary
    d = Digest("MOO 成交")
    attach_daily_summary(d, ACCOUNTS, now=datetime(2026, 9, 15, 14, 0))
    attach_daily_summary(d, ACCOUNTS, now=datetime(2026, 9, 19, 21, 31))
    assert d.render() is None


def test_attach_daily_summary_never_raises(fake_config, monkeypatch, caplog):
    import run_status
    from notify import Digest

    def boom(*a, **k):
        raise ValueError("bad record")

    monkeypatch.setattr(run_status, "daily_summary", boom)
    d = Digest("MOO 成交")
    with caplog.at_level(logging.WARNING):
        run_status.attach_daily_summary(d, ACCOUNTS, now=AT_2131)
    assert d.render() is None
    assert "daily summary failed: bad record" in caplog.text


# ── 排程入口真的有寫紀錄（runpy 跑 __main__；不連 IB、不發 Telegram）──────

AUTOMATION = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def entry_point(fake_config, monkeypatch, tmp_path):
    """跑腳本的 __main__：log 寫暫存目錄、帳戶 ctx 一律建不起來（絕不碰 IB）。"""
    pytest.importorskip("ib_insync")
    import types
    import ib_fetcher
    import ib_insync
    import log_setup
    import notify
    from account_ctx import AccountCtx

    def no_ib(*a, **k):
        raise AssertionError("測試不該連 IB")

    def no_ctx(cls, name, role):
        raise RuntimeError("accounts.json broken")

    monkeypatch.setattr(ib_insync.IB, "connect", no_ib)
    monkeypatch.setattr(ib_fetcher, "connect", no_ib)
    monkeypatch.setattr(AccountCtx, "from_name", classmethod(no_ctx))
    sent = []
    monkeypatch.setattr(notify, "tg_send", lambda msg, *, token, chat_id: sent.append(msg))
    monkeypatch.setattr(log_setup, "_LOG_DIR", tmp_path / "logs")
    fake_update = types.ModuleType("data.update_data")
    fake_update.update_etfs = lambda: None
    fake_update.update_stocks = lambda: None
    monkeypatch.setitem(sys.modules, "data.update_data", fake_update)

    saved_handlers, saved_level = logging.root.handlers[:], logging.root.level

    def run(script, *args):
        monkeypatch.setattr(sys, "argv", [script, *args])
        try:
            runpy.run_path(os.path.join(AUTOMATION, script), run_name="__main__")
        except SystemExit as e:
            return e.code
        return 0

    yield run, sent
    for h in logging.root.handlers:
        if h not in saved_handlers:
            h.close()
    logging.root.handlers[:] = saved_handlers
    logging.root.setLevel(saved_level)


@pytest.mark.parametrize("script, args, job, err_headline", [
    ("rebalance_ib.py", ("--mode", "skewls", "--account", "all"), "rebalance", "❌ 失敗"),
    ("sp500_daily_check.py", ("--account", "all"), "daily_check", "❌ 日度風控失敗"),
    ("daily_report.py", ("--account", "all"), "daily", "❌ 每日報告失敗"),
])
def test_account_entry_points_record_each_account(entry_point, run_status_dir,
                                                  script, args, job, err_headline):
    run, sent = entry_point
    run(script, *args)
    [path] = run_status_dir.glob(f"{job}_*.json")
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["finished"] and rec["error"] is None
    assert rec["outcomes"] == {n: ["error", err_headline] for n in ACCOUNTS}
    assert len(sent) == 1 and f"<b>Taylor、Rosa、Yvonne</b> {err_headline}" in sent[0]


def test_single_account_run_writes_no_record(entry_point, run_status_dir):
    run, _ = entry_point
    run("rebalance_ib.py", "--mode", "skewls", "--account", "Taylor")
    run("sp500_daily_check.py", "--account", "Taylor")
    assert not list(run_status_dir.glob("*.json"))


def test_update_entry_point_records(entry_point, run_status_dir):
    run, sent = entry_point
    assert run("update_data.py") == 0
    [path] = run_status_dir.glob("update_*.json")
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["outcomes"] == {"": ["ok", None]} and rec["error"] is None
    assert sent == []
