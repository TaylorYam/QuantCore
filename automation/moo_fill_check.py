#!/usr/bin/env python3
"""
QuantCore — MOO 開盤成交回報
開盤後 1 分鐘（09:31 ET = 21:31 TW）自動連線 IB 檢查 MOO 委託成交狀況，
並透過 Telegram 發送回報。

排程：
  週二 21:31 → python moo_fill_check.py   (SR 調倉回報)
  週三 21:31 → python moo_fill_check.py   (SP500 調倉回報)

也可手動執行：
  python moo_fill_check.py
"""

import json, logging, sys, urllib.request, urllib.parse
from pathlib import Path
from datetime import date, datetime

import numpy as np

# ── 路徑設定 ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
LOG_DIR  = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

# ── 日誌設定 ──────────────────────────────────────────────────────────────────
log_file = LOG_DIR / f"moo_fill_{date.today().strftime('%Y%m%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)s  %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ],
    force=True,
)
log = logging.getLogger(__name__)

from ib_insync import util
util.logToConsole(logging.WARNING)
logging.root.setLevel(logging.INFO)


# ── Telegram 通知（統一由 notify.py 提供）──────────────────────────────────
from notify import tg_send as _tg

# ── AccountCtx（S6：ctx 為必要參數）────────────────────────────────────
from account_ctx import AccountCtx

# ── IB 連線 helper（S8：統一由 ib_fetcher 提供）────────────────────────
import ib_fetcher


def main(ctx: AccountCtx):
    """MOO 成交回報。所有設定從 ctx 取得。"""
    account    = ctx.account
    acct_name  = ctx.name
    tg_token   = ctx.tg_token
    tg_chat_id = ctx.tg_chat_id

    start_ts = datetime.now()
    log.info(f"{'='*60}")
    log.info(f"  MOO Fill Check  {date.today()}")
    log.info(f"{'='*60}")

    try:
        ib = ib_fetcher.connect(ctx)
    except Exception as e:
        log.error(f"IB connect failed: {e}")
        _tg(f"<b>MOO Fill Check ❌</b>\n{date.today()}\nIB 連線失敗: {e}",
            token=tg_token, chat_id=tg_chat_id)
        return

    try:
        # 取得今日所有已完成和進行中的委託
        trades = ib.trades()
        today_str = date.today().strftime('%Y%m%d')

        # 篩選今日 MOO 委託 (orderType=MKT + tif=OPG)
        moo_trades = []
        for t in trades:
            o = t.order
            # MOO = MKT + OPG
            if o.orderType == 'MKT' and o.tif == 'OPG':
                moo_trades.append(t)

        if not moo_trades:
            # 也檢查 executions 以防 trades 已清
            log.info("No active MOO trades found, checking executions...")
            fills = ib.fills()
            moo_fills = [f for f in fills if f.execution.orderId > 0]
            # 用 executions 組報告
            if moo_fills:
                _report_from_fills(moo_fills, tg_token=tg_token, tg_chat_id=tg_chat_id)
            else:
                msg = (f"<b>MOO Fill Check</b>\n"
                       f"{date.today()}\n\n"
                       f"今日無 MOO 委託紀錄")
                log.info("No MOO orders found today")
                _tg(msg, token=tg_token, chat_id=tg_chat_id)
            return

        # 組裝報告
        acct_tag = f"[{acct_name}] " if acct_name else ""
        lines = [
            f"<b>{acct_tag}MOO 開盤成交回報</b>",
            f"Date: {date.today()}",
            f"Check Time: {datetime.now().strftime('%H:%M:%S')} (TW)",
            f"委託數: {len(moo_trades)} 筆",
            "",
        ]

        all_filled = True
        total_buy_val = 0.0
        total_sell_val = 0.0

        for t in moo_trades:
            sym    = t.contract.symbol
            action = t.order.action
            qty    = int(t.order.totalQuantity)
            status = t.orderStatus.status
            filled = int(t.orderStatus.filled)
            avg_px = t.orderStatus.avgFillPrice

            if filled < qty:
                all_filled = False

            fill_val = filled * avg_px if avg_px else 0
            if action == 'BUY':
                total_buy_val += fill_val
            else:
                total_sell_val += fill_val

            # 狀態 icon
            if status == 'Filled':
                icon = "✅"
            elif status == 'Cancelled':
                icon = "🚫"
            elif filled > 0:
                icon = "⚠️"  # 部分成交
            else:
                icon = "⏳"  # 未成交

            action_tag = "🔴 SELL" if action == 'SELL' else "🟢 BUY "
            px_str = f"@ ${avg_px:.2f}" if avg_px and avg_px > 0 else ""
            val_str = f"  ~${fill_val:,.0f}" if fill_val > 0 else ""

            lines.append(
                f"  {icon} {action_tag} {filled}/{qty}sh"
                f"  {sym}  {px_str}{val_str}"
                f"  [{status}]"
            )
            log.info(f"  {action} {sym}: {filled}/{qty}sh @ ${avg_px:.2f} [{status}]")

        # 彙總
        lines.append("")
        overall = "✅ 全部成交" if all_filled else "⚠️ 有未完成委託"
        lines.append(f"<b>{overall}</b>")
        if total_sell_val > 0:
            lines.append(f"  賣出金額: ${total_sell_val:,.0f}")
        if total_buy_val > 0:
            lines.append(f"  買入金額: ${total_buy_val:,.0f}")
        net = total_sell_val - total_buy_val
        if total_sell_val > 0 or total_buy_val > 0:
            sign = "+" if net >= 0 else ""
            lines.append(f"  淨現金流: {sign}${net:,.0f}")

        # 帳戶 NAV（S8：走 ib_fetcher）
        try:
            nav = ib_fetcher.get_nav(ib, ctx)
            lines.append(f"\n  NAV: <b>${nav:,.2f}</b>")
        except Exception:
            pass

        elapsed = (datetime.now() - start_ts).seconds
        lines.append(f"\nElapsed: {elapsed}s")

        report = "\n".join(lines)
        log.info(f"\n{report}")
        _tg(report, token=tg_token, chat_id=tg_chat_id)

    except Exception as e:
        log.error(f"ERROR: {e}", exc_info=True)
        _tg(f"<b>MOO Fill Check ❌</b>\n{date.today()}\nError: {str(e)[:300]}",
            token=tg_token, chat_id=tg_chat_id)

    finally:
        ib.disconnect()
        log.info("Disconnected from IB Gateway.")


