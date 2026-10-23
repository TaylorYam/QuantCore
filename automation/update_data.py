#!/usr/bin/env python3
"""
QuantCore — 每日收盤後資料更新
美東 16:30（收盤後 30 分鐘）由 Task Scheduler 執行

流程：
  1. 更新 ETF 資料（11 板塊 + SPY/SSO/QQQ/VIX）
  2. 更新 SP500 成分股資料（~500 檔）
  3. 有失敗才發 Telegram（#26：成功不通知；資料沒更新到時，16:00 日度風控
     會發「資料錯位中止」）
"""

import html
import logging, sys
from pathlib import Path
from datetime import date, datetime

BASE_DIR = Path(__file__).parent
LOG_DIR  = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

import _repo_root  # noqa: E402,F401  QuantCore root 可 import data

log = logging.getLogger(__name__)
from data.update_outcomes import outcome_errors

# ── Telegram（統一由 notify.py 提供；config.json 的共用目的地）─────────────
from notify import send_system

# ── 排程執行殼（log、執行紀錄；#206）
import run_status
import scheduled_run


def failure_message(errors: list[str], elapsed: int, today: date) -> str | None:
    """資料更新失敗通知；全部成功回 None（不通知）。錯誤字串會 HTML escape
    — 例外訊息常含 `<urlopen error ...>`，未 escape 會讓 Telegram 拒收整則。"""
    if not errors:
        return None
    return (
        f"<b>⚠️ QuantCore 資料更新部分失敗</b>\n"
        f"Date: {today}\n"
        f"Errors:\n" + "\n".join(f"  • {html.escape(e)}" for e in errors) +
        f"\n\nElapsed: {elapsed}s"
    )


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main() -> list[str]:
    start_ts = datetime.now()
    log.info(f"{'='*60}")
    log.info(f"  QuantCore Data Update  {date.today()}")
    log.info(f"{'='*60}")

    errors = []

    # ── 1. 更新 ETF ──────────────────────────────────────────────────────
    log.info("Step 1: Updating ETFs...")
    try:
        from data.update_data import update_etfs
        errors.extend(outcome_errors(update_etfs() or []))
        log.info("ETF update done.")
    except Exception as e:
        log.error(f"ETF update failed: {e}", exc_info=True)
        errors.append(f"ETF: {e}")

    # ── 2. 更新個股 ──────────────────────────────────────────────────────
    log.info("Step 2: Updating stocks...")
    try:
        from data.update_data import update_stocks
        errors.extend(outcome_errors(update_stocks() or []))
        log.info("Stock update done.")
    except Exception as e:
        log.error(f"Stock update failed: {e}", exc_info=True)
        errors.append(f"Stocks: {e}")

    elapsed = (datetime.now() - start_ts).seconds
    for e in errors:
        log.warning(f"Update error: {e}")

    # ── 3. Telegram 通知（只在失敗時）──────────────────────────────────────
    msg = failure_message(errors, elapsed, date.today())
    if msg is not None:
        send_system(msg)                      # config.json 共用頻道（#226）
    log.info(f"Done. Elapsed: {elapsed}s  Errors: {len(errors)}")
    return errors


def run_outcome(errors: list[str]) -> dict:
    """當日排程總結用的結果（#38）：資料更新不分帳戶，key 為空字串。"""
    if errors:
        return {"": ("warn", "⚠️ 部分失敗")}
    return {"": ("ok", None)}


def run() -> int:
    """排程入口：資料更新不分帳戶，執行殼只負責 log 與執行紀錄。"""
    errors: list[str] = []
    return scheduled_run.run_task(run_status.UPDATE_JOB, lambda: errors.extend(main()),
                                  lambda: run_outcome(errors))


if __name__ == '__main__':
    sys.exit(run())
