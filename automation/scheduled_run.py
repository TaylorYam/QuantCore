"""
QuantCore — 排程執行殼（#206）

04:40 每日報告、16:00 日度風控、21:00 調倉、21:31 成交回報都是「逐帳戶跑同一段
本體、三帳戶合成一則 Telegram」。這裡是它們共用的殼；腳本只剩本體
`per_account(ctx, digest)` 與一行 `__main__`：

  - log 寫 `logs/<job.key>_<YYYYMMDD>.log`，壓掉 ib_insync 主控台雜訊
  - `--account all` = 排程跑（帳戶清單取 accounts.json）；指定帳戶 = 手動跑，
    不寫執行紀錄、不附當日總結
  - 一個帳戶失敗（ctx 建不起來、連不上 Gateway、本體出錯）→ log、補該帳戶的
    ❌ 段落（本體已經加過段落就不重複），繼續下一個帳戶；Ctrl+C 立即中止，還沒跑
    的帳戶標「⚠️ 未執行」
  - 跑完送一則；有帳戶失敗 → exit code 1
  - 執行紀錄、當日總結依 Job（`run_status`）：21:31 不寫紀錄、自己附總結

04:30 資料更新不分帳戶，只用 `run_task`（log ＋ 執行紀錄）。
"""

from __future__ import annotations

import argparse
import logging
from datetime import date
from typing import Callable, Mapping, Sequence

import account_utils
import run_status
from account_ctx import AccountCtx
from account_utils import telegram_dest
from log_setup import quiet_ib_console, setup_logging
from notify import Digest, error_line

log = logging.getLogger(__name__)

PerAccount = Callable[[AccountCtx, Digest], None]


def account_parser(description: str) -> argparse.ArgumentParser:
    """排程腳本共用的參數：`--account`（帳戶名稱或 all）。腳本可再加自己的參數。"""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--account", required=True,
                        help="Account name from accounts.json (e.g. Taylor/Rosa/Yvonne), "
                             "or 'all' to run all accounts sequentially")
    return parser


def run_job(job: run_status.Job, per_account: PerAccount, *,
            argv: Sequence[str] | None = None, account: str | None = None) -> int:
    """跑一次逐帳戶的排程，回傳 exit code（有帳戶失敗 = 1）。

    account 沒給時從 argv（預設 sys.argv）解析 `--account`；腳本有自己的參數時
    先用 `account_parser` 解析，再把 account 傳進來。
    """
    setup_logging(job.key)
    quiet_ib_console()
    if account is None:
        account = account_parser(f"QuantCore {job.name}").parse_args(argv).account
    scheduled = account.lower() == "all"
    digest = Digest(f"{job.title} {date.today()}", icon=job.icon)
    names: list[str] = []
    # 當日排程總結用的執行紀錄（#38）：只記排程的 --account all
    with run_status.recording(job.key, lambda: digest.outcomes(names),
                              enabled=scheduled and job.records):
        names.extend([a["name"] for a in account_utils.load_accounts()] if scheduled
                     else [account])
        failures = _run_accounts(job, names, per_account, digest,
                                 summary=scheduled and job.summary)
    if failures:
        log.error(f"{job.name} finished with {len(failures)} failure(s): "
                  + "; ".join(f"{n}={msg}" for n, msg in failures))
        return 1
    return 0


def _run_accounts(job: run_status.Job, names: Sequence[str], per_account: PerAccount,
                  digest: Digest, *, summary: bool) -> list[tuple[str, str]]:
    """依序跑各帳戶，合成一則發送。回傳失敗清單 [(帳戶, 錯誤)]。

    三帳戶的 Gateway port、clientId、state 檔互相獨立，前一個帳戶失敗不該拖累
    其他帳戶（#29）。
    """
    failures: list[tuple[str, str]] = []
    started: list[str] = []
    try:
        for name in names:
            started.append(name)
            try:
                ctx = AccountCtx.from_name(name, job.role)
                log.info(f">>> Account: {ctx.name} ({ctx.account}) port={ctx.port} <<<")
                per_account(ctx, digest)
            except Exception as e:
                failures.append((name, str(e)))
                log.error(f"Account {name} failed (continuing to next): {e}", exc_info=True)
                if not digest.has(name):
                    digest.add(name, job.failed, [error_line(e)], "error",
                               **telegram_dest(name))
    except BaseException:
        for name in names[len(started):]:
            digest.add(name, "⚠️ 未執行", ["手動中止，本輪未執行"],
                       "warn", **telegram_dest(name))
        raise
    finally:
        if summary:
            run_status.attach_daily_summary(digest, names)
        digest.send()
    return failures


def run_task(job: run_status.Job, body: Callable[[], None],
             outcomes: Callable[[], Mapping[str, run_status.Outcome]]) -> int:
    """不分帳戶的排程（04:30 資料更新）：log ＋ 執行紀錄；通知由本體自己發。"""
    setup_logging(job.key)
    with run_status.recording(job.key, outcomes):
        body()
    return 0
