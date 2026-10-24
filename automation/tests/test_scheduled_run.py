"""
排程執行殼（#206：scheduled_run.run_job / run_task）.

04:40、16:00、21:00、21:31 共用這一套：逐帳戶跑本體、一個帳戶失敗繼續跑下一個、
三帳戶合成一則 Telegram、`--account all` 才寫執行紀錄、21:31 附當日總結、有帳戶
失敗 exit 1。這裡只透過 run_job 測一次；各排程的測試只測自己的本體。

No IB, no network: 本體一律是假的 per_account。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import pytest

import run_status
import scheduled_run
from run_status import (DAILY_CHECK_JOB, DAILY_REPORT_JOB, MOO_JOB, REBALANCE_JOB, UPDATE_JOB)

ACCOUNTS = ["Taylor", "Rosa", "Yvonne"]
ACCOUNT_JOBS = (DAILY_REPORT_JOB, DAILY_CHECK_JOB, REBALANCE_JOB, MOO_JOB)


@pytest.fixture
def shell(fake_config, run_status_dir, monkeypatch):
    """假的 Telegram、log 設定只記呼叫；帳戶設定用 conftest 的 fake_config。"""
    import notify
    sent, logs = [], []
    monkeypatch.setattr(notify, "tg_send",
                        lambda msg, *, token, chat_id: sent.append((msg, token, chat_id)))
    monkeypatch.setattr(scheduled_run, "setup_logging", lambda key: logs.append(key))
    monkeypatch.setattr(scheduled_run, "quiet_ib_console", lambda: None)
    return sent, logs


def at(when: datetime, monkeypatch) -> None:
    """當日總結看的「現在」。"""
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return when
    monkeypatch.setattr(run_status, "datetime", Fixed)


def section(text="送出 1 筆 MOO", level="ok"):
    """假本體：每個帳戶加一段。"""
    def per_account(ctx, digest):
        from account_utils import telegram_dest
        digest.add(ctx.name, text, [], level, **telegram_dest(ctx.name))
    return per_account


def quiet(ctx, digest):
    pass


def records(status_dir):
    return {p.name.split("_2")[0]: json.loads(p.read_text(encoding="utf-8"))
            for p in status_dir.glob("*.json")}


# ── 逐帳戶、合成一則 ─────────────────────────────────────────────────

def test_each_account_runs_once_and_one_message_is_sent(shell):
    sent, logs = shell
    seen = []

    def per_account(ctx, digest):
        seen.append(ctx.name)
        section()(ctx, digest)

    assert scheduled_run.run_job(REBALANCE_JOB, per_account, argv=["--account", "all"]) == 0
    assert seen == ACCOUNTS
    assert logs == ["rebalance"]
    [(msg, token, chat_id)] = sent
    assert msg.startswith("<b>📤 調倉 ")
    assert "<b>Taylor、Rosa、Yvonne</b> 送出 1 筆 MOO" in msg
    assert (token, chat_id) == ("FAKE_TOKEN", "FAKE_CHAT")


@pytest.mark.parametrize("job", ACCOUNT_JOBS, ids=lambda j: j.key)
def test_each_job_builds_ctx_with_its_role_and_titles_its_message(shell, monkeypatch, job):
    from account_ctx import AccountCtx
    sent, logs = shell
    roles = []
    real = AccountCtx.from_name.__func__

    def spy(cls, name, role):
        roles.append(role)
        return real(cls, name, role)

    monkeypatch.setattr(AccountCtx, "from_name", classmethod(spy))
    monkeypatch.setattr(run_status, "attach_daily_summary", lambda *a, **k: None)
    scheduled_run.run_job(job, section(), account="Taylor")
    assert roles == [job.role]
    assert logs == [job.key]
    assert sent[0][0].startswith(f"<b>{job.icon} {job.title} ")


def test_quiet_day_sends_nothing(shell):
    sent, _ = shell
    assert scheduled_run.run_job(DAILY_REPORT_JOB, quiet, argv=["--account", "all"]) == 0
    assert sent == []


def test_single_account_runs_only_that_account(shell):
    seen = []
    scheduled_run.run_job(DAILY_CHECK_JOB, lambda ctx, d: seen.append(ctx.name),
                          argv=["--account", "Rosa"])
    assert seen == ["Rosa"]


# ── 一個帳戶失敗：繼續跑下一個，exit 1 ───────────────────────────────

def test_failure_continues_to_next_account(shell, caplog):
    """#29：Taylor 的 Gateway 掛掉，Rosa / Yvonne 照常跑。"""
    sent, _ = shell
    ran = []

    def per_account(ctx, digest):
        ran.append(ctx.name)
        if ctx.name == "Taylor":
            raise ConnectionRefusedError("[WinError 1225] <refused>")
        section()(ctx, digest)

    with caplog.at_level(logging.ERROR):
        code = scheduled_run.run_job(REBALANCE_JOB, per_account, argv=["--account", "all"])
    assert code == 1
    assert ran == ACCOUNTS
    msg = sent[0][0]
    assert msg.startswith("<b>❌ 調倉 ")
    assert "<b>Taylor</b> ❌ 失敗\n[WinError 1225] &lt;refused&gt;" in msg
    assert "<b>Rosa、Yvonne</b> 送出 1 筆 MOO" in msg
    assert "未執行" not in msg
    failed = [r for r in caplog.records if "Account Taylor failed" in r.getMessage()]
    assert failed and failed[0].exc_info               # log 有完整錯誤堆疊
    assert "調倉 finished with 1 failure(s): Taylor=[WinError 1225] <refused>" in caplog.text


