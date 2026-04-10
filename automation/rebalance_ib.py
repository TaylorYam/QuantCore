#!/usr/bin/env python3
"""
QuantCore — IB Gateway 自動調倉腳本
80% Sector Rotation + 50% SP500 Momentum = 130% exposure (1.3x leverage)

兩個策略各自獨立排程，與回測一致：
  週二 21:00 → python rebalance_ib.py --mode sr      (Sector Rotation)
  週三 21:00 → python rebalance_ib.py --mode sp500   (SP500 Momentum)
  手動全部  → python rebalance_ib.py --mode both     (兩個一起)

使用方式：
  1. 修改 config.json（帳號、port、dry_run、order_type）
  2. python rebalance_ib.py --mode sr     # 只跑 SR
  3. python rebalance_ib.py --mode sp500  # 只跑 SP500
  4. python rebalance_ib.py --mode both   # 兩個都跑（預設）
  5. 確認 log 無誤後改 dry_run=false

多帳戶模式：
  python rebalance_ib.py --mode sr --account A     # 指定帳戶
  python rebalance_ib.py --mode sr --account all   # 所有帳戶循序執行

order_type 選項：
  "MKT"  - 市價單（盤中任何時間）
  "MOO"  - Market-on-Open，保證開盤價成交，須在 09:25 ET 前送出
  "LMT"  - 限價單（需搭配 outside_rth=true 可做盤前盤後）
"""

import argparse
import html as html_mod
import json, logging, time, sys, urllib.request, urllib.parse
from pathlib import Path
from datetime import date, datetime

import matplotlib
matplotlib.use('Agg')
import numpy as np
import pandas as pd

# ── 路徑設定 ──────────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent
ROOT_DIR  = BASE_DIR.parent          # C:/QuantCore-master
LOG_DIR   = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

# 加入 QuantCore root 以便 import strategies
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from excel_logger import append_trade_log

# ── 日誌設定 ──────────────────────────────────────────────────────────────────
log_file = LOG_DIR / f"rebalance_{date.today().strftime('%Y%m%d')}.log"
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

# ── IB 連線工具 ───────────────────────────────────────────────────────────────
from ib_insync import IB, Stock, MarketOrder, LimitOrder, Order, util
util.logToConsole(logging.WARNING)
logging.root.setLevel(logging.INFO)


# ── Telegram 通知（統一由 notify.py 提供）──────────────────────────────────
from notify import tg_send as _tg

# ── AccountCtx（S6：ctx 為所有函式的必要參數，模組不再有 mutable globals）
from account_ctx import AccountCtx

# ── IB 工具函式（S8：統一由 ib_fetcher 提供，這裡 re-export 給舊 callers）
from ib_fetcher import (
    connect as connect_ib,
    get_nav,
    get_positions,
    get_positions_detail,
    get_prices,
)


def make_order(action: str, qty: int, lmt_price: float,
               ctx: AccountCtx) -> Order:
    if ctx.order_type == 'MOO':
        o = Order()
        o.action        = action
        o.totalQuantity = qty
        o.orderType     = 'MKT'
        o.tif           = 'OPG'
        o.outsideRth    = False
    elif ctx.order_type == 'LMT':
        o = LimitOrder(action, qty, lmt_price)
        o.outsideRth = ctx.outside_rth
    else:  # MKT
        o = MarketOrder(action, qty)
        o.outsideRth = ctx.outside_rth
        if ctx.outside_rth:
            o.tif = 'GTC'
    # FA 帳戶必須指定下單帳戶，否則 Error 436
    if ctx.account:
        o.account = ctx.account
    return o


def place_orders(ib: IB, order_list: list, ctx: AccountCtx) -> tuple:
    """先 SELL 再 BUY。回傳 (placed_list, trades_list)"""
    sells = [o for o in order_list if o['action'] == 'SELL']
    buys  = [o for o in order_list if o['action'] == 'BUY']
    placed, trades = [], []

    for o in sells + buys:
        sym = o['symbol']
        qty = int(abs(o['qty']))
        if qty == 0:
            continue

        contract = Stock(sym, 'SMART', 'USD')
        ib.qualifyContracts(contract)
        order = make_order(o['action'], qty, o.get('lmt_price', 0), ctx=ctx)

        if ctx.dry_run:
            log.info(f"  [DRY RUN] {ctx.order_type} {o['action']:4s} {qty:5d} {sym}"
                     f"  ({o.get('strategy', '')})")
        else:
            trade = ib.placeOrder(contract, order)
            trades.append(trade)
            log.info(f"  {ctx.order_type} {o['action']:4s} {qty:5d} {sym}"
                     f"  orderId={trade.order.orderId}  ({o.get('strategy', '')})")
        placed.append(o)
        time.sleep(0.2)

    if not ctx.dry_run and placed:
        log.info(f"Waiting {ctx.wait_fill_sec}s for fills...")
        ib.sleep(ctx.wait_fill_sec)

    return placed, trades


