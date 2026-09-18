#!/usr/bin/env python3
"""
QuantCore — 每日收盤後帳戶快照
台灣時間 04:40 由 Task Scheduler 執行

功能：
  1. 從 IB Gateway 取帳戶 NAV、保證金、持倉明細
  2. 寫入 Excel 每日快照
  3. Margin Cushion 低於 25% 才發 Telegram 警示（#26：持倉報告與權益曲線
     不再推播）；失敗（含連不上 IB）發 ❌（#38）；三帳戶合成一則
"""

import html
import logging
import sys
from pathlib import Path
from datetime import date

import matplotlib
matplotlib.use('Agg')

BASE_DIR = Path(__file__).parent
LOG_DIR  = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

sys.path.insert(0, str(BASE_DIR))
from excel_logger import append_daily_snapshot

# ── 日誌 ──────────────────────────────────────────────────────────────────────
from log_setup import setup_logging, quiet_ib_console
log = logging.getLogger(__name__)

from ib_insync import IB

# ── Telegram（統一由 notify.py 提供）─────────────────────────────────────
from notify import Digest

# ── AccountCtx（S6：ctx 為所有函式的必要參數，無 module global 可 fallback）
from account_ctx import AccountCtx

# ── IB helpers 統一由 ib_fetcher 提供（S8 遷移）
from ib_fetcher import (
    get_account_info,
    get_positions_detail,
    get_prices,
)

# Margin Cushion 警示門檻（%）。沿用原報告的 🟡 分界；2026-06-18 → 09-15
# 三帳戶最低 40.6%，正常不會觸發。
CUSHION_ALERT_PCT = 25.0


def cushion_alert(nav: float, cushion_pct: float,
                  excess_liq: float) -> tuple[str, list[str], str] | None:
    """Margin Cushion 低於門檻 → Digest 段落 (headline, body, level)；否則 None。

    nav <= 0 視為帳戶資料沒讀到（get_account_info 缺欄位時預設 0），
    不警示 — 避免把「沒資料」誤判成「cushion 0%」。
    """
    if nav <= 0 or cushion_pct >= CUSHION_ALERT_PCT:
        return None
    return (
        f"Margin Cushion <b>{cushion_pct:.1f}%</b>（低於 {CUSHION_ALERT_PCT:.0f}%）",
        [f"ExcessLiq ${excess_liq:,.0f} · NAV ${nav:,.2f}"],
        "alert",
    )


# ── 主流程 ────────────────────────────────────────────────────────────────────
def mark_at_reference_close(ib, symbols, ctx):
    """每檔以最近官方收盤（Reference Close = CSV index[-1]）標價；CSV 缺該檔才
    回退 IB 報價。

    daily report 是 EOD 快照，正確 mark 是最近官方收盤，而非盤後/週末可能陳舊的
    即時 tick（見 ADR-0003）。live 下單取價走 `robust_get_prices`，不受此影響。
    """
    from price_utils import load_csv_close
    marks, missing = {}, []
    for s in symbols:
        c = load_csv_close(s)
        if c > 0:
            marks[s] = c
        else:
            missing.append(s)
    if missing:
        ib_prices = get_prices(ib, missing, ctx=ctx)
        for s in missing:
            marks[s] = ib_prices.get(s, 0)
    return marks