def _report_from_fills(fills, tg_token: str, tg_chat_id: str):
    """從 ib.fills() 組裝回報（當 trades 已清空時的備援）"""
    lines = [
        f"<b>MOO 開盤成交回報</b>",
        f"Date: {date.today()}",
        f"Check Time: {datetime.now().strftime('%H:%M:%S')} (TW)",
        f"（從成交紀錄回溯）",
        "",
    ]
    total_buy_val = 0.0
    total_sell_val = 0.0

    for f in fills:
        ex = f.execution
        sym    = f.contract.symbol
        action = ex.side
        shares = int(ex.shares)
        avg_px = ex.avgPrice
        fill_val = shares * avg_px

        if action == 'BOT':
            action_tag = "🟢 BUY "
            total_buy_val += fill_val
        else:
            action_tag = "🔴 SELL"
            total_sell_val += fill_val

        lines.append(
            f"  ✅ {action_tag} {shares}sh  {sym}"
            f"  @ ${avg_px:.2f}  ~${fill_val:,.0f}"
        )
        log.info(f"  {action} {sym}: {shares}sh @ ${avg_px:.2f}")

    lines.append("")
    if total_sell_val > 0:
        lines.append(f"  賣出金額: ${total_sell_val:,.0f}")
    if total_buy_val > 0:
        lines.append(f"  買入金額: ${total_buy_val:,.0f}")

    report = "\n".join(lines)
    log.info(f"\n{report}")
    _tg(report, token=tg_token, chat_id=tg_chat_id)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description="QuantCore MOO Fill Check")
    parser.add_argument("--account", required=True,
                        help="Account name from accounts.json (e.g. Taylor/Rosa/Yvonne), "
                             "or 'all' to run all accounts sequentially")
    args = parser.parse_args()

    from account_utils import load_accounts, ROLE_MOO_CHECK

    if args.account.lower() == 'all':
        names = [a['name'] for a in load_accounts()]
    else:
        names = [args.account]

    for name in names:
        ctx = AccountCtx.from_name(name, ROLE_MOO_CHECK)
        log.info(f">>> Account: {ctx.name} ({ctx.account}) port={ctx.port} <<<")
        main(ctx=ctx)
