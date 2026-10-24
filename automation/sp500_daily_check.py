#!/usr/bin/env python3
"""
SP500 Momentum — 每日檢查腳本

每個美股交易日執行：熊市 dip-buy 觸發（每日檢查 SPY 回撤 DD）。

註：固定 20% 停損與 50% 跳空出場已於 ADR-0004 移除（8 年回測零貢獻、
跳空從未觸發）；個股出場改由週三的 EMA50/排名 rotation 處理。

Live 模式下這個腳本每個美股交易日執行一次（建議美東 04:00 AM = 台灣 16:00），
若觸發任何動作則立即送 LMT 單（outsideRth=True）。

使用方式：
  python sp500_daily_check.py --account Taylor
  python sp500_daily_check.py --account all     # 所有帳戶

注意：
  - 此腳本現在只處理熊市 dip-buy 觸發
  - 個股出場（含原停損/跳空）與 rotation 輪動皆由 rebalance_ib.py 週三執行
  - 不使用 MOO 單（盤外用 LMT）

Telegram：dip-buy 送單、資料錯位中止、失敗（讀不到資料、連不上 IB、例外）
才發，三帳戶合成一則。失敗以前只寫 log（#38）。
"""

import logging
import sys
from pathlib import Path
from datetime import date

BASE_DIR = Path(__file__).parent
import _repo_root  # noqa: E402,F401  QuantCore root 可 import strategies

LOG_DIR = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

import market_calendar
from notify import Digest, error_line, order_group_lines   # 三帳戶合併一則（#26）
import run_status
import scheduled_run                            # #206：排程執行殼
from account_ctx import AccountCtx
from account_utils import telegram_dest       # #207：Telegram 目的地唯一來源
import strategy_orders                          # #205：策略單送出（撤舊單、限價、送單、成交）
log = logging.getLogger(__name__)


class _DipBuySend(strategy_orders.Narrator):
    """dip-buy 送單的進度 log（文字與 #205 之前逐字相同）。"""

    def placing(self, count: int) -> None:
        log.info(f"  Placing {count} LMT orders...")

    def priced(self, src: dict, lmt: float) -> None:
        log.info(f"    LMT {src['action']} {src['qty']}sh {src['symbol']} @ ${lmt:.2f}"
                 f"  ({src['reason']})")