def check_fills(trades: list) -> list:
    results = []
    for trade in trades:
        sym     = trade.contract.symbol
        action  = trade.order.action
        qty     = int(trade.order.totalQuantity)
        filled  = sum(f.execution.shares for f in trade.fills)
        avg_px  = (
            sum(f.execution.shares * f.execution.avgPrice for f in trade.fills) / filled
            if filled > 0 else 0.0
        )
        status  = trade.orderStatus.status
        log.info(f"  Fill: {action} {sym}  {filled}/{qty}sh @ ${avg_px:.2f}  [{status}]")
        results.append({
            'symbol':    sym,
            'action':    action,
            'qty':       qty,
            'filled':    filled,
            'avg_price': avg_px,
            'status':    status,
        })
    return results


# ── 策略訊號計算（S7：thin wrapper，實作在 signals.py）──────────────────

def compute_sr_weights(ctx: AccountCtx) -> tuple[dict, str]:
    """
    取 Sector Rotation 最新目標權重。

    S7：實作搬到 `signals.get_sr_weights()`，本函式保留為 thin wrapper
    以免 callers 需要同時 import signals + rebalance_ib。
    ctx 參數目前不影響計算（保留供將來 per-account SR 參數）。
    """
    from signals import get_sr_weights
    return get_sr_weights()


def compute_sp500_weights(current_sp500_holdings: list[str],
                          account_name: str,
                          ctx: AccountCtx) -> tuple[dict, str]:
    """
    基於 IB 帳戶的實際 SP500 持股，產生 live 目標權重。

    S7：實作搬到 `signals.get_sp500_weights()`，本函式保留為 thin wrapper。
    `ctx.name` 為 account_name 的權威來源。
    """
    from signals import get_sp500_weights
    return get_sp500_weights(current_sp500_holdings, ctx.name)


def combine_targets(nav: float, sr_wgt: dict, sp500_wgt: dict,
                    prices: dict, ctx: AccountCtx) -> list:
    """
    合併兩個策略的目標權重，計算每個標的的目標股數。

    SR 持倉：target_shares = NAV × alloc_sr × weight / price
    SP500 持倉：target_shares = NAV × alloc_sp500 × weight / price

    兩個策略使用不同標的池（ETF vs 個股），不會衝突。
    """
    alloc_sr    = ctx.alloc_sr
    alloc_sp500 = ctx.alloc_sp500

    targets = {}  # symbol -> {'shares': int, 'strategy': str}

    def _valid_price(p) -> bool:
        return p is not None and np.isfinite(p) and p > 0

    # SR 部位（ETF）
    for sym, w in sr_wgt.items():
        price = prices.get(sym, 0)
        if not _valid_price(price):
            log.warning(f"  {sym}: no price, skipping (SR)")
            continue
        shares = int(nav * alloc_sr * w / price)
        targets[sym] = {'shares': shares, 'strategy': 'SR',
                        'alloc_w': alloc_sr * w}

    # SP500 部位（個股）
    for sym, w in sp500_wgt.items():
        price = prices.get(sym, 0)
        if not _valid_price(price):
            log.warning(f"  {sym}: no price, skipping (SP500)")
            continue
        shares = int(nav * alloc_sp500 * w / price)
        if sym in targets:
            # 罕見：同一標的出現在兩個策略中（如 SPY dip-buy）
            targets[sym]['shares'] += shares
            targets[sym]['strategy'] = 'SR+SP500'
            targets[sym]['alloc_w'] += alloc_sp500 * w
        else:
            targets[sym] = {'shares': shares, 'strategy': 'SP500',
                            'alloc_w': alloc_sp500 * w}

    return targets


# ── Telegram 報告組合 ─────────────────────────────────────────────────────────