@pytest.mark.parametrize("job", ACCOUNT_JOBS, ids=lambda j: j.key)
def test_every_job_reports_failures_the_same_way(shell, monkeypatch, job):
    sent, _ = shell
    monkeypatch.setattr(run_status, "attach_daily_summary", lambda *a, **k: None)

    def down(ctx, digest):
        raise ConnectionRefusedError("refused")

    assert scheduled_run.run_job(job, down, argv=["--account", "all"]) == 1
    assert f"<b>Taylor、Rosa、Yvonne</b> {job.failed}\nrefused" in sent[0][0]


def test_section_added_by_the_body_is_not_repeated(shell):
    sent, _ = shell

    def per_account(ctx, digest):
        section("❌ 每日報告失敗", "error")(ctx, digest)
        raise RuntimeError("excel locked")

    assert scheduled_run.run_job(DAILY_REPORT_JOB, per_account, account="Taylor") == 1
    assert sent[0][0].count("Taylor") == 1
    assert "excel locked" not in sent[0][0]


def test_account_that_cannot_be_built_is_reported(shell):
    """ctx 建不起來（帳戶名打錯、accounts.json 壞掉）→ 補 ❌ 段落。"""
    sent, _ = shell
    assert scheduled_run.run_job(MOO_JOB, quiet, account="Nobody") == 1
    assert "<b>Nobody</b> ❌ 成交回報失敗" in sent[0][0]


def test_ctrl_c_aborts_and_marks_the_rest_not_run(shell):
    sent, _ = shell
    ran = []

    def per_account(ctx, digest):
        ran.append(ctx.name)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        scheduled_run.run_job(DAILY_CHECK_JOB, per_account, argv=["--account", "all"])
    assert ran == ["Taylor"]
    assert "<b>Rosa、Yvonne</b> ⚠️ 未執行\n手動中止，本輪未執行" in sent[0][0]


# ── 執行紀錄（#38）───────────────────────────────────────────────────

def test_scheduled_run_records_each_account(shell, run_status_dir):
    def per_account(ctx, digest):
        if ctx.name == "Rosa":
            raise RuntimeError("down")
        section()(ctx, digest)

    scheduled_run.run_job(REBALANCE_JOB, per_account, argv=["--account", "all"])
    rec = records(run_status_dir)["rebalance"]
    assert rec["finished"] and rec["error"] is None
    assert rec["outcomes"] == {"Taylor": ["ok", "送出 1 筆 MOO"],
                               "Rosa": ["error", "❌ 失敗"],
                               "Yvonne": ["ok", "送出 1 筆 MOO"]}


def test_manual_single_account_run_writes_no_record(shell, run_status_dir):
    scheduled_run.run_job(REBALANCE_JOB, section(), argv=["--account", "Taylor"])
    assert records(run_status_dir) == {}


