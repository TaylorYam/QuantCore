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

log = logging.getLogger(__name__)

# ── Telegram（統一由 notify.py 提供）─────────────────────────────────────
from notify import Digest, error_line

# ── 排程執行殼（log、逐帳戶、失敗段落、執行紀錄、exit code；#206）
import run_status
import scheduled_run

# ── AccountCtx（S6：ctx 為所有函式的必要參數，無 module global 可 fallback）
from account_ctx import AccountCtx

# ── IB 連線經 Broker（#185）；權益、持倉、報價都在 broker.Connection 上
import broker

# Margin Cushion 警示門檻（%）。沿用原報告的 🟡 分界；2026-06-18 → 09-15
# 三帳戶最低 40.6%，正常不會觸發。
CUSHION_ALERT_PCT = 25.0


def cushion_alert(nav: float, cushion_pct: float,
                  excess_liq: float) -> tuple[str, list[str], str] | None:
    """Margin Cushion 低於門檻 → Digest 段落 (headline, body, level)；否則 None。

    nav <= 0 視為帳戶資料沒讀到，不警示 — 避免把「沒資料」誤判成「cushion 0%」。
    （Broker 讀不到權益或 cushion 時直接拋例外，正常流程不會傳 0 進來。）
    """
    if nav <= 0 or cushion_pct >= CUSHION_ALERT_PCT:
        return None
    return (
        f"Margin Cushion <b>{cushion_pct:.1f}%</b>（低於 {CUSHION_ALERT_PCT:.0f}%）",
        [f"ExcessLiq ${excess_liq:,.0f} · NAV ${nav:,.2f}"],
        "alert",
    )


# ── 主流程 ────────────────────────────────────────────────────────────────────
def mark_at_reference_close(conn, symbols):
    """每檔以最近官方收盤（Reference Close = CSV index[-1]）標價；CSV 缺該檔才
    回退 IB 報價。

    daily report 是 EOD 快照，正確 mark 是最近官方收盤，而非盤後/週末可能陳舊的
    即時 tick（見 ADR-0003）。live 下單取價走 Broker 的參考價（`Connection.prices`），不受此影響。
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
        ib_prices = conn.prices(missing)
        for s in missing:
            marks[s] = ib_prices.get(s, 0)
    return marks


def main(ctx: AccountCtx, digest: Digest):
    """每日快照 + 保證金警示。所有設定從 ctx 取得，thread-safe。

    警示加進 digest（排程執行殼三帳戶合併一則發送）。
    """
    dest = {"token": ctx.tg_token, "chat_id": ctx.tg_chat_id}
    acct_name = ctx.name

    log.info(f"QuantCore daily report start — {date.today()}")
    # 直接向 Broker 要連線（不走 ib_fetcher.connect）：timeout 與這行 log 和 #185 之前相同
    conn = broker.Broker().connect(ctx, timeout=20)
    log.info(f"Connected  account={ctx.account}")

    try:
        acct       = conn.account_info()
        nav        = acct['nav']
        cushion    = acct['cushion_pct']
        excess_liq = acct['excess_liq']
        pos_detail = conn.positions_detail()
        from symbol_contract import canonical_symbol
        symbols    = [canonical_symbol(p.contract.symbol) for p in pos_detail]
        # 標價用 Reference Close（最近官方收盤，CSV index[-1]）；IB 僅在 CSV 缺檔
        # 時 fallback——避免週末/盤前 IB 回傳陳舊 tick 蓋過正確收盤（ADR-0003）。
        prices     = mark_at_reference_close(conn, symbols) if symbols else {}

        log.info(f"NAV = ${nav:,.2f}  Cushion = {cushion:.1f}%  ExcessLiq = ${excess_liq:,.0f}")

        alert = cushion_alert(nav, cushion, excess_liq)
        if alert is not None:
            digest.add(acct_name, *alert, **dest)
            log.warning(f"Margin cushion {cushion:.1f}% < {CUSHION_ALERT_PCT:.0f}% "
                        f"— Telegram alert queued")

        append_daily_snapshot(nav, pos_detail, prices,
                              cushion_pct=cushion, excess_liq=excess_liq,
                              account_name=acct_name,
                              strategy_alloc=(ctx.alloc_sr, ctx.alloc_skewls))
        log.info(f"Excel snapshot written -> portfolio_tracker.xlsx")

    except Exception as e:
        log.error(f"ERROR: {e}", exc_info=True)
        digest.add(acct_name, "❌ 每日報告失敗", [error_line(e)], "error", **dest)
        raise

    finally:
        conn.close()
        log.info("Disconnected.")


if __name__ == '__main__':
    sys.exit(scheduled_run.run_job(run_status.DAILY_REPORT_JOB, main))