def _send_tg_report(nav, order_list, placed, trades, fill_results,
                    pos_detail, prices, sr_wgt, sp500_wgt,
                    sr_signal_date, sp500_signal_date, elapsed, error_msg,
                    ctx: AccountCtx):
    dry_run     = ctx.dry_run
    acct_name   = ctx.name
    alloc_sr    = ctx.alloc_sr
    alloc_sp500 = ctx.alloc_sp500
    order_type  = ctx.order_type
    leverage    = ctx.leverage
    tg_token    = ctx.tg_token
    tg_chat_id  = ctx.tg_chat_id

    dry_tag = "[DRY RUN] " if dry_run else ""
    acct_tag = f"[{acct_name}] " if acct_name else ""
    status  = "❌ ERROR" if error_msg else "✅ Done"
    lines   = [
        f"<b>{acct_tag}{dry_tag}QuantCore Combo — {status}</b>",
        f"Date: {date.today()}",
        f"SR Signal: {sr_signal_date}  |  SP500 Signal: {sp500_signal_date}",
        f"Alloc: SR {alloc_sr:.0%} + SP500 {alloc_sp500:.0%} = {alloc_sr+alloc_sp500:.0%}",
        f"Order: {order_type}  |  Leverage: {leverage}x",
    ]

    # NAV + 槓桿部位規模
    if nav is not None:
        pos_size = nav * leverage
        lines.append(f"NAV: <b>${nav:,.2f}</b>  →  部位 <b>${pos_size:,.0f}</b> ({leverage}x)")

    # SR 目標
    if sr_wgt:
        lines.append(f"\n<b>SR 目標 ({alloc_sr:.0%}):</b>")
        for sym, w in sorted(sr_wgt.items(), key=lambda x: -x[1]):
            lines.append(f"  {sym}: {w*100:.0f}%  (→ {alloc_sr*w*100:.0f}% of NAV)")

    # SP500 目標
    if sp500_wgt:
        lines.append(f"\n<b>SP500 目標 ({alloc_sp500:.0%}):</b>")
        for sym, w in sorted(sp500_wgt.items(), key=lambda x: -x[1]):
            lines.append(f"  {sym}: {w*100:.1f}%  (→ {alloc_sp500*w*100:.1f}% of NAV)")

    # 委託結果
    if not placed:
        lines.append("\nNo orders (already on target)")
    elif dry_run:
        lines.append(f"\n<b>委託 DRY RUN ({len(placed)} 筆):</b>")
        for o in placed:
            est_px  = o.get('lmt_price', 0)
            est_val = int(o['qty']) * est_px
            action_tag = "🔴 SELL" if o['action'] == 'SELL' else "🟢 BUY "
            strat = f"[{o.get('strategy', '')}]"
            lines.append(
                f"  {action_tag} {int(o['qty']):4d}sh {o['symbol']}"
                f"  ~${est_px:.2f}/sh  ~${est_val:,.0f}  {strat}"
            )
    else:
        lines.append(f"\n<b>委託結果 ({len(fill_results)} 筆):</b>")
        for f in fill_results:
            icon    = "✅" if f['status'] == 'Filled' else "⚠️"
            filled_val = f['filled'] * f['avg_price']
            px_str  = f"@ ${f['avg_price']:.2f}  ~${filled_val:,.0f}" if f['filled'] > 0 else "未成交"
            action_tag = "🔴 SELL" if f['action'] == 'SELL' else "🟢 BUY "
            lines.append(
                f"  {icon} {action_tag} {f['filled']}/{f['qty']}sh"
                f"  {f['symbol']}  {px_str}  [{f['status']}]"
            )

    # 目前持倉 + 損益
    if pos_detail and prices:
        lines.append("\n<b>目前持倉:</b>")
        total_upnl = 0.0
        for p in sorted(pos_detail, key=lambda x: -abs(x.position * prices.get(x.contract.symbol, 0))):
            sym      = p.contract.symbol
            pos      = int(p.position)
            avg_cost = p.avgCost
            mkt_px   = prices.get(sym, 0)
            upnl     = pos * mkt_px - pos * avg_cost
            pct      = upnl / (pos * avg_cost) * 100 if avg_cost else 0
            total_upnl += upnl
            sign     = "+" if upnl >= 0 else ""
            lines.append(
                f"  {sym}: {pos}sh"
                f"  均${avg_cost:.2f}→現${mkt_px:.2f}"
                f"  {sign}{upnl:.2f}({sign}{pct:.1f}%)"
            )
        sign = "+" if total_upnl >= 0 else ""
        lines.append(f"  <b>未實現損益: {sign}${total_upnl:,.2f}</b>")

    if error_msg:
        lines.append(f"\n<b>Error:</b> {html_mod.escape(str(error_msg)[:300])}")

    lines.append(f"\nElapsed: {elapsed}s")
    _tg("\n".join(lines), token=tg_token, chat_id=tg_chat_id)