def test_crash_is_recorded_and_raised(shell, run_status_dir):
    def per_account(ctx, digest):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        scheduled_run.run_job(DAILY_REPORT_JOB, per_account, argv=["--account", "all"])
    assert records(run_status_dir)["daily"]["error"] == "KeyboardInterrupt"


# ── 21:31：不寫紀錄、自己附當日總結 ─────────────────────────────────

def test_moo_job_appends_the_daily_summary_and_writes_no_record(shell, run_status_dir,
                                                                monkeypatch):
    sent, _ = shell
    at(datetime(2026, 9, 15, 21, 31, 5), monkeypatch)
    seen = []

    def fake_summary(now, accounts, moo_outcomes):
        seen.append((now, list(accounts), dict(moo_outcomes)))
        return "✅ 9/15 排程都跑完：（總結）", "ok"

    monkeypatch.setattr(run_status, "daily_summary", fake_summary)
    scheduled_run.run_job(MOO_JOB, section("1/1"), argv=["--account", "all"])
    [(msg, _, _)] = sent
    assert msg.startswith("<b>✅ MOO 成交 ")
    assert msg.endswith("\n\n✅ 9/15 排程都跑完：（總結）")
    assert seen[0][1] == ACCOUNTS and seen[0][2]["Taylor"] == ("ok", "1/1")
    assert records(run_status_dir) == {}


def test_moo_quiet_day_sends_only_the_summary(shell, monkeypatch):
    sent, _ = shell
    at(datetime(2026, 9, 15, 21, 31, 5), monkeypatch)
    monkeypatch.setattr(run_status, "daily_summary",
                        lambda now, accounts, moo: ("✅ 9/15 排程都跑完：（總結）", "ok"))
    scheduled_run.run_job(MOO_JOB, quiet, argv=["--account", "all"])
    assert sent == [("✅ 9/15 排程都跑完：（總結）", "FAKE_TOKEN", "FAKE_CHAT")]


@pytest.mark.parametrize("account, now", [
    ("Taylor", datetime(2026, 9, 15, 21, 31, 5)),     # 手動跑單一帳戶
    ("all", datetime(2026, 9, 15, 10, 0)),            # 白天手動跑
])
def test_moo_summary_only_for_the_evening_scheduled_run(shell, monkeypatch, account, now):
    sent, _ = shell
    at(now, monkeypatch)
    monkeypatch.setattr(run_status, "daily_summary",
                        lambda *a: pytest.fail("不該附總結"))
    scheduled_run.run_job(MOO_JOB, quiet, account=account)
    assert sent == []


def test_other_jobs_never_append_the_summary(shell, monkeypatch):
    at(datetime(2026, 9, 15, 21, 31, 5), monkeypatch)
    monkeypatch.setattr(run_status, "daily_summary", lambda *a: pytest.fail("不該附總結"))
    for job in (DAILY_REPORT_JOB, DAILY_CHECK_JOB, REBALANCE_JOB):
        scheduled_run.run_job(job, quiet, argv=["--account", "all"])


# ── 04:30 資料更新：不分帳戶 ─────────────────────────────────────────

def test_run_task_records_the_outcome(shell, run_status_dir):
    _, logs = shell
    done = []
    assert scheduled_run.run_task(UPDATE_JOB, lambda: done.append(1),
                                  lambda: {"": ("warn", "⚠️ 部分失敗")}) == 0
    assert done == [1] and logs == ["update"]
    rec = records(run_status_dir)["update"]
    assert rec["outcomes"] == {"": ["warn", "⚠️ 部分失敗"]} and rec["error"] is None


def test_run_task_crash_is_recorded_and_raised(shell, run_status_dir):
    def body():
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        scheduled_run.run_task(UPDATE_JOB, body, lambda: {"": ("ok", None)})
    assert records(run_status_dir)["update"]["error"] == "RuntimeError: disk full"


# ── Job 表 ───────────────────────────────────────────────────────────

def test_every_account_job_has_what_the_shell_needs():
    for job in ACCOUNT_JOBS:
        assert job.title and job.icon and job.failed.startswith("❌") and job.role is not None
    assert [j.key for j in ACCOUNT_JOBS if not j.records] == ["moo_fill"]
    assert [j.key for j in ACCOUNT_JOBS if j.summary] == ["moo_fill"]
