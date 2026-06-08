#!/usr/bin/env python3
"""
SP500 Momentum — 每日檢查腳本

回測 backtester 在每個交易日執行下列檢查（非只有 rebalance day）：
  1. _check_stop_loss:   前收盤 < 成本 × (1 - STOP_LOSS_PCT) → 隔日開盤出場
  2. _check_gap_exit:    abs(gap) ≥ GAP_EXIT_PCT → 隔日開盤出場
  3. _check_bear_sso_logic: 熊市 dip-buy 觸發（每日檢查 SPY DD）

Live 模式下這個腳本每個美股交易日執行一次（建議美東 04:00 AM = 台灣 16:00），
若觸發任何動作則立即送 LMT 單（outsideRth=True）。

使用方式：
  python sp500_daily_check.py --account Taylor
  python sp500_daily_check.py --account all     # 所有帳戶

注意：
  - 此腳本只處理 stop loss / gap exit / dip-buy 觸發
  - 完整的 rotation 輪動由 rebalance_ib.py 週三執行
  - 不使用 MOO 單（盤外用 LMT）
"""

import argparse
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


def evaluate_exits(sp500_holdings, positions, avg_costs, histories,
                   ref_date, prior_date, *, stop_loss_pct, gap_exit_pct,
                   skip_tickers=()):
    """Pure stop-loss / gap-exit decision for the Reference Close session.

    Reference Close (= most recent completed session = data ``index[-1]``) is
    the close we evaluate; the resulting order executes at the next open — the
    live Execution Session, which is NOT in the data because ``update_data``
    only writes completed sessions after the close. See ADR-0003.

    Parameters
    ----------
    histories  : callable(ticker) -> DataFrame (Open/Close indexed by date)
    ref_date   : the Reference Close session
    prior_date : the session before ref_date (gap baseline), or None

    Returns ``(orders, missing)`` — ``missing`` lists held tickers with no
    ``ref_date`` row; the caller must alert (those positions go unchecked).
    """
    orders, missing = [], []
    skip = set(skip_tickers)
    for ticker in sp500_holdings:
        if ticker in skip:
            continue
        avg_cost = avg_costs.get(ticker, 0)
        if avg_cost <= 0:
            continue
        df = histories(ticker)
        if df is None or ref_date not in df.index:
            missing.append(ticker)
            continue
        qty = int(positions.get(ticker, 0))
        if qty == 0:
            continue
        ref_close = float(df.loc[ref_date, 'Close'])
        # Stop loss: Reference Close below cost threshold
        if ref_close < avg_cost * (1 - stop_loss_pct):
            orders.append({
                'symbol': ticker, 'action': 'SELL', 'qty': qty,
                'reason': f'Stop loss ({ref_close:.2f}<{avg_cost:.2f}*{1 - stop_loss_pct:.2f})',
            })
            continue  # already exiting — don't double up with a gap exit
        # Gap exit: Reference Close session opened with a gap vs its prior close
        if prior_date is not None and prior_date in df.index and 'Open' in df.columns:
            prior_close = float(df.loc[prior_date, 'Close'])
            ref_open = float(df.loc[ref_date, 'Open'])
            if prior_close > 0 and ref_open > 0:
                gap = (ref_open - prior_close) / prior_close
                if abs(gap) >= gap_exit_pct:
                    direction = 'Up' if gap > 0 else 'Down'
                    orders.append({
                        'symbol': ticker, 'action': 'SELL', 'qty': qty,
                        'reason': f'Gap exit ({gap * 100:+.1f}% {direction})',
                    })
    return orders, missing