# ── SR ETF 清單（用於分辨 IB 持倉屬於哪個策略）────────────────────────────────
# 只含 11 支板塊 ETF（與 strategies/sector_rotation/config.py SECTOR_TICKERS 一致）
# SPY / SSO 是 SP500 策略的熊市 dip-buy 標的，歸類為 SP500 持倉
SR_ETFS = {'XLK', 'XLF', 'XLV', 'XLE', 'XLI', 'XLY', 'XLB',
           'XLP', 'XLU', 'XLC', 'XLRE'}


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main(mode: str, ctx: AccountCtx):
    """
    mode = "sr"    → 只調 Sector Rotation（週二）
    mode = "sp500" → 只調 SP500 Momentum（週三）
    mode = "both"  → 兩個都調（手動用）

    ctx 為 AccountCtx，從 __main__ block 建立並傳入。
    """
    dry_run     = ctx.dry_run
    order_type  = ctx.order_type
    alloc_sr    = ctx.alloc_sr
    alloc_sp500 = ctx.alloc_sp500
    leverage    = ctx.leverage
    acct_name   = ctx.name

    run_sr    = mode in ("sr", "both")
    run_sp500 = mode in ("sp500", "both")
    mode_label = {"sr": "SR Only", "sp500": "SP500 Only", "both": "Both"}[mode]

    start_ts     = datetime.now()
    nav          = None
    placed       = []
    trades       = []
    fill_results = []
    pos_detail   = []
    prices       = {}
    error_msg    = None
    sr_wgt       = {}
    sp500_wgt    = {}
    sr_signal_date    = "N/A"
    sp500_signal_date = "N/A"

    log.info(f"{'='*60}")
    log.info(f"  QuantCore Rebalance  {date.today()}  [{mode_label}]")
    log.info(f"  dry_run={dry_run}  order_type={order_type}")
    log.info(f"  Alloc: SR {alloc_sr:.0%} + SP500 {alloc_sp500:.0%} = {alloc_sr+alloc_sp500:.0%}")
    log.info(f"{'='*60}")

    try:
        # ── 1. 計算 SR 目標權重（不需 IB）─────────────────────────────────────
        if run_sr:
            log.info("Step 1: Computing Sector Rotation target weights...")
            sr_wgt, sr_signal_date = compute_sr_weights(ctx=ctx)
        else:
            log.info("Step 1: SR skipped (sp500 mode)")

        # ── 2. 連線 IB ───────────────────────────────────────────────────────
        log.info("Step 2: Connecting to IB Gateway...")
        ib = connect_ib(ctx=ctx)

    except Exception as e:
        error_msg = str(e)
        log.error(f"ERROR in signal / IB connect: {e}", exc_info=True)
        elapsed = (datetime.now() - start_ts).seconds
        _send_tg_report(nav, [], placed, trades, fill_results,
                        pos_detail, prices, sr_wgt, sp500_wgt,
                        sr_signal_date, sp500_signal_date, elapsed, error_msg,
                        ctx=ctx)
        raise

    try:
        # ── 3. 帳戶資訊 + 辨別持倉歸屬 ──────────────────────────────────────
        nav = get_nav(ib, ctx=ctx)
        log.info(f"Step 3: NAV = ${nav:,.2f}  →  Position Size = ${nav*leverage:,.0f}")

        current_pos = get_positions(ib, ctx=ctx)
        log.info(f"Current positions: {current_pos if current_pos else '(none)'}")

        # 載入 SP500 成分股清單（用於分辨持倉歸屬）— S7 走 signals 單一入口
        from signals import get_sp500_universe
        _sp500_universe = get_sp500_universe()

        # 分辨當前持股：SR ETFs vs SP500 個股 vs 其他（手動操作，排除在外）
        current_sr_holdings = [sym for sym in current_pos if sym in SR_ETFS]
        current_sp500_holdings = [sym for sym in current_pos
                                  if sym not in SR_ETFS and sym in _sp500_universe]
        current_other_holdings = [sym for sym in current_pos
                                  if sym not in SR_ETFS and sym not in _sp500_universe]
        log.info(f"  SR holdings:    {current_sr_holdings or '(none)'}")
        log.info(f"  SP500 holdings: {current_sp500_holdings or '(none)'}")
        if current_other_holdings:
            other_val = sum(abs(current_pos[s]) * 1 for s in current_other_holdings)
            log.info(f"  OTHER holdings: {current_other_holdings} (excluded from strategy)")
            log.info(f"    -> These are manual/discretionary trades, NOT rebalanced")

        # ── 4. 計算 SP500 目標權重（基於 IB 實際持倉 + rank buffer）────────
        if run_sp500:
            log.info("Step 4: Computing SP500 Momentum weights (live, rank-aware)...")
            sp500_wgt, sp500_signal_date = compute_sp500_weights(
                current_sp500_holdings, acct_name, ctx=ctx)
        else:
            log.info("Step 4: SP500 skipped (sr mode)")
            # SR only 模式：保留 SP500 現有持倉不動
            for sym in current_sp500_holdings:
                sp500_wgt[sym] = 0  # 佔位用，不會產生 order

        # ── 5. 即時報價 ─────────────────────────────────────────────────────
        all_syms = list(set(
            list(sr_wgt.keys()) +
            list(sp500_wgt.keys()) +
            list(current_pos.keys())
        ))
        log.info(f"Step 5: Getting prices for {len(all_syms)} symbols...")
        prices = get_prices(ib, all_syms, ctx=ctx)

        # ── 6. 合併目標 + 計算差額 ──────────────────────────────────────────
        log.info("Step 6: Computing targets and deltas...")

        # 依 mode 決定哪些標的要計算目標、哪些保持不動
        if mode == "both":
            targets = combine_targets(nav, sr_wgt, sp500_wgt, prices, ctx=ctx)
        elif mode == "sr":
            # 只算 SR 目標，SP500 持倉維持現狀
            targets = combine_targets(nav, sr_wgt, {}, prices, ctx=ctx)
            # SP500 持倉標記為「保持現狀」→ target = 現有股數
            for sym in current_sp500_holdings:
                targets[sym] = {'shares': int(current_pos[sym]),
                                'strategy': 'SP500 (hold)', 'alloc_w': 0}
        elif mode == "sp500":
            # 只算 SP500 目標，SR 持倉維持現狀
            targets = combine_targets(nav, {}, sp500_wgt, prices, ctx=ctx)
            # SR 持倉標記為「保持現狀」→ target = 現有股數
            for sym in current_sr_holdings:
                targets[sym] = {'shares': int(current_pos[sym]),
                                'strategy': 'SR (hold)', 'alloc_w': 0}

        order_list = []

        # 處理所有策略標的（排除 OTHER 手動持倉，不會被賣出或調整）
        # 加入 REBALANCE_THRESHOLD 過濾 — 小於 3% NAV 漂移不動作（與 backtest 一致）
        from strategies.sp500_momentum import config as sp_cfg
        rebal_thresh = sp_cfg.REBALANCE_THRESHOLD  # 0.03

        all_target_syms = set(targets.keys()) | set(current_pos.keys())
        all_target_syms -= set(current_other_holdings)  # 排除手動操作標的
        for sym in all_target_syms:
            price = prices.get(sym, 0)
            if price <= 0:
                log.warning(f"  {sym}: no price, skipping")
                continue

            target_shares = targets.get(sym, {}).get('shares', 0)
            curr_shares   = int(current_pos.get(sym, 0))
            delta         = target_shares - curr_shares
            strategy      = targets.get(sym, {}).get('strategy', 'EXIT')

            # REBALANCE_THRESHOLD 過濾：小於 3% NAV 漂移不動作
            # 例外：完全清倉（target=0）或從無到有建倉（current=0）必須執行
            if delta != 0 and target_shares > 0 and curr_shares > 0:
                drift_val = abs(delta) * price
                drift_pct = drift_val / nav if nav > 0 else 0
                if drift_pct < rebal_thresh:
                    log.info(
                        f"  {sym} [{strategy}]: target={target_shares:5d}sh "
                        f"current={curr_shares:5d}sh  delta={delta:+d}  "
                        f"drift={drift_pct*100:.1f}% < {rebal_thresh*100:.0f}% → skip"
                    )
                    continue

            log.info(
                f"  {sym} [{strategy}]: target={target_shares:5d}sh "
                f"current={curr_shares:5d}sh  delta={delta:+d}"
            )

            if delta != 0:
                order_list.append({
                    'symbol':    sym,
                    'action':    'BUY' if delta > 0 else 'SELL',
                    'qty':       abs(delta),
                    'lmt_price': round(price * (0.9995 if delta > 0 else 1.0005), 2),
                    'strategy':  strategy,
                })

        # ── 7. 下單（先取消既有掛單，避免重複）─────────────────────────
        if not order_list:
            log.info("Step 7: No changes needed.")
        else:
            # 訂單去重
            seen_syms = set()
            dedup_list = []
            for o in order_list:
                if o['symbol'] in seen_syms:
                    log.warning(f"  duplicate order for {o['symbol']}, keeping first")
                    continue
                seen_syms.add(o['symbol'])
                dedup_list.append(o)
            order_list = dedup_list

            # 取消該帳戶既有掛單
            if not dry_run:
                open_trades = ib.openTrades()
                if open_trades:
                    log.info(f"  Cancelling {len(open_trades)} existing open orders...")
                    for t in open_trades:
                        ib.cancelOrder(t.order)
                    ib.sleep(2)

            log.info(f"Step 7: Placing {len(order_list)} orders ({order_type})...")
            placed, trades = place_orders(ib, order_list, ctx=ctx)

        # ── 8. 確認成交 ─────────────────────────────────────────────────────
        if trades:
            log.info("Step 8: Checking fills...")
            fill_results = check_fills(trades)

        # ── 9. 帳戶現況 + 損益 ──────────────────────────────────────────────
        log.info("Step 9: Fetching positions detail for P&L...")
        pos_detail = get_positions_detail(ib, ctx=ctx)

        log.info(f"{'='*60}")
        log.info(f"  Rebalance Done  [{mode_label}]")
        log.info(f"{'='*60}")

    except Exception as e:
        error_msg = str(e)
        log.error(f"ERROR: {e}", exc_info=True)
        raise

    finally:
        ib.disconnect()
        log.info("Disconnected from IB Gateway.")

        # ── Excel 交易記錄 ────────────────────────────────────────────────
        if placed:
            signal_str = f"SR:{sr_signal_date} SP500:{sp500_signal_date}"
            for o in placed:
                strat = o.get('strategy', 'Unknown')
                append_trade_log([o], fill_results, signal_str,
                                 order_type, leverage, dry_run,
                                 strategy=strat, account_name=acct_name)
            log.info("Trade log written -> trade_log.xlsx")

        elapsed = (datetime.now() - start_ts).seconds
        _send_tg_report(
            nav=nav,
            order_list=order_list if 'order_list' in dir() else [],
            placed=placed,
            trades=trades,
            fill_results=fill_results,
            pos_detail=pos_detail,
            prices=prices,
            sr_wgt=sr_wgt,
            sp500_wgt=sp500_wgt,
            sr_signal_date=sr_signal_date,
            sp500_signal_date=sp500_signal_date,
            elapsed=elapsed,
            error_msg=error_msg,
            ctx=ctx,
        )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="QuantCore Rebalance")
    parser.add_argument("--mode", choices=["sr", "sp500", "both"],
                        default="both",
                        help="sr=Sector Rotation only (Tue), "
                             "sp500=SP500 Momentum only (Wed), "
                             "both=all (default)")
    parser.add_argument("--order-type", choices=["MKT", "MOO", "LMT"],
                        default=None,
                        help="Override order type from ctx (e.g. MKT for manual intraday)")
    parser.add_argument("--account", required=True,
                        help="Account name from accounts.json (e.g. Taylor/Rosa/Yvonne), "
                             "or 'all' to run all accounts sequentially")
    args = parser.parse_args()

    from account_utils import load_accounts, ROLE_REBALANCE
    from dataclasses import replace as _replace

    if args.account.lower() == 'all':
        names = [a['name'] for a in load_accounts()]
    else:
        names = [args.account]

    for name in names:
        ctx = AccountCtx.from_name(name, ROLE_REBALANCE)
        if args.order_type:
            ctx = _replace(ctx, order_type=args.order_type)
        log.info(f">>> Account: {ctx.name} ({ctx.account}) port={ctx.port} <<<")
        main(mode=args.mode, ctx=ctx)
