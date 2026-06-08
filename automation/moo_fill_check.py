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
from log_setup import setup_logging, quiet_ib_console
log = logging.getLogger(__name__)

from ib_insync import util


# ── Telegram 通知（統一由 notify.py 提供）──────────────────────────────────
from notify import tg_send as _tg

# ── AccountCtx（S6：ctx 為必要參數）────────────────────────────────────
from account_ctx import AccountCtx

# ── IB 連線 helper（S8：統一由 ib_fetcher 提供）────────────────────────
import ib_fetcher


def _classify_direction(action: str, post_position: float,
                         filled_qty: int) -> tuple[str, str]:
    """
    根據 fill action 與 post-fill IB position 判斷方向。

    回傳 (icon, action_tag)。
      SELL + post<0 → 🔻 SHORT  (新開/加碼空單)
      SELL + post>=0 → 🔴 SELL  (清長/減長)
      BUY  + post>0  → 🟢 BUY   (加碼/新開多單)
      BUY  + post<=0 → 💚 COVER (回補空單；post=0 表示全平，post<0 表示部分回補)

    Skew LS XLC 短倉的常見訊息：
      開倉： SELL XLC → 🔻 SHORT
      平倉： BUY  XLC → 💚 COVER
    """
    if action == 'SELL':
        if post_position < -0.5:    # 視為 short (避免浮點誤差)
            return "🔻", "SHORT"
        return "🔴", "SELL "
    # action == 'BUY'
    if post_position > 0.5:
        return "🟢", "BUY  "
    if filled_qty > 0:
        # post ≤ 0 + 有成交 → 必然是 cover (從 short 平回 0 或仍是 short)
        return "💚", "COVER"
    return "🟢", "BUY  "


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

        # 抓 post-fill 部位（用於判斷 SHORT / COVER 方向）
        try:
            positions_after = {p.contract.symbol: float(p.position)
                                for p in ib.positions(account)
                                if p.contract.secType == 'STK'}
        except Exception as e:
            log.warning(f"Failed to fetch post-fill positions: {e}")
            positions_after = {}

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
        total_buy_val   = 0.0   # 含 BUY + COVER (現金流出)
        total_sell_val  = 0.0   # 含 SELL + SHORT (現金流入)
        total_short_val = 0.0   # 純 SHORT 開倉（單獨統計，方便看 short notional）
        total_cover_val = 0.0   # 純 COVER 平倉

        for t in moo_trades:
            sym    = t.contract.symbol
            action = t.order.action
            qty    = int(t.order.totalQuantity)
            status = t.orderStatus.status
            filled = int(t.orderStatus.filled)
            avg_px = t.orderStatus.avgFillPrice
            post_pos = positions_after.get(sym, 0.0)

            if filled < qty:
                all_filled = False

            fill_val = filled * avg_px if avg_px else 0

            # 狀態 icon（成交完成度）
            if status == 'Filled':
                stat_icon = "✅"
            elif status == 'Cancelled':
                stat_icon = "🚫"
            elif filled > 0:
                stat_icon = "⚠️"
            else:
                stat_icon = "⏳"

            # 方向 icon + tag（含 SHORT / COVER 辨識）
            dir_icon, action_tag = _classify_direction(action, post_pos, filled)

            # 累計現金流
            if action == 'BUY':
                total_buy_val += fill_val
                if action_tag.strip() == 'COVER':
                    total_cover_val += fill_val
            else:  # SELL
                total_sell_val += fill_val
                if action_tag.strip() == 'SHORT':
                    total_short_val += fill_val

            px_str = f"@ ${avg_px:.2f}" if avg_px and avg_px > 0 else ""
            val_str = f"  ~${fill_val:,.0f}" if fill_val > 0 else ""
            # post-fill position 後綴 (短倉用負號顯示)
            if abs(post_pos) > 0.5:
                pos_suffix = f"  → {int(post_pos):+d}sh"
            else:
                pos_suffix = "  → flat"

            lines.append(
                f"  {stat_icon} {dir_icon} {action_tag} {filled}/{qty}sh"
                f"  {sym}  {px_str}{val_str}{pos_suffix}"
                f"  [{status}]"
            )
            log.info(f"  {action_tag.strip()} {sym}: {filled}/{qty}sh "
                     f"@ ${avg_px:.2f}  post={post_pos:+.0f}  [{status}]")

        # 彙總
        lines.append("")
        overall = "✅ 全部成交" if all_filled else "⚠️ 有未完成委託"
        lines.append(f"<b>{overall}</b>")
        if total_sell_val > 0:
            sell_label = "賣出金額"
            if total_short_val > 0:
                sell_label += f"（含開空 ${total_short_val:,.0f}）"
            lines.append(f"  {sell_label}: ${total_sell_val:,.0f}")
        if total_buy_val > 0:
            buy_label = "買入金額"
            if total_cover_val > 0:
                buy_label += f"（含回補 ${total_cover_val:,.0f}）"
            lines.append(f"  {buy_label}: ${total_buy_val:,.0f}")
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
    setup_logging("moo_fill")
    quiet_ib_console()
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

    failures = []
    for name in names:
        try:
            ctx = AccountCtx.from_name(name, ROLE_MOO_CHECK)
            log.info(f">>> Account: {ctx.name} ({ctx.account}) port={ctx.port} <<<")
            main(ctx=ctx)
        except Exception as e:
            failures.append((name, str(e)))
            log.error(f"Account {name} failed (continuing to next): {e}")

    if failures:
        log.error(f"MOO fill check finished with {len(failures)} failure(s): "
                  + "; ".join(f"{n}={msg}" for n, msg in failures))
        sys.exit(1)
