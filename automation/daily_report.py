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
from log_setup import setup_logging, quiet_ib_console
log = logging.getLogger(__name__)

from ib_insync import IB, Stock, util

# R1.3：SR ETF 常數單一來源在 ib_fetcher
from ib_fetcher import SR_ETFS

# Skew LS 反向 ETF 清單（給持倉分類用）
try:
    from strategies.sector_skew_ls import config as _skewls_cfg
    SKEWLS_INVERSE_TICKERS = set(_skewls_cfg.INVERSE_TICKERS)
except Exception:
    SKEWLS_INVERSE_TICKERS = set()


def _load_sp500_universe() -> set:
    """載入 S&P 500 成分股「歷史聯集」（含 SPY/SSO）。

    用歷史聯集而非當前清單：使用者透過 SP500 strategy 買進的個股，
    若該檔被 SP500 移除 (如 WDC 2024, STX 2018)，仍需正確歸類為
    SP500 bucket，而非誤判為 Other。
    """
    try:
        # 確保 strategies / signals 可 import
        import sys as _sys
        from pathlib import Path as _P
        _root = _P(__file__).parent.parent
        if str(_root) not in _sys.path:
            _sys.path.insert(0, str(_root))
        from signals import get_sp500_universe
        return get_sp500_universe(historical=True)
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


def main(ctx: AccountCtx):
    """每日持倉報告。所有設定從 ctx 取得，thread-safe。"""
    host        = ctx.host
    port        = ctx.port
    client_id   = ctx.client_id
    account     = ctx.account
    acct_name   = ctx.name
    alloc_sr    = ctx.alloc_sr
    alloc_sp500 = ctx.alloc_sp500
    alloc_skewls = ctx.alloc_skewls
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
        # 標價用 Reference Close（最近官方收盤，CSV index[-1]）；IB 僅在 CSV 缺檔
        # 時 fallback——避免週末/盤前 IB 回傳陳舊 tick 蓋過正確收盤（ADR-0003）。
        prices     = mark_at_reference_close(ib, symbols, ctx) if symbols else {}

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
        alloc_line = f"Alloc: SR {alloc_sr:.0%} + SP500 {alloc_sp500:.0%}"
        if alloc_skewls > 0:
            alloc_line += f" + SkewLS {alloc_skewls:.0%}"
        total_alloc = alloc_sr + alloc_sp500 + alloc_skewls
        alloc_line += f" = {total_alloc:.0%}"
        lines = [
            f"<b>{acct_tag}📊 QuantCore 每日持倉報告 — {date.today()}</b>",
            f"NAV: <b>${nav:,.2f}</b>  |  {alloc_line}",
            f"{cushion_icon} Margin Cushion: <b>{cushion:.1f}%</b>  ExcessLiq: ${excess_liq:,.0f}",
        ]

        if not pos_detail:
            lines.append("\n（無持倉）")
        else:
            # 分類持倉 (Issue 2 Option 2 — 純 ETF/Stock/Other 三桶)
            #   ETF Holdings   : sector ETF (SR) + inverse ETF (SkewLS) + 任何 short 部位
            #   Stock Holdings : SP500 個股
            #   Other          : 手動操作
            # 重疊（SR + SkewLS sector ETF long）併入 ETF Holdings；IB 端本來也是合併部位
            etf_positions   = []
            sp_positions    = []
            other_positions = []
            for p in pos_detail:
                sym = p.contract.symbol
                if sym in SR_ETFS or sym in SKEWLS_INVERSE_TICKERS:
                    etf_positions.append(p)
                elif SP500_UNIVERSE and sym in SP500_UNIVERSE:
                    sp_positions.append(p)
                else:
                    other_positions.append(p)

            total_upnl       = 0.0
            strategy_mktval  = 0.0
            other_mktval     = 0.0

            def _fmt_position(p):
                sym, pos, avg_cost = p.contract.symbol, int(p.position), p.avgCost
                mkt_px   = prices.get(sym, 0)
                mkt_val  = pos * mkt_px                          # short 時為負
                upnl     = mkt_val - pos * avg_cost              # 公式對 long/short 都對
                cost_abs = abs(pos * avg_cost)
                pct      = upnl / cost_abs * 100 if cost_abs else 0
                weight = mkt_val / nav * 100 if nav else 0       # short 時為負 %
                sign = "+" if upnl >= 0 else ""
                tag  = " SHORT" if pos < 0 else ""
                return (
                    f"  {sym}{tag}: {pos}sh ({weight:+.0f}%)"
                    f"  均${avg_cost:.2f}→現${mkt_px:.2f}"
                    f"  {sign}{upnl:.2f}({sign}{pct:.1f}%)",
                    upnl, mkt_val
                )

            # ETF Holdings (SR + SkewLS 合併)
            if etf_positions:
                if alloc_skewls > 0:
                    header = (f"\n<b>🔵 ETF Holdings "
                              f"(SR {alloc_sr:.0%} + SkewLS {alloc_skewls:.0%}):</b>")
                else:
                    header = f"\n<b>🔵 ETF Holdings (SR {alloc_sr:.0%}):</b>"
                lines.append(header)
                for p in sorted(etf_positions, key=lambda x: -abs(x.position * prices.get(x.contract.symbol, 0))):
                    txt, upnl, mv = _fmt_position(p)
                    lines.append(txt)
                    total_upnl += upnl
                    strategy_mktval += mv

            # Stock Holdings (SP500 MOM)
            if sp_positions:
                lines.append(f"\n<b>🟢 Stock Holdings (MOM {alloc_sp500:.0%}):</b>")
                for p in sorted(sp_positions, key=lambda x: -abs(x.position * prices.get(x.contract.symbol, 0))):
                    txt, upnl, mv = _fmt_position(p)
                    lines.append(txt)
                    total_upnl += upnl
                    strategy_mktval += mv
                    sym = p.contract.symbol
                    log.info(f"  {sym}: {int(p.position)}sh  uPnL {'+' if upnl >= 0 else ''}{upnl:.2f}")

            # 其他手動持倉
            if other_positions:
                lines.append(f"\n<b>⚪ Other (manual, excluded):</b>")
                for p in sorted(other_positions, key=lambda x: -abs(x.position * prices.get(x.contract.symbol, 0))):
                    txt, upnl, mv = _fmt_position(p)
                    lines.append(txt)
                    total_upnl += upnl
                    other_mktval += mv

            sign = "+" if total_upnl >= 0 else ""
            strategy_exp = strategy_mktval / nav * 100 if nav else 0
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
    setup_logging("daily")
    quiet_ib_console()
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

    failures = []
    for name in names:
        try:
            ctx = AccountCtx.from_name(name, ROLE_REPORT)
            log.info(f">>> Account: {ctx.name} ({ctx.account}) port={ctx.port} <<<")
            main(ctx=ctx)
        except Exception as e:
            failures.append((name, str(e)))
            log.error(f"Account {name} failed (continuing to next): {e}")

    if failures:
        log.error(f"Daily report finished with {len(failures)} failure(s): "
                  + "; ".join(f"{n}={msg}" for n, msg in failures))
        sys.exit(1)