def run_daily_check(account_name: str):
    """單一帳戶的每日檢查。S6：account_name 為必填，ctx 一律建立。"""
    import rebalance_ib as reb     # 仍需要：place_orders, check_fills
    import ib_fetcher               # S8：IB 連線/查詢 helpers
    from account_ctx import AccountCtx
    from account_utils import ROLE_MOO_CHECK
    from sp500_state import (load_state, save_state, update_bull_counter,
                             mark_dip_triggered, is_dip_triggered)
    # S7：config / utils 是資料載入 helper（不是策略 business logic），可保留
    from strategies.sp500_momentum import config as sp_cfg, utils
    # S7：MarketRegime / SelectionEngine 走 signals 統一入口
    from signals import get_market_regime_state, get_ticker_history

    ctx = AccountCtx.from_name(account_name, ROLE_MOO_CHECK)
    tag = f"[{account_name}]"

    log.info(f"{'='*60}")
    log.info(f"  {tag} SP500 Daily Check  {date.today()}")
    log.info(f"{'='*60}")

    # ── 載入市場資料（SPY df 用於取 trading day index，非 strategy logic）
    spy_df = utils.load_benchmark(sp_cfg.ETF_DIR / "SPY.csv")

    if len(spy_df) < 2:
        log.error("SPY data too short")
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
        try:
            from notify import tg_send as _tg
            _tg(f"⚠️ {tag} 日度風控中止：資料錯位（SPY 最後 {ref_date.date()}，"
                f"預期 {expected_ref}）。本次未檢查停損/跳空，請人工確認資料管線。",
                token=ctx.tg_token, chat_id=ctx.tg_chat_id)
        except Exception as e:
            log.warning(f"  alert send failed: {e}")
        return

    mkt = get_market_regime_state(ref_date)
    if not mkt:
        log.error("No market regime data")
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
                          if s not in ib_fetcher.SR_ETFS and s in sp500_universe]
        log.info(f"  SP500 holdings: {sp500_holdings or '(none)'}")

        # 取得報價（包括持倉 + 潛在 dip-buy 標的）
        quote_syms = list(set(sp500_holdings + [sp_cfg.DIP_BUY_TICKER]))
        prices = ib_fetcher.get_prices(ib, quote_syms, ctx) if quote_syms else {}

        # 取得持倉成本
        pos_detail = ib_fetcher.get_positions_detail(ib, ctx)
        avg_costs = {p.contract.symbol: float(p.avgCost) for p in pos_detail}

        # ── Step 1+2: 停損 / 跳空出場（純函式，讀 Reference Close = index[-1]）──
        dip_tickers = {sp_cfg.DIP_BUY_TICKER, 'SPY', 'SSO'}
        orders, missing = evaluate_exits(
            sp500_holdings, current_pos, avg_costs, get_ticker_history,
            ref_date, prior_date,
            stop_loss_pct=sp_cfg.STOP_LOSS_PCT,
            gap_exit_pct=sp_cfg.GAP_EXIT_PCT,
            skip_tickers=dip_tickers,
        )
        for o in orders:
            log.info(f"  {o['reason']}: SELL {o['qty']}sh {o['symbol']}")
        # 持倉缺 Reference Close 資料 = 該倉本次未受保護 → 告警（不再靜默跳過）
        if missing:
            log.warning(f"  UNPROTECTED (no {ref_date.date()} close): {missing}")
            try:
                from notify import tg_send as _tg
                _tg(f"⚠️ {tag} 日度風控：持倉 {missing} 缺 {ref_date.date()} 收盤資料，"
                    f"本次未檢查停損/跳空，請人工確認。",
                    token=ctx.tg_token, chat_id=ctx.tg_chat_id)
            except Exception as e:
                log.warning(f"  alert send failed: {e}")

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
                    qty = int(target_val / dip_price)
                    if qty > 0:
                        log.info(f"  NEW dip-buy: {qty}sh {dip_ticker} "
                                 f"(~${qty*dip_price:,.0f}, {total_new_alloc*100:.0f}% of SP500 alloc)")
                        orders.append({
                            'symbol': dip_ticker, 'action': 'BUY', 'qty': qty,
                            'reason': f'Bear dip-buy ({total_new_alloc*100:.0f}%)',
                        })

        state["last_check_date"] = str(date.today())
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

        # 取消該帳戶既有的掛單（避免新單與舊單衝突）
        open_trades = ib.openTrades()
        if open_trades:
            log.info(f"  Cancelling {len(open_trades)} existing open orders...")
            for t in open_trades:
                ib.cancelOrder(t.order)
            ib.sleep(2)

        log.info(f"  Placing {len(orders)} LMT orders...")

        # 取得即時報價以計算 LMT 價格
        from ib_insync import Stock as IBStock
        import numpy as np
        from price_utils import robust_get_prices
        order_syms = list({o['symbol'] for o in orders})

        # 先抓本連線的 bid/ask（用於 LMT 精準 buffer）
        contracts = [IBStock(s, 'SMART', 'USD') for s in order_syms]
        ib.qualifyContracts(*contracts)
        tickers = ib.reqTickers(*contracts)
        quotes = {}
        for t in tickers:
            sym = t.contract.symbol
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

            # Telegram 通知
            lines = [
                f"<b>{tag} SP500 Daily Check</b>",
                f"Date: {date.today()}",
                f"SPY=${mkt['SPY_Close']:.2f}  DD={spy_dd*100:.1f}%",
                "",
            ]
            for o in placed:
                lines.append(f"  {o['action']} {o['qty']}sh {o['symbol']} @ ${o['lmt_price']:.2f}")
            from notify import tg_send as _tg
            _tg("\n".join(lines), token=ctx.tg_token, chat_id=ctx.tg_chat_id)
        elif ctx.dry_run:
            log.info("  [DRY RUN] orders not sent")

    finally:
        ib.disconnect()
        log.info("Disconnected")


def main():
    parser = argparse.ArgumentParser(description="SP500 Daily Check")
    parser.add_argument("--account", required=True,
                        help="帳戶名稱（Taylor/Rosa/Yvonne），或 'all' = 全部帳戶")
    args = parser.parse_args()

    from account_utils import load_accounts
    if args.account.lower() == "all":
        names = [a["name"] for a in load_accounts()]
    else:
        names = [args.account]

    for name in names:
        log.info(f"\n\n{'*'*60}\n  Account: {name}\n{'*'*60}")
        try:
            run_daily_check(name)
        except Exception as e:
            log.error(f"  [{name}] daily check failed: {e}", exc_info=True)


if __name__ == '__main__':
    setup_logging("daily_check")
    main()