def main(ctx: AccountCtx, digest: Digest | None = None):
    """每日快照 + 保證金警示。所有設定從 ctx 取得，thread-safe。

    警示加進 digest（__main__ 三帳戶合併一則發送）；沒給 digest 時自己建一則
    並在結束時發送。
    """
    own_digest = digest is None
    if own_digest:
        digest = Digest(f"每日報告 {date.today()}", icon="📊")
    dest = {"token": ctx.tg_token, "chat_id": ctx.tg_chat_id}
    acct_name = ctx.name

    log.info(f"QuantCore daily report start — {date.today()}")
    ib = IB()
    ib.connect(ctx.host, ctx.port, clientId=ctx.client_id, timeout=20)
    log.info(f"Connected  account={ctx.account}")

    try:
        acct       = get_account_info(ib, ctx=ctx)
        nav        = acct['nav']
        cushion    = acct['cushion_pct']
        excess_liq = acct['excess_liq']
        pos_detail = get_positions_detail(ib, ctx=ctx)
        from symbol_contract import canonical_symbol
        symbols    = [canonical_symbol(p.contract.symbol) for p in pos_detail]
        # 標價用 Reference Close（最近官方收盤，CSV index[-1]）；IB 僅在 CSV 缺檔
        # 時 fallback——避免週末/盤前 IB 回傳陳舊 tick 蓋過正確收盤（ADR-0003）。
        prices     = mark_at_reference_close(ib, symbols, ctx) if symbols else {}

        log.info(f"NAV = ${nav:,.2f}  Cushion = {cushion:.1f}%  ExcessLiq = ${excess_liq:,.0f}")

        alert = cushion_alert(nav, cushion, excess_liq)
        if alert is not None:
            digest.add(acct_name, *alert, **dest)
            log.warning(f"Margin cushion {cushion:.1f}% < {CUSHION_ALERT_PCT:.0f}% "
                        f"— Telegram alert queued")

        append_daily_snapshot(nav, pos_detail, prices,
                              cushion_pct=cushion, excess_liq=excess_liq,
                              account_name=acct_name)
        log.info(f"Excel snapshot written -> portfolio_tracker.xlsx")

    except Exception as e:
        log.error(f"ERROR: {e}", exc_info=True)
        digest.add(acct_name, "❌ 每日報告失敗", [html.escape(str(e)[:300])],
                   "error", **dest)
        raise

    finally:
        ib.disconnect()
        log.info("Disconnected.")
        if own_digest:
            digest.send()


def run_all(names: list[str], digest: Digest) -> list[tuple[str, str]]:
    """依序跑多個帳戶，警示合成一則發送。回傳失敗清單 [(帳戶, 錯誤)]。

    main() 沒來得及加段落就失敗的帳戶（連不上 IB、ctx 建不起來）補 ❌ 段落
    — 以前只寫 log，Gateway 沒開時 04:40 失敗沒有任何通知（#38）。
    """
    from account_utils import ROLE_REPORT, telegram_dest

    failures = []
    try:
        for name in names:
            try:
                ctx = AccountCtx.from_name(name, ROLE_REPORT)
                log.info(f">>> Account: {ctx.name} ({ctx.account}) port={ctx.port} <<<")
                main(ctx=ctx, digest=digest)
            except Exception as e:
                failures.append((name, str(e)))
                log.error(f"Account {name} failed (continuing to next): {e}")
                if not digest.has(name):
                    digest.add(name, "❌ 每日報告失敗", [html.escape(str(e)[:300])],
                               "error", **telegram_dest(name))
    finally:
        digest.send()
    return failures


if __name__ == '__main__':
    setup_logging("daily")
    quiet_ib_console()
    import argparse
    parser = argparse.ArgumentParser(description="QuantCore Daily Report")
    parser.add_argument("--account", required=True,
                        help="Account name from accounts.json (e.g. Taylor/Rosa/Yvonne), "
                             "or 'all' to run all accounts sequentially")
    args = parser.parse_args()

    from account_utils import load_accounts
    import run_status

    all_accounts = args.account.lower() == 'all'
    names: list[str] = []
    digest = Digest(f"每日報告 {date.today()}", icon="📊")
    # 當日排程總結用的執行紀錄（#38）：只記排程的 --account all
    with run_status.recording("daily", lambda: digest.outcomes(names),
                              enabled=all_accounts):
        names.extend([a['name'] for a in load_accounts()] if all_accounts
                     else [args.account])
        failures = run_all(names, digest)

    if failures:
        log.error(f"Daily report finished with {len(failures)} failure(s): "
                  + "; ".join(f"{n}={msg}" for n, msg in failures))
        sys.exit(1)
