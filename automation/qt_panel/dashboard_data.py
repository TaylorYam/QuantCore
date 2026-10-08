"""Dashboard 要顯示的資料（#148）：值班下次時間、今日排程、資料日期。

全部是讀取、不執行：值班的下次時間用 rosa_poll／ty_check／morning_check 的
`next_*_time` 算（和值班本身同一套排程計算）。今日排程讀 `run_status` 的執行
紀錄，規則同「當日排程總結」。
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Sequence

import run_status
from morning_check import next_morning_check_time
from rosa_poll import next_rosa_poll_time
from ty_check import next_ty_check_time

AUTOMATION_DIR = Path(__file__).resolve().parent.parent
DATA_DATE_CSV = AUTOMATION_DIR.parent / "data" / "etfs" / "SPY.csv"
LOG_DIR = AUTOMATION_DIR / "logs"

_WEEKDAY = "一二三四五六日"


# ── 值班 ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WatchItem:
    name: str
    schedule: str
    next_run: datetime


def watch_items(now_taipei: datetime, remote_account: str | None,
                ty_check_accounts: Sequence[str]) -> list[WatchItem]:
    """值班卡的監控與下次時間；沒有遠端 Gateway／TY check 帳戶就不列那一項（#211）。"""
    items = []
    if remote_account:
        items.append(WatchItem(f"{remote_account} poll", "台北 21:00–05:00 市場窗每 30 分",
                               next_rosa_poll_time(now_taipei)))
    if ty_check_accounts:
        items.append(WatchItem("TY check", f"週二・三 20:05 驗證 {'／'.join(ty_check_accounts)} 手動登入",
                               next_ty_check_time(now_taipei)))
    items.append(WatchItem("Morning check", "NYSE 交易日 07:00 健檢",
                           next_morning_check_time(now_taipei)))
    return items


def format_next_run(now: datetime, when: datetime) -> str:
    days = (when.date() - now.date()).days
    if days == 0:
        day = "今天"
    elif days == 1:
        day = "明天"
    else:
        day = f"{when:%m-%d}（{_WEEKDAY[when.weekday()]}）"
    return f"{day} {when:%H:%M}"


# ── 資料日期 ─────────────────────────────────────────────────────────

_TAIL_BYTES = 4096


def read_data_date(csv_path: Path = DATA_DATE_CSV) -> str:
    """ETF 資料最後一天（SPY.csv 最後一列的 Date）；讀不到回 —。
    只讀檔尾，不載入整個檔案。"""
    try:
        with open(csv_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(size - _TAIL_BYTES, 0))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return "—"
    for line in reversed(tail.splitlines()):
        first = line.split(",", 1)[0].strip()
        if first and first != "Date" and first[:1].isdigit():
            return first
    return "—"


# ── 今日排程 ─────────────────────────────────────────────────────────

# 過了排程時間這麼久還沒有紀錄，才算「沒有執行紀錄」（排程啟動要一點時間）
MISSING_AFTER = timedelta(minutes=5)

JOB_STATE_TEXT = {
    "pending": "未到時間",
    "missing": "沒有執行紀錄",
    "unfinished": "沒跑完",
    "ok": "完成",
    "problem": "有狀況",
    "ran": "已執行",
}

# 04:30 資料更新、04:40 每日報告每天跑；其餘只有平日（install_all_tasks.task_specs）
_EVERY_DAY = {"update", "daily"}


@dataclass(frozen=True)
class JobRow:
    key: str
    at: str
    name: str
    state: str
    detail: str = ""


def _scheduled(job: run_status.Job, day: date) -> datetime:
    hh, mm = (int(x) for x in job.at.split(":"))
    return datetime(day.year, day.month, day.day, hh, mm)


def _not_run_state(job: run_status.Job, now: datetime) -> str:
    return "pending" if now < _scheduled(job, now.date()) + MISSING_AFTER else "missing"


def _recorded_row(job: run_status.Job, now: datetime, record: dict | None,
                  accounts: Sequence[str]) -> JobRow:
    """record = 排程跑的紀錄（`run_status.scheduled_record`；手動跑的不算）。"""
    if record is None:
        return JobRow(job.key, job.at, job.name, _not_run_state(job, now))
    if not record.get("finished"):
        return JobRow(job.key, job.at, job.name, "unfinished")
    outcomes = {name: (o[0], o[1]) for name, o in (record.get("outcomes") or {}).items()}
    text, level = run_status.job_segment(job, outcomes, accounts, record.get("error"))
    # 總結片段是給 Telegram HTML 的：還原跳脫字元，拿掉畫面上已經有的時間與名稱
    detail = html.unescape(text).removeprefix(f"{job.at} ").removeprefix(job.name).strip()
    return JobRow(job.key, job.at, job.name, "ok" if level == "ok" else "problem", detail)


def _log_written_since(path: Path, since: datetime) -> bool:
    """log 檔在 since 之後寫過（白天手動跑留下的 log 不算排程跑過）。"""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime) >= since
    except OSError:
        return False


def today_jobs(now: datetime, accounts: Sequence[str], *,
               load: Callable[[str, date], dict | None] = run_status.load,
               log_dir: Path = LOG_DIR) -> list[JobRow]:
    """今天各排程跑了沒（當日排程總結的畫面版）。now 是本機時間（排程用本機時間）。

    21:31 成交回報不寫執行紀錄（結果直接附在它自己的 Telegram），這裡只看
    當天它的 log 檔在排程時間後有沒有寫過 → 「已執行」。
    """
    weekday = now.weekday() < 5
    rows = [_recorded_row(job, now, run_status.scheduled_record(job, now.date(), read=load),
                          accounts)
            for job in run_status.SUMMARY_JOBS if weekday or job.key in _EVERY_DAY]
    if weekday:
        job = run_status.MOO_JOB
        ran = _log_written_since(log_dir / f"{job.key}_{now:%Y%m%d}.log",
                                 _scheduled(job, now.date()) - run_status.EARLY_TOLERANCE)
        rows.append(JobRow(job.key, job.at, job.name,
                           "ran" if ran else _not_run_state(job, now)))
    return rows
