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

log_file = LOG_DIR / f"daily_check_{date.today().strftime('%Y%m%d')}.log"
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


def run_daily_check(account_name: str = ""):
    """單一帳戶的每日檢查"""
    import rebalance_ib as reb
    from account_utils import get_account_config
    from sp500_state import (load_state, save_state, update_bull_counter,
                             mark_dip_triggered, is_dip_triggered)
    from strategies.sp500_momentum import config as sp_cfg, utils
    from strategies.sp500_momentum.market_regime import MarketRegime

    if account_name:
        cfg = get_account_config(account_name)
        reb._apply_config(cfg)
        tag = f"[{account_name}]"
    else:
        tag = ""

    log.info(f"{'='*60}")
    log.info(f"  {tag} SP500 Daily Check  {date.today()}")
    log.info(f"{'='*60}")

    # ── 載入市場資料 ────────────────────────────────────────────────
    spy_df = utils.load_benchmark(sp_cfg.ETF_DIR / "SPY.csv")
    sso_df = utils.load_benchmark(sp_cfg.ETF_DIR / "SSO.csv")
    regime = MarketRegime(spy_df, sso_df)

    if len(spy_df) < 2:
        log.error("SPY data too short")
        return

    today_idx   = spy_df.index[-1]
    prev_idx    = spy_df.index[-2]
    day_before  = spy_df.index[-3] if len(spy_df) >= 3 else None

    mkt = regime.get_state(today_idx)
    if not mkt:
        log.error("No market regime data")
        return

    spy_above = mkt["SPY_Close"] > mkt["SPY_MA200"]
    spy_dd    = abs(mkt["SPY_DD"])
    log.info(f"  SPY=${mkt['SPY_Close']:.2f}  MA200=${mkt['SPY_MA200']:.2f}  "
             f"DD={spy_dd*100:.1f}%  {'BULL' if spy_above else 'BEAR'}")

    # ── 連線 IB ────────────────────────────────────────────────────
    try:
        ib = reb.connect_ib()
    except Exception as e:
        log.error(f"IB connect failed: {e}")
        return

    try:
        # 取得當前持倉
        current_pos = reb.get_positions(ib)
        log.info(f"  Current positions: {current_pos or '(none)'}")

        if not current_pos:
            log.info("  No positions to check")
            return

        # 分辨 SP500 持倉（排除 SR ETFs）
        from strategies.sp500_momentum.selection import SelectionEngine
        selector = SelectionEngine()
        sp500_universe = set(selector.get_constituents(spy_df.index[-1]))
        sp500_universe |= {'SPY', 'SSO'}

        sp500_holdings = [s for s in current_pos
                          if s not in reb.SR_ETFS and s in sp500_universe]
        log.info(f"  SP500 holdings: {sp500_holdings or '(none)'}")

        # 取得報價（包括持倉 + 潛在 dip-buy 標的）
        quote_syms = list(set(sp500_holdings + [sp_cfg.DIP_BUY_TICKER]))
        prices = reb.get_prices(ib, quote_syms) if quote_syms else {}

        # 取得持倉成本
        pos_detail = reb.get_positions_detail(ib)
        avg_costs = {p.contract.symbol: float(p.avgCost) for p in pos_detail}

        # ── Step 1: 停損檢查 ───────────────────────────────────────
        orders = []
        dip_tickers = {sp_cfg.DIP_BUY_TICKER, 'SPY', 'SSO'}

        for ticker in sp500_holdings:
            if ticker in dip_tickers:
                continue  # dip 部位不檢查停損
            avg_cost = avg_costs.get(ticker, 0)
            if avg_cost <= 0:
                continue
            try:
                prev_close = float(selector._get_ticker_data(ticker).loc[prev_idx, 'Close'])
            except Exception:
                log.warning(f"    {ticker}: no prev close data")
                continue
            if prev_close < avg_cost * (1 - sp_cfg.STOP_LOSS_PCT):
                qty = int(current_pos[ticker])
                log.info(f"  STOP LOSS {ticker}: prev_close=${prev_close:.2f} "
                         f"< cost*0.8=${avg_cost*0.8:.2f}")
                orders.append({
                    'symbol': ticker, 'action': 'SELL', 'qty': qty,
                    'reason': f'Stop loss ({prev_close:.2f}<{avg_cost:.2f}*0.8)',
                })

        # ── Step 2: 跳空出場檢查 ───────────────────────────────────
        if day_before is not None:
            for ticker in sp500_holdings:
                if ticker in dip_tickers:
                    continue
                try:
                    df = selector._get_ticker_data(ticker)
                    close_before = float(df.loc[day_before, 'Close'])
                    open_prev = float(df.loc[prev_idx, 'Open']) if 'Open' in df.columns else None
                    if not open_prev or close_before <= 0:
                        continue
                    gap = (open_prev - close_before) / close_before
                    if abs(gap) >= sp_cfg.GAP_EXIT_PCT:
                        qty = int(current_pos[ticker])
                        direction = "Up" if gap > 0 else "Down"
                        log.info(f"  GAP EXIT {ticker}: gap={gap*100:+.1f}% {direction}")
                        orders.append({
                            'symbol': ticker, 'action': 'SELL', 'qty': qty,
                            'reason': f'Gap exit ({gap*100:+.1f}%)',
                        })
                except Exception as e:
                    log.warning(f"    {ticker}: gap check failed: {e}")

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
                # 計算 dip-buy 股數：alloc × NAV × ALLOC_SP500 / price
                nav = reb.get_nav(ib)
                dip_ticker = sp_cfg.DIP_BUY_TICKER
                dip_price = prices.get(dip_ticker, 0)
                if dip_price > 0:
                    total_new_alloc = sum(a for _, a in newly_triggered)
                    # 按 SP500 配額比例
                    target_val = total_new_alloc * nav * reb.ALLOC_SP500
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
                host=reb.HOST,
                price_feed_port=reb.PRICE_FEED_PORT,
                verbose_log=lambda m: log.info(m),
            )
            for sym in missing:
                p = fallback.get(sym, 0)
                if p > 0 and not quotes[sym].get('bid'):
                    quotes[sym]['ref'] = p

        reb.ORDER_TYPE = 'LMT'
        reb.OUTSIDE_RTH = True

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

        if ib_orders and not reb.DRY_RUN:
            placed, trades = reb.place_orders(ib, ib_orders)
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
            reb.tg_send("\n".join(lines))
        elif reb.DRY_RUN:
            log.info("  [DRY RUN] orders not sent")

    finally:
        ib.disconnect()
        log.info("Disconnected")


def main():
    parser = argparse.ArgumentParser(description="SP500 Daily Check")
    parser.add_argument("--account", default="",
                        help="帳戶名稱，all = 全部帳戶")
    args = parser.parse_args()

    if args.account.lower() == "all":
        from account_utils import load_accounts
        accounts = [a["name"] for a in load_accounts()]
        for name in accounts:
            log.info(f"\n\n{'*'*60}\n  Account: {name}\n{'*'*60}")
            try:
                run_daily_check(name)
            except Exception as e:
                log.error(f"  [{name}] daily check failed: {e}", exc_info=True)
    else:
        run_daily_check(args.account)


if __name__ == '__main__':
    main()