def run_daily_check(ctx: AccountCtx, digest: Digest, source=None):
    """單一帳戶的每日檢查（ctx 由排程執行殼以 ROLE_MOO_CHECK 建立）。

    Telegram 內容加進 digest，由排程執行殼三帳戶合併發送（#26）。
    source：市場狀態與 [[Universe]] 的 signals.StrategySource；None ＝ LiveSource。
    """
    import broker                   # #185：經 Broker 開連線（讀數、送單在 Connection 上）
    import manual_attribution       # #106：手動持倉暫停閘門
    from strategy_owner import SECTOR_ETFS   # #187：類股 ETF 清單唯一來源
    from symbol_contract import canonical_symbol
    from account_state import MANUAL_FIX, StateCorrupted
    from sp500_state import (load_state, save_state,
                             mark_dip_triggered, is_dip_triggered)
    # S7：config / utils 是資料載入 helper（不是策略 business logic），可保留
    from strategies.sp500_momentum import config as sp_cfg, utils
    # MarketRegime / SelectionEngine 走訊號來源（#232）
    if source is None:
        import signals
        source = signals.LiveSource()

    account_name = ctx.name
    tag = f"[{account_name}]"

    log.info(f"{'='*60}")
    log.info(f"  {tag} SP500 Daily Check  {date.today()}")
    log.info(f"{'='*60}")

    tg = telegram_dest(ctx.name)

    # ── 載入市場資料（SPY df 用於取 trading day index，非 strategy logic）
    spy_path = sp_cfg.ETF_DIR / "SPY.csv"
    spy_df = utils.load_benchmark(spy_path)

    # load_benchmark deliberately returns None for a missing, empty, or
    # malformed CSV.  This is a hard precondition for the daily risk check:
    # fail closed with an actionable alert before any len/index access.
    if spy_df is None:
        log.error(f"SPY benchmark unavailable: {spy_path}")
        digest.add(account_name, run_status.DAILY_CHECK_JOB.failed,
                   ["SPY 資料不可用（檔案不存在或格式錯誤），本次沒有做 dip-buy 檢查"],
                   "error", **tg)
        return

    if len(spy_df) < 2:
        log.error("SPY data too short")
        digest.add(account_name, run_status.DAILY_CHECK_JOB.failed,
                   ["SPY 資料不足，本次沒有做 dip-buy 檢查"], "error", **tg)
        return

    # Reference Close = most recent completed session = data index[-1] (ADR-0003).
    # The Execution Session (today's live open) is NOT in the data — update_data
    # writes only completed sessions, after the close.
    ref_date   = spy_df.index[-1]
    prior_date = spy_df.index[-2] if len(spy_df) >= 2 else None

    # Holiday skip: no Execution Session today → nothing to send orders into.
    today_nyse = date.today()   # 16:00 TW run == 04:00 ET → local date == ET date
    if not market_calendar.is_nyse_trading_day(today_nyse):
        log.info(f"  {today_nyse} is not an NYSE trading day — daily risk check no-op")
        return

    # Staleness guard: Reference Close must be the most recent trading day strictly
    # before today. If the data pipeline lagged/failed, abort rather than act on a
    # stale close (ADR-0003: halt + alert, never trade on bad data).
    expected_ref = market_calendar.previous_session(today_nyse)
    if ref_date.date() != expected_ref:
        log.error(f"  STALE DATA: SPY last bar {ref_date.date()} != expected "
                  f"Reference Close {expected_ref}; aborting daily risk check")
        digest.add(account_name, "⚠️ 日度風控中止",
                   [f"資料錯位（SPY 最後 {ref_date.date()}，預期 {expected_ref}）。"
                    f"本次未做 dip-buy 檢查，請人工確認資料管線。"],
                   "warn", **tg)
        return

    mkt = source.market_regime(ref_date)
    if not mkt:
        log.error("No market regime data")
        digest.add(account_name, run_status.DAILY_CHECK_JOB.failed,
                   ["讀不到市場狀態資料（SPY / MA200），本次沒有做 dip-buy 檢查"],
                   "error", **tg)
        return

    spy_above = mkt["SPY_Close"] > mkt["SPY_MA200"]
    spy_dd    = abs(mkt["SPY_DD"])
    log.info(f"  SPY=${mkt['SPY_Close']:.2f}  MA200=${mkt['SPY_MA200']:.2f}  "
             f"DD={spy_dd*100:.1f}%  {'BULL' if spy_above else 'BEAR'}")

    # ── 連線 IB ────────────────────────────────────────────────────
    try:
        conn = broker.Broker().connect(ctx, timeout=20)
        log.info(broker.connected_line(ctx))
    except Exception as e:
        log.error(f"IB connect failed: {e}")
        digest.add(account_name, "❌ IB 連線失敗", [error_line(e)], "error", **tg)
        return

    try:
        # 取得當前持倉
        current_pos = conn.positions()
        log.info(f"  Current positions: {current_pos or '(none)'}")

        if not current_pos:
            log.info("  No positions to check")
            return

        # 分辨 SP500 持倉（排除 SR ETFs）
        sp500_universe = source.sp500_universe()

        sp500_holdings = [s for s in current_pos
                          if s not in SECTOR_ETFS and s in sp500_universe]
        log.info(f"  SP500 holdings: {sp500_holdings or '(none)'}")

        # 取得報價（包括持倉 + 潛在 dip-buy 標的）
        quote_syms = list(set(sp500_holdings + [sp_cfg.DIP_BUY_TICKER]))
        prices = conn.prices(quote_syms) if quote_syms else {}

        # 取得持倉成本
        pos_detail = conn.positions_detail()
        avg_costs = {canonical_symbol(p.contract.symbol): float(p.avgCost)
                     for p in pos_detail}

        # 停損 / 跳空出場已移除（ADR-0004）：個股出場改由週三 rotation 處理。
        # 本腳本只剩下方的熊市 dip-buy 檢查。
        orders = []

        # ── Step 3: 熊市 dip-buy 每日檢查 ──────────────────────────
        try:
            state = load_state(account_name)
        except StateCorrupted as e:
            # #233：不從預設值重來（會重複觸發已買過的 dip 階層），停下等人工處理
            digest.add(account_name, "⚠️ 日度風控中止",
                       [f"SP500 state 讀不出來：{e.where()}。本次沒有做 dip-buy 檢查；"
                        f"{MANUAL_FIX}"],
                       "warn", **tg)
            return
        ALLOC_MAP = sp_cfg.DIP_BUY_ALLOCATIONS

        # 非 rebal day：只觸發新的 dip 階層，不改 bull_counter
        if not spy_above and sp_cfg.ENABLE_BEAR_DIP_BUY:
            newly_triggered = []
            for level in (0.15, 0.20, 0.25):
                if spy_dd >= level and not is_dip_triggered(state, level):
                    log.info(f"  DIP-BUY trigger: -{level*100:.0f}% (DD={spy_dd*100:.1f}%)")
                    state = mark_dip_triggered(state, level)
                    newly_triggered.append((level, ALLOC_MAP[level]))

            if newly_triggered:
                # 計算 dip-buy 股數：alloc × NAV × alloc_sp500 / price
                nav = conn.nav()
                dip_ticker = sp_cfg.DIP_BUY_TICKER
                dip_price = prices.get(dip_ticker, 0)
                if dip_price > 0:
                    total_new_alloc = sum(a for _, a in newly_triggered)
                    target_val = total_new_alloc * nav * ctx.alloc_sp500
                    # round 同 rebalance_ib SP500 sizing(frac_sizing_report.md)
                    qty = int(round(target_val / dip_price))
                    if qty > 0:
                        log.info(f"  NEW dip-buy: {qty}sh {dip_ticker} "
                                 f"(~${qty*dip_price:,.0f}, {total_new_alloc*100:.0f}% of SP500 alloc)")
                        orders.append({
                            'symbol': dip_ticker, 'action': 'BUY', 'qty': qty,
                            'reason': f'Bear dip-buy ({total_new_alloc*100:.0f}%)',
                        })

        state["last_check_date"] = str(date.today())
        # 手動持倉暫停（#106）：存 state 前先過送單閘門。讀不到暫停紀錄時
        # 拋例外 = 本帳戶不送單、dip 觸發不消耗（state 不存），隔天重試
        if orders:
            orders, _held = manual_attribution.gate_orders(account_name, orders)
        save_state(account_name, state)

        # ── Step 4: 執行訂單（LMT + outsideRth）───────────────────────
        if not orders:
            log.info("  No daily-check actions needed")
            return

        # 訂單去重：同一 symbol 只保留第一筆（stop/gap 優先於 dip-buy）
        seen = set()
        dedup_orders = []
        for o in orders:
            if o['symbol'] in seen:
                log.warning(f"  duplicate order for {o['symbol']}, keeping first")
                continue
            seen.add(o['symbol'])
            dedup_orders.append(o)
        orders = dedup_orders

        # 送單（strategy_orders，#205）：撤該帳戶既有策略掛單（Panel 手動單除外，
        # dry run 不撤）→ Broker 的送單報價＋統一限價規則（報價全缺才向參考價要價）
        # → LMT＋outsideRth 送出 → 等成交
        from dataclasses import replace as _replace
        place_ctx = _replace(ctx, order_type='LMT', outside_rth=True)
        sent = strategy_orders.send(
            conn,
            [{'symbol': o['symbol'], 'action': o['action'], 'qty': o['qty'],
              'strategy': 'SP500-daily', 'reason': o['reason']} for o in orders],
            place_ctx, narrator=_DipBuySend())

        if ctx.dry_run:
            log.info("  [DRY RUN] orders not sent")
        elif sent.placed:
            # Telegram 通知（排程執行殼三帳戶合併發送）
            digest.note(f"SPY {mkt['SPY_Close']:.2f} · DD {spy_dd*100:.1f}%")
            digest.add(account_name, f"熊市 dip-buy 送出 {len(sent.placed)} 筆 LMT",
                       order_group_lines(sent.placed, show_price=True), **tg)

    finally:
        conn.close()
        log.info("Disconnected")


def run(argv: list[str] | None = None) -> int:
    """排程入口：三帳戶共用一個訊號來源（SPY／SSO 與成分股只讀一次，#232）。"""
    import signals
    source = signals.LiveSource()
    return scheduled_run.run_job(
        run_status.DAILY_CHECK_JOB,
        lambda ctx, digest: run_daily_check(ctx, digest, source=source), argv=argv)


if __name__ == '__main__':
    sys.exit(run())
