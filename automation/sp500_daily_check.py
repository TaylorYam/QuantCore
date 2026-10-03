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

import argparse
import html
import logging
import sys
from pathlib import Path
from datetime import date, datetime

BASE_DIR = Path(__file__).parent
ROOT_DIR = BASE_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

LOG_DIR = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

from log_setup import setup_logging
import market_calendar
from notify import Digest, order_group_lines   # 三帳戶合併一則（#26）
log = logging.getLogger(__name__)


def _prev_trading_day(d):
    """Most recent NYSE trading day strictly before date ``d``."""
    from datetime import timedelta
    cur = d - timedelta(days=1)
    for _ in range(10):
        if market_calendar.is_nyse_trading_day(cur):
            return cur
        cur -= timedelta(days=1)
    return cur


def run_daily_check(account_name: str, digest: Digest):
    """單一帳戶的每日檢查。S6：account_name 為必填，ctx 一律建立。

    Telegram 內容加進 digest，由 run_all() 三帳戶合併發送（#26）。
    """
    import rebalance_ib as reb     # 仍需要：place_orders, check_fills
    import ib_fetcher               # S8：IB 連線/查詢 helpers
    import manual_attribution       # #106：手動持倉暫停閘門
    from strategy_owner import SECTOR_ETFS   # #187：類股 ETF 清單唯一來源
    from symbol_contract import canonical_symbol
    from account_ctx import AccountCtx
    from account_utils import ROLE_MOO_CHECK
    from sp500_state import (load_state, save_state, update_bull_counter,
                             mark_dip_triggered, is_dip_triggered)
    # S7：config / utils 是資料載入 helper（不是策略 business logic），可保留
    from strategies.sp500_momentum import config as sp_cfg, utils
    # S7：MarketRegime / SelectionEngine 走 signals 統一入口
    from signals import get_market_regime_state

    ctx = AccountCtx.from_name(account_name, ROLE_MOO_CHECK)
    tag = f"[{account_name}]"

    log.info(f"{'='*60}")
    log.info(f"  {tag} SP500 Daily Check  {date.today()}")
    log.info(f"{'='*60}")

    tg = {"token": ctx.tg_token, "chat_id": ctx.tg_chat_id}

    # ── 載入市場資料（SPY df 用於取 trading day index，非 strategy logic）
    spy_path = sp_cfg.ETF_DIR / "SPY.csv"
    spy_df = utils.load_benchmark(spy_path)

    # load_benchmark deliberately returns None for a missing, empty, or
    # malformed CSV.  This is a hard precondition for the daily risk check:
    # fail closed with an actionable alert before any len/index access.
    if spy_df is None:
        log.error(f"SPY benchmark unavailable: {spy_path}")
        digest.add(account_name, "❌ 日度風控失敗",
                   ["SPY 資料不可用（檔案不存在或格式錯誤），本次沒有做 dip-buy 檢查"],
                   "error", **tg)
        return

    if len(spy_df) < 2:
        log.error("SPY data too short")
        digest.add(account_name, "❌ 日度風控失敗",
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
    expected_ref = _prev_trading_day(today_nyse)
    if ref_date.date() != expected_ref:
        log.error(f"  STALE DATA: SPY last bar {ref_date.date()} != expected "
                  f"Reference Close {expected_ref}; aborting daily risk check")
        digest.add(account_name, "⚠️ 日度風控中止",
                   [f"資料錯位（SPY 最後 {ref_date.date()}，預期 {expected_ref}）。"
                    f"本次未做 dip-buy 檢查，請人工確認資料管線。"],
                   "warn", **tg)
        return

    mkt = get_market_regime_state(ref_date)
    if not mkt:
        log.error("No market regime data")
        digest.add(account_name, "❌ 日度風控失敗",
                   ["讀不到市場狀態資料（SPY / MA200），本次沒有做 dip-buy 檢查"],
                   "error", **tg)
        return

    spy_above = mkt["SPY_Close"] > mkt["SPY_MA200"]
    spy_dd    = abs(mkt["SPY_DD"])
    log.info(f"  SPY=${mkt['SPY_Close']:.2f}  MA200=${mkt['SPY_MA200']:.2f}  "
             f"DD={spy_dd*100:.1f}%  {'BULL' if spy_above else 'BEAR'}")

    # ── 連線 IB ────────────────────────────────────────────────────
    try:
        ib = ib_fetcher.connect(ctx)
    except Exception as e:
        log.error(f"IB connect failed: {e}")
        digest.add(account_name, "❌ IB 連線失敗", [html.escape(str(e)[:300])],
                   "error", **tg)
        return

    try:
        # 取得當前持倉
        current_pos = ib_fetcher.get_positions(ib, ctx)
        log.info(f"  Current positions: {current_pos or '(none)'}")

        if not current_pos:
            log.info("  No positions to check")
            return

        # 分辨 SP500 持倉（排除 SR ETFs）— S7 走 signals 單一入口
        from signals import get_sp500_universe
        sp500_universe = get_sp500_universe()

        sp500_holdings = [s for s in current_pos
                          if s not in SECTOR_ETFS and s in sp500_universe]
        log.info(f"  SP500 holdings: {sp500_holdings or '(none)'}")

        # 取得報價（包括持倉 + 潛在 dip-buy 標的）
        quote_syms = list(set(sp500_holdings + [sp_cfg.DIP_BUY_TICKER]))
        prices = ib_fetcher.get_prices(ib, quote_syms, ctx) if quote_syms else {}

        # 取得持倉成本
        pos_detail = ib_fetcher.get_positions_detail(ib, ctx)
        avg_costs = {canonical_symbol(p.contract.symbol): float(p.avgCost)
                     for p in pos_detail}

        # 停損 / 跳空出場已移除（ADR-0004）：個股出場改由週三 rotation 處理。
        # 本腳本只剩下方的熊市 dip-buy 檢查。
        orders = []

        # ── Step 3: 熊市 dip-buy 每日檢查 ──────────────────────────
        state = load_state(account_name)
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
                nav = ib_fetcher.get_nav(ib, ctx)
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

        # 取消該帳戶既有的掛單（避免新單與舊單衝突；Panel 手動單除外，#106）
        open_trades = manual_attribution.strategy_open_trades(ib.openTrades())
        if open_trades:
            log.info(f"  Cancelling {len(open_trades)} existing open orders...")
            for t in open_trades:
                ib.cancelOrder(t.order)
            ib.sleep(2)

        log.info(f"  Placing {len(orders)} LMT orders...")

        # 取得即時報價以計算 LMT 價格
        from symbol_contract import ib_stock
        import numpy as np
        from price_utils import robust_get_prices
        order_syms = list({o['symbol'] for o in orders})

        # 先抓本連線的 bid/ask（用於 LMT 精準 buffer）
        contracts = [ib_stock(s) for s in order_syms]
        ib.qualifyContracts(*contracts)
        tickers = ib.reqTickers(*contracts)
        quotes = {}
        for t in tickers:
            sym = canonical_symbol(t.contract.symbol)
            bid = t.bid if (t.bid and t.bid > 0 and np.isfinite(t.bid)) else None
            ask = t.ask if (t.ask and t.ask > 0 and np.isfinite(t.ask)) else None
            if bid and ask:
                if bid > ask:
                    log.warning(f"  {sym}: crossed quote bid={bid} > ask={ask}")
                    bid = ask = None
                elif (ask - bid) / ((ask + bid) / 2) > 0.05:
                    log.warning(f"  {sym}: wide spread bid={bid} ask={ask}")
            quotes[sym] = {'bid': bid, 'ask': ask}

        # 對於缺 bid/ask 的 symbol，用 robust_get_prices fallback 到 feed/CSV
        missing = [s for s in order_syms
                   if not (quotes[s].get('bid') and quotes[s].get('ask'))]
        if missing:
            fallback = robust_get_prices(
                ib, missing,
                host=ctx.host,
                price_feed_port=ctx.price_feed_port,
                verbose_log=lambda m: log.info(m),
            )
            for sym in missing:
                p = fallback.get(sym, 0)
                if p > 0 and not quotes[sym].get('bid'):
                    quotes[sym]['ref'] = p

        # 為本段下單建一個 LMT+outsideRTH 的臨時 ctx（frozen，不汙染原 ctx）
        from dataclasses import replace as _replace
        place_ctx = _replace(ctx, order_type='LMT', outside_rth=True)

        ib_orders = []
        for o in orders:
            sym = o['symbol']
            q = quotes.get(sym, {})
            if o['action'] == 'BUY':
                base = q.get('ask') or q.get('ref') or 0
                lmt = round(base * 1.003, 2) if base > 0 else 0
            else:
                base = q.get('bid') or q.get('ref') or 0
                lmt = round(base * 0.997, 2) if base > 0 else 0

            if lmt <= 0:
                log.warning(f"    {sym}: no price, skipping")
                continue

            ib_orders.append({
                'symbol': sym, 'action': o['action'], 'qty': o['qty'],
                'lmt_price': lmt, 'strategy': 'SP500-daily',
            })
            log.info(f"    LMT {o['action']} {o['qty']}sh {sym} @ ${lmt:.2f}  ({o['reason']})")

        if ib_orders and not ctx.dry_run:
            placed, trades = reb.place_orders(ib, ib_orders, ctx=place_ctx)
            fill_results = reb.check_fills(trades) if trades else []

            # Telegram 通知（main() 合併發送）
            if placed:
                digest.note(f"SPY {mkt['SPY_Close']:.2f} · DD {spy_dd*100:.1f}%")
                digest.add(account_name, f"熊市 dip-buy 送出 {len(placed)} 筆 LMT",
                           order_group_lines(placed, show_price=True), **tg)
        elif ctx.dry_run:
            log.info("  [DRY RUN] orders not sent")

    finally:
        ib.disconnect()
        log.info("Disconnected")


def run_all(names: list[str], digest: Digest) -> None:
    """依序檢查多個帳戶，合成一則發送。某帳戶出例外 → ❌ 段落，繼續下一個。"""
    from account_utils import telegram_dest
    try:
        for name in names:
            log.info(f"\n\n{'*'*60}\n  Account: {name}\n{'*'*60}")
            try:
                run_daily_check(name, digest)
            except Exception as e:
                log.error(f"  [{name}] daily check failed: {e}", exc_info=True)
                digest.add(name, "❌ 日度風控失敗", [html.escape(str(e)[:300])],
                           "error", **telegram_dest(name))
    finally:
        digest.send()


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="SP500 Daily Check")
    parser.add_argument("--account", required=True,
                        help="帳戶名稱（Taylor/Rosa/Yvonne），或 'all' = 全部帳戶")
    args = parser.parse_args(argv)

    from account_utils import load_accounts
    import run_status

    all_accounts = args.account.lower() == "all"
    names: list[str] = []
    digest = Digest(f"日度風控 {date.today()}", icon="📤")
    # 當日排程總結用的執行紀錄（#38）：只記排程的 --account all
    with run_status.recording("daily_check", lambda: digest.outcomes(names),
                              enabled=all_accounts):
        names.extend([a["name"] for a in load_accounts()] if all_accounts
                     else [args.account])
        run_all(names, digest)


if __name__ == '__main__':
    setup_logging("daily_check")
    main()
