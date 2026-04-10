#!/usr/bin/env python3
"""
QuantCore — 每日收盤後帳戶持倉報告
台灣時間 04:10（夏令）/ 05:10（冬令）由 Task Scheduler 執行

功能：
  1. 從 IB Gateway 取帳戶 NAV、保證金、持倉明細
  2. 發送 Telegram 持倉報告（含未實現損益）
  3. 寫入 Excel 每日快照
"""

import json, logging, sys, urllib.request, urllib.parse
from pathlib import Path
from datetime import date, datetime

import matplotlib
matplotlib.use('Agg')
import numpy as np

BASE_DIR = Path(__file__).parent
LOG_DIR  = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

sys.path.insert(0, str(BASE_DIR))
from excel_logger import append_daily_snapshot
from equity_chart import generate_equity_chart

# ── 日誌 ──────────────────────────────────────────────────────────────────────
log_file = LOG_DIR / f"daily_{date.today().strftime('%Y%m%d')}.log"
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

from ib_insync import IB, Stock, util
util.logToConsole(logging.WARNING)
logging.root.setLevel(logging.INFO)

# R1.3：SR ETF 常數單一來源在 ib_fetcher
from ib_fetcher import SR_ETFS


def _load_sp500_universe() -> set:
    """載入當前 S&P 500 成分股清單"""
    try:
        import pandas as pd
        ref = Path(__file__).parent.parent / "data" / "reference" / "sp500_constituents_daily_2015_2026.xlsx"
        if not ref.exists():
            return set()
        df = pd.read_excel(ref, index_col=0, parse_dates=True)
        if df.empty:
            return set()
        row = df.iloc[-1]
        return set(row.dropna().values) | {'SPY', 'SSO'}
    except Exception:
        return set()

SP500_UNIVERSE = _load_sp500_universe()


# ── Telegram（統一由 notify.py 提供）─────────────────────────────────────
from notify import tg_send as _tg, tg_send_photo as _tg_photo

# ── AccountCtx（S6：ctx 為所有函式的必要參數，無 module global 可 fallback）
from account_ctx import AccountCtx

