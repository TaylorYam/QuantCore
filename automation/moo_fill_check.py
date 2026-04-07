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

# ── 讀取設定 ──────────────────────────────────────────────────────────────────
cfg_path = BASE_DIR / 'config.json'
with open(cfg_path, encoding='utf-8') as f:
    cfg = json.load(f)

HOST         = cfg.get('host',           '127.0.0.1')
PORT         = cfg.get('port',           7497)
ACCOUNT      = cfg.get('account',        '')
ACCOUNT_NAME = cfg.get('account_name',   '')
TG_TOKEN     = cfg.get('telegram_token',   '')
TG_CHAT_ID   = cfg.get('telegram_chat_id', '')
CLIENT_ID    = 22


def _apply_config(cfg_dict: dict) -> None:
    """多帳戶切換用"""
    global HOST, PORT, ACCOUNT, ACCOUNT_NAME, TG_TOKEN, TG_CHAT_ID, CLIENT_ID
    HOST         = cfg_dict.get('host',           '127.0.0.1')
    PORT         = cfg_dict.get('port',           7497)
    ACCOUNT      = cfg_dict.get('account',        '')
    ACCOUNT_NAME = cfg_dict.get('account_name',   '')
    TG_TOKEN     = cfg_dict.get('telegram_token',   '')
    TG_CHAT_ID   = cfg_dict.get('telegram_chat_id', '')
    from account_utils import get_client_id, ROLE_MOO_CHECK
    CLIENT_ID    = get_client_id(ACCOUNT_NAME, ROLE_MOO_CHECK) if ACCOUNT_NAME else 22

from ib_insync import IB, util
util.logToConsole(logging.WARNING)
logging.root.setLevel(logging.INFO)


# ── Telegram 通知 ─────────────────────────────────────────────────────────────
def tg_send(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        url  = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            'chat_id':    TG_CHAT_ID,
            'text':       msg,
            'parse_mode': 'HTML',
        }).encode()
        req = urllib.request.Request(url, data=data, method='POST')
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                log.warning(f"Telegram HTTP {resp.status}")
    except Exception as e:
        log.warning(f"Telegram notify failed: {e}")


def main():
    start_ts = datetime.now()
    log.info(f"{'='*60}")
    log.info(f"  MOO Fill Check  {date.today()}")
    log.info(f"{'='*60}")

    ib = IB()
    try:
        ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=20)
        log.info(f"Connected  host={HOST} port={PORT} clientId={CLIENT_ID}")
    except Exception as e:
        log.error(f"IB connect failed: {e}")
        tg_send(f"<b>MOO Fill Check ❌</b>\n{date.today()}\nIB 連線失敗: {e}")
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
                _report_from_fills(moo_fills)
            else:
                msg = (f"<b>MOO Fill Check</b>\n"
                       f"{date.today()}\n\n"
                       f"今日無 MOO 委託紀錄")
                log.info("No MOO orders found today")
                tg_send(msg)
            return

        # 組裝報告
        acct_tag = f"[{ACCOUNT_NAME}] " if ACCOUNT_NAME else ""
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

        # 帳戶 NAV
        try:
            for v in ib.accountValues(ACCOUNT):
                if v.tag == 'NetLiquidation' and v.currency == 'USD':
                    nav = float(v.value)
                    lines.append(f"\n  NAV: <b>${nav:,.2f}</b>")
                    break
        except Exception:
            pass

        elapsed = (datetime.now() - start_ts).seconds
        lines.append(f"\nElapsed: {elapsed}s")

        report = "\n".join(lines)
        log.info(f"\n{report}")
        tg_send(report)

    except Exception as e:
        log.error(f"ERROR: {e}", exc_info=True)
        tg_send(f"<b>MOO Fill Check ❌</b>\n{date.today()}\nError: {str(e)[:300]}")

    finally:
        ib.disconnect()
        log.info("Disconnected from IB Gateway.")


def _report_from_fills(fills):
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
    tg_send(report)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description="QuantCore MOO Fill Check")
    parser.add_argument("--account", default=None,
                        help="Account name from accounts.json, or 'all'")
    args = parser.parse_args()

    if args.account:
        from account_utils import get_account_config, get_all_account_configs
        accounts = (get_all_account_configs() if args.account.lower() == 'all'
                    else [get_account_config(args.account)])
        for acfg in accounts:
            _apply_config(acfg)
            log.info(f">>> Account: {ACCOUNT_NAME} ({ACCOUNT}) port={PORT} <<<")
            main()
    else:
        main()
