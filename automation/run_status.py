"""
QuantCore — 排程執行紀錄 + 當日排程總結（#38）

#26 之後沒下單、沒出錯就不發 Telegram，「沒事」和「排程沒跑、程式當掉、
Telegram 發不出去」看起來一模一樣（07-20 ~ 09-15 平日有三成多整天零訊息）。

做法：
  - 排程入口開始時寫一份當日紀錄 `logs/run_status/<job>_<YYYYMMDD>.json`，
    結束時補上各帳戶結果（沿用 Digest 段落：沒有段落 = 跑完、沒事可報）
  - 平日 21:31 成交回報讀當天的紀錄組一行總結：沒事的日子只有這一行；有單或
    出錯時附在成交回報那則最後（`notify.Digest.footer`）
  - 沒有紀錄 = 沒執行；有開始沒結束 = 沒跑完（當掉、被砍或卡住）。平日 21:35
    還沒收到總結，就是電腦、排程或 Telegram 本身出問題

只有 `--account all`（排程用的形式）和資料更新會寫紀錄 — 手動只跑 Taylor 不會
蓋掉三帳戶的結果。開始時間早於排程時間的紀錄（例如白天手動跑一次）不算當天
排程的紀錄。
"""

from __future__ import annotations

import html
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

import json_state
from account_utils import ROLE_MOO_CHECK, ROLE_REBALANCE, ROLE_REPORT, telegram_dest
from notify import LEVEL_ICON, LEVEL_RANK

log = logging.getLogger(__name__)

STATUS_DIR = Path(__file__).parent / "logs" / "run_status"

Outcome = tuple[str, str | None]   # (level, headline)，同 Digest.outcomes()

# 平日成交回報要在這之後才附總結：排程 21:31；白天手動跑不發
SUMMARY_FROM = time(21, 0)
# 紀錄的開始時間最多可以比排程早這麼多（時鐘誤差）；再早就是手動跑的
EARLY_TOLERANCE = timedelta(minutes=5)

_HEAD_ICON = {"ok": "✅", **LEVEL_ICON}


@dataclass(frozen=True)
class Job:
    """一個排程的唯一定義：install_all_tasks 的時間與腳本、執行殼（scheduled_run）
    的 Telegram 標題與失敗段落、當日總結與 Dashboard 的顯示都讀這裡（#206）。"""
    key: str      # 紀錄檔名前綴（= 該腳本的 log 前綴）
    at: str       # 排程時間 HH:MM（台北）
    script: str   # 排程執行的腳本（install_all_tasks.task_specs 從這裡取）
    name: str     # 出狀況時顯示的名稱
    quiet: str    # 跑完、沒事時顯示
    active: str   # 跑完、有送單或成交時顯示
    # 逐帳戶的排程才有（資料更新不分帳戶）：
    title: str = ""            # Telegram 標題（後面接日期）
    icon: str = ""             # Telegram 標題沒有警示時的圖示
    role: int | None = None    # clientId 用途（AccountCtx.from_name）
    failed: str = ""           # 帳戶失敗時的段落標題
    records: bool = True       # 排程跑（--account all）時寫執行紀錄
    summary: bool = False      # 排程跑時在訊息最後附當日總結


UPDATE_JOB = Job("update", "04:30", "update_data.py", "資料更新", "資料更新", "資料更新")
DAILY_REPORT_JOB = Job("daily", "04:40", "daily_report.py", "每日報告", "每日報告", "每日報告",
                       title="每日報告", icon="📊", role=ROLE_REPORT,
                       failed="❌ 每日報告失敗")
DAILY_CHECK_JOB = Job("daily_check", "16:00", "sp500_daily_check.py", "日度風控", "無動作",
                      "dip-buy 送單", title="日度風控", icon="📤", role=ROLE_MOO_CHECK,
                      failed="❌ 日度風控失敗")
REBALANCE_JOB = Job("rebalance", "21:00", "rebalance_ib.py", "調倉", "無需調倉", "送單",
                    title="調倉", icon="📤", role=ROLE_REBALANCE, failed="❌ 失敗")
# 總結由 21:31 成交回報自己發：它的結果直接取當輪的 Digest，不寫紀錄
MOO_JOB = Job("moo_fill", "21:31", "moo_fill_check.py", "成交回報", "無 MOO 單", "成交",
              title="MOO 成交", icon="✅", role=ROLE_MOO_CHECK, failed="❌ 成交回報失敗",
              records=False, summary=True)