# ── IB helpers 統一由 ib_fetcher 提供（S8 遷移）
from ib_fetcher import (
    get_account_info,
    get_positions_detail,
    get_prices,
)


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main(ctx: AccountCtx):
    """每日持倉報告。所有設定從 ctx 取得，thread-safe。"""
    host        = ctx.host
    port        = ctx.port
    client_id   = ctx.client_id
    account     = ctx.account
    acct_name   = ctx.name
    alloc_sr    = ctx.alloc_sr
    alloc_sp500 = ctx.alloc_sp500
    tg_token    = ctx.tg_token
    tg_chat_id  = ctx.tg_chat_id

    log.info(f"QuantCore daily report start — {date.today()}")
    ib = IB()
    ib.connect(host, port, clientId=client_id, timeout=20)
    log.info(f"Connected  account={account}")

    try:
        acct       = get_account_info(ib, ctx=ctx)
        nav        = acct['nav']
        cushion    = acct['cushion_pct']
        excess_liq = acct['excess_liq']
        pos_detail = get_positions_detail(ib, ctx=ctx)
        symbols    = [p.contract.symbol for p in pos_detail]
        prices     = get_prices(ib, symbols, ctx=ctx) if symbols else {}

        log.info(f"NAV = ${nav:,.2f}  Cushion = {cushion:.1f}%  ExcessLiq = ${excess_liq:,.0f}")

        # 保證金警戒顏色
        if cushion < 10:
            cushion_icon = "🔴"
        elif cushion < 25:
            cushion_icon = "🟡"
        else:
            cushion_icon = "🟢"

        # ── 組 Telegram 訊息 ──────────────────────────────────────────────
        acct_tag = f"[{acct_name}] " if acct_name else ""
        lines = [
            f"<b>{acct_tag}📊 QuantCore 每日持倉報告 — {date.today()}</b>",
            f"NAV: <b>${nav:,.2f}</b>  |  Alloc: SR {alloc_sr:.0%} + SP500 {alloc_sp500:.0%}",
            f"{cushion_icon} Margin Cushion: <b>{cushion:.1f}%</b>  ExcessLiq: ${excess_liq:,.0f}",
        ]

        if not pos_detail:
            lines.append("\n（無持倉）")
        else:
            # 分類持倉：SR ETFs vs SP500 個股 vs 其他（手動操作）
            sr_positions  = []
            sp_positions  = []
            other_positions = []
            for p in pos_detail:
                sym = p.contract.symbol
                if sym in SR_ETFS:
                    sr_positions.append(p)
                elif SP500_UNIVERSE and sym in SP500_UNIVERSE:
                    sp_positions.append(p)
                else:
                    other_positions.append(p)

            total_upnl       = 0.0
            strategy_mktval  = 0.0
            other_mktval     = 0.0

            def _fmt_position(p, bucket_upnl, bucket_mktval):
                sym, pos, avg_cost = p.contract.symbol, int(p.position), p.avgCost
                mkt_px   = prices.get(sym, 0)
                mkt_val  = pos * mkt_px
                upnl     = mkt_val - pos * avg_cost
                pct      = upnl / (pos * avg_cost) * 100 if avg_cost else 0
                weight = mkt_val / nav * 100 if nav else 0
                sign = "+" if upnl >= 0 else ""
                return (
                    f"  {sym}: {pos}sh ({weight:.0f}%)"
                    f"  均${avg_cost:.2f}→現${mkt_px:.2f}"
                    f"  {sign}{upnl:.2f}({sign}{pct:.1f}%)",
                    upnl, mkt_val
                )

            # SR 持倉
            if sr_positions:
                lines.append(f"\n<b>🔵 Sector Rotation ({alloc_sr:.0%}):</b>")
                for p in sorted(sr_positions, key=lambda x: -abs(x.position * prices.get(x.contract.symbol, 0))):
                    txt, upnl, mv = _fmt_position(p, total_upnl, strategy_mktval)
                    lines.append(txt)
                    total_upnl += upnl
                    strategy_mktval += mv

            # SP500 MOM 持倉
            if sp_positions:
                lines.append(f"\n<b>🟢 SP500 Momentum ({alloc_sp500:.0%}):</b>")
                for p in sorted(sp_positions, key=lambda x: -abs(x.position * prices.get(x.contract.symbol, 0))):
                    txt, upnl, mv = _fmt_position(p, total_upnl, strategy_mktval)
                    lines.append(txt)
                    total_upnl += upnl
                    strategy_mktval += mv
                    sym = p.contract.symbol
                    log.info(f"  {sym}: {int(p.position)}sh  uPnL {'+' if upnl >= 0 else ''}{upnl:.2f}")

            # 其他手動持倉
            if other_positions:
                lines.append(f"\n<b>⚪ Other (manual, excluded):</b>")
                for p in sorted(other_positions, key=lambda x: -abs(x.position * prices.get(x.contract.symbol, 0))):
                    txt, upnl, mv = _fmt_position(p, total_upnl, other_mktval)
                    lines.append(txt)
                    total_upnl += upnl
                    other_mktval += mv

            sign = "+" if total_upnl >= 0 else ""
            strategy_exp = strategy_mktval / nav * 100 if nav else 0
            total_mktval = strategy_mktval + other_mktval
            lines.append(f"\n  策略部位: ${strategy_mktval:,.0f}  ({strategy_exp:.0f}% exposure)")
            if other_mktval > 0:
                lines.append(f"  手動部位: ${other_mktval:,.0f}  (excluded)")
            lines.append(f"  <b>未實現損益: {sign}${total_upnl:,.2f}</b>")

        _tg("\n".join(lines), token=tg_token, chat_id=tg_chat_id)
        log.info("Daily report sent.")

        append_daily_snapshot(nav, pos_detail, prices,
                              cushion_pct=cushion, excess_liq=excess_liq,
                              account_name=acct_name)
        log.info(f"Excel snapshot written -> portfolio_tracker.xlsx")

        # ── 產生權益曲線圖 + 發送 ────────────────────────────────────────
        try:
            chart_path = generate_equity_chart(account_name=acct_name)
            if chart_path:
                caption = f"{acct_tag}QuantCore 權益曲線 — {date.today()}"
                _tg_photo(chart_path, token=tg_token, chat_id=tg_chat_id, caption=caption)
                log.info(f"Equity chart generated: {chart_path}")
            else:
                log.info("Not enough data to generate equity chart.")
        except Exception as chart_err:
            log.warning(f"Equity chart failed (non-fatal): {chart_err}")

    except Exception as e:
        log.error(f"ERROR: {e}", exc_info=True)
        _tg(f"<b>❌ QuantCore 每日報告失敗</b>\n{e}",
            token=tg_token, chat_id=tg_chat_id)
        raise

    finally:
        ib.disconnect()
        log.info("Disconnected.")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description="QuantCore Daily Report")
    parser.add_argument("--account", required=True,
                        help="Account name from accounts.json (e.g. Taylor/Rosa/Yvonne), "
                             "or 'all' to run all accounts sequentially")
    args = parser.parse_args()

    from account_utils import load_accounts, ROLE_REPORT

    if args.account.lower() == 'all':
        names = [a['name'] for a in load_accounts()]
    else:
        names = [args.account]

    for name in names:
        ctx = AccountCtx.from_name(name, ROLE_REPORT)
        log.info(f">>> Account: {ctx.name} ({ctx.account}) port={ctx.port} <<<")
        main(ctx=ctx)