# 平日總結涵蓋的排程（時間順序），與 install_all_tasks.task_specs() 的平日排程
# 一致（測試把關）。週三 04:50 成分股更新有自己的 halt 警報，不列入。
SUMMARY_JOBS = (UPDATE_JOB, DAILY_REPORT_JOB, DAILY_CHECK_JOB, REBALANCE_JOB)


# ── 紀錄讀寫 ──────────────────────────────────────────────────────────

def _path(job: str, day: date) -> Path:
    return STATUS_DIR / f"{job}_{day:%Y%m%d}.json"


def _save(job: str, day: date, record: dict) -> None:
    """原子寫入（json_state）：程式被砍也不會留下半個 JSON。失敗只寫 log，
    不影響排程本身。"""
    try:
        path = _path(job, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        json_state.save_atomic(path, record, indent=1, ensure_ascii=False)
    except Exception as e:
        log.warning(f"run status write failed ({job}): {e}")


def load(job: str, day: date) -> dict | None:
    """當天的紀錄；沒有或讀不到回 None。"""
    path = _path(job, day)
    try:
        return json_state.load(path, None)
    except Exception as e:
        log.warning(f"run status unreadable ({path.name}): {e}")
        return None


@contextmanager
def recording(job: str, outcomes: Callable[[], Mapping[str, Outcome]], *,
              enabled: bool = True,
              clock: Callable[[], datetime] = datetime.now) -> Iterator[None]:
    """排程入口用：進入時寫開始時間，離開時寫各帳戶結果與結束時間。

    outcomes 在離開時才呼叫（例：`lambda: digest.outcomes(names)`）；例外離開
    也照寫，並記下錯誤。紀錄寫不進去只寫 log，不影響排程本身。
    """
    if not enabled:
        yield
        return
    started = clock()
    base = {"job": job, "started": started.isoformat(timespec="seconds")}
    _save(job, started.date(),
          {**base, "finished": None, "outcomes": {}, "error": None})
    error = None
    try:
        yield
    except BaseException as e:
        if not (isinstance(e, SystemExit) and not e.code):
            error = f"{type(e).__name__}: {e}".rstrip(": ")
        raise
    finally:
        try:
            got = {name: [level, headline]
                   for name, (level, headline) in outcomes().items()}
        except Exception as e:
            got = {}
            error = error or f"結果整理失敗: {e}"
        _save(job, started.date(),
              {**base, "finished": clock().isoformat(timespec="seconds"),
               "outcomes": got, "error": error})


# ── 當日總結 ──────────────────────────────────────────────────────────

def summary_due(now: datetime) -> bool:
    """平日 21:00 之後的成交回報才附當日總結。"""
    return now.weekday() < 5 and now.time() >= SUMMARY_FROM


def _scheduled(job: Job, day: date) -> datetime:
    hh, mm = (int(x) for x in job.at.split(":"))
    return datetime.combine(day, time(hh, mm))


def _started(record: dict | None) -> datetime | None:
    try:
        return datetime.fromisoformat(record["started"])
    except Exception:
        return None


def _scheduled_run(job: Job, day: date, record: dict | None) -> dict | None:
    """當天這個排程的紀錄；沒有、或開始得比排程早（手動跑的）→ None。"""
    started = _started(record)
    if started is None or started < _scheduled(job, day) - EARLY_TOLERANCE:
        return None
    return record


def scheduled_record(job: Job, day: date, *,
                     read: Callable[[str, date], dict | None] | None = None) -> dict | None:
    """當天這個排程「排程跑的」紀錄；沒有、或是排程前手動跑的 → None。
    read 預設讀紀錄檔（`load`）；Dashboard 測試給假的。"""
    return _scheduled_run(job, day, (read or load)(job.key, day))


def _has_earlier_records(day: date) -> bool:
    """day 之前有沒有任何紀錄 — 沒有代表今天是這個功能上線第一天。"""
    try:
        return any(p.stem.rsplit("_", 1)[-1] < f"{day:%Y%m%d}"
                   for p in STATUS_DIR.glob("*_????????.json"))
    except OSError:
        return False


def _label(names: Sequence[str], accounts: Sequence[str]) -> str:
    """原因前面的帳戶名；涵蓋全部帳戶或不分帳戶時省略。"""
    shown = [n for n in names if n]
    if not shown or (accounts and set(accounts) <= set(shown)):
        return ""
    return "、".join(html.escape(n) for n in shown) + " "


def _problem_text(job: Job, level: str, headline: str | None) -> str:
    """段落標題 → 總結用的原因：去掉重複的排程名稱（「每日報告 ❌ 每日報告失敗」
    → 「每日報告 ❌ 失敗」），標題本身沒有等級圖示時補上。"""
    icon = _HEAD_ICON[level]
    text = (headline or "").replace(job.name, "").strip()
    return text if icon in text else f"{icon} {text}".strip()


def job_segment(job: Job, outcomes: Mapping[str, Outcome],
                accounts: Sequence[str],
                error: str | None = None) -> tuple[str, str]:
    """單一排程的總結片段 → (文字, level)。只有出狀況的帳戶才列名字與原因。"""
    problems: dict[tuple[str, str], list[str]] = {}
    for name, (level, headline) in outcomes.items():
        if level != "ok":
            problems.setdefault((level, _problem_text(job, level, headline)), []).append(name)
    if error:
        text = f"❌ 程式錯誤：{html.escape(error[:120])}"
        problems.setdefault(("error", text), []).append("")
    if problems:
        worst = max((level for level, _ in problems), key=LEVEL_RANK.__getitem__)
        parts = [f"{_label(names, accounts)}{text}"
                 for (_, text), names in problems.items()]
        return f"{job.at} {job.name} " + "，".join(parts), worst
    if any(headline for _, headline in outcomes.values()):
        return f"{job.at} {job.active}", "ok"
    return f"{job.at} {job.quiet}", "ok"


def build_summary(day: date, accounts: Sequence[str],
                  records: Mapping[str, dict | None],
                  moo_outcomes: Mapping[str, Outcome], *,
                  first_day: bool, now: datetime) -> tuple[str, str]:
    """當日排程總結（一行）→ (文字, level)。紀錄由 caller 讀好傳入。

    first_day（這個功能上線第一天）：上線前就該跑完的排程不會有紀錄，
    標「上線前」，不當成 ❌。
    """
    runs = {job.key: _scheduled_run(job, day, records.get(job.key))
            for job in SUMMARY_JOBS}
    launch = min((_started(r) for r in runs.values() if r), default=now)

    segments: list[str] = []
    levels: list[str] = []
    before_launch = False
    for job in SUMMARY_JOBS:
        rec = runs[job.key]
        if rec is None and first_day and _scheduled(job, day) < launch:
            segments.append(f"{job.at} {job.name} 上線前")
            before_launch = True
            continue
        if rec is None:
            seg, level = f"{job.at} {job.name} ❌ 沒有執行紀錄", "error"
        elif not rec.get("finished"):
            seg, level = f"{job.at} {job.name} ❌ 沒跑完", "error"
        else:
            outcomes = {name: (o[0], o[1])
                        for name, o in (rec.get("outcomes") or {}).items()}
            seg, level = job_segment(job, outcomes, accounts, rec.get("error"))
        segments.append(seg)
        levels.append(level)

    seg, level = job_segment(MOO_JOB, moo_outcomes, accounts)
    segments.append(seg)
    levels.append(level)

    worst = max(levels, key=LEVEL_RANK.__getitem__)
    if worst != "ok":
        status = "排程有狀況"
    elif before_launch:
        status = "排程總結"
    else:
        status = "排程都跑完"
    head = f"{_HEAD_ICON[worst]} {day.month}/{day.day} {status}："
    return head + " · ".join(segments), worst


def daily_summary(now: datetime, accounts: Sequence[str],
                  moo_outcomes: Mapping[str, Outcome]) -> tuple[str, str]:
    """讀當天紀錄組出總結。"""
    day = now.date()
    records = {job.key: load(job.key, day) for job in SUMMARY_JOBS}
    return build_summary(day, accounts, records, moo_outcomes,
                         first_day=not _has_earlier_records(day), now=now)


def attach_daily_summary(digest, accounts: Sequence[str], *,
                         now: datetime | None = None) -> None:
    """21:31 成交回報發送前呼叫：平日 21:00 後把當日總結加到訊息最後。

    發到 config.json 的共用 Telegram（系統層級訊息）。在 finally 內呼叫，
    自身絕不拋例外。
    """
    now = now or datetime.now()
    if not summary_due(now):
        return
    try:
        line, level = daily_summary(now, accounts, digest.outcomes(accounts))
        digest.footer(line, level, **telegram_dest())
        log.info(f"Daily summary: {line}")
    except Exception as e:
        log.warning(f"daily summary failed: {e}")
