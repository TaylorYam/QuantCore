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
from log_setup import setup_logging, quiet_ib_console
log = logging.getLogger(__name__)

# ── IB 連線工具 ───────────────────────────────────────────────────────────────
from ib_insync import IB, Stock, MarketOrder, LimitOrder, Order, util


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
    SR_ETFS,  # R1.3：SR ETF 常數單一來源
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


def compute_skewls_weights(ctx: AccountCtx) -> tuple[dict, str]:
    """
    取 Sector Skew LS 最新目標權重（live 模式）— ticker-level signed weights。

    S16：thin wrapper 包 `signals.get_skewls_weights()`。
    回傳 dict 含 long sector ETF (正權重)、long inverse ETF (正權重)、
    XLC direct short (負權重)。未套 threshold gating。
    """
    from signals import get_skewls_weights
    return get_skewls_weights()


def combine_targets(nav: float, sr_wgt: dict, sp500_wgt: dict,
                    prices: dict, ctx: AccountCtx,
                    skewls_wgt: dict | None = None) -> dict:
    """
    合併三個策略的目標權重，計算每個標的的目標股數。

    SR 持倉：    target_shares = NAV × alloc_sr     × weight / price
    SP500 持倉： target_shares = NAV × alloc_sp500  × weight / price
    Skew LS：    target_shares = NAV × alloc_skewls × signed_weight / price
                  (XLC 的 weight < 0 → target_shares < 0 → IB SELL → 自動 SHORT)

    三策略共用同一檔 ticker 時 target_shares **直接加總**
    (pure-function combine，CONTEXT.md 已釘住此約定)，IB 自動 net 多空。
    """
    alloc_sr    = ctx.alloc_sr
    alloc_sp500 = ctx.alloc_sp500
    alloc_skewls = ctx.alloc_skewls

    targets = {}  # symbol -> {'shares': int, 'strategy': str, 'alloc_w': float}

    def _valid_price(p) -> bool:
        return p is not None and np.isfinite(p) and p > 0

    def _add(sym: str, shares: int, strategy_tag: str, alloc_w: float) -> None:
        """add shares + 合併 strategy tag (使用 '+' 分隔)"""
        if sym in targets:
            targets[sym]['shares']   += shares
            targets[sym]['alloc_w']  += alloc_w
            existing = targets[sym]['strategy'].split('+')
            if strategy_tag not in existing:
                targets[sym]['strategy'] = '+'.join(existing + [strategy_tag])
        else:
            targets[sym] = {'shares': shares, 'strategy': strategy_tag,
                            'alloc_w': alloc_w}

    # SR 部位（ETF, long-only）
    for sym, w in sr_wgt.items():
        price = prices.get(sym, 0)
        if not _valid_price(price):
            log.warning(f"  {sym}: no price, skipping (SR)")
            continue
        shares = int(nav * alloc_sr * w / price)
        _add(sym, shares, 'SR', alloc_sr * w)

    # SP500 部位（個股, long-only）
    for sym, w in sp500_wgt.items():
        price = prices.get(sym, 0)
        if not _valid_price(price):
            log.warning(f"  {sym}: no price, skipping (SP500)")
            continue
        # int(round(...)) 而非 floor:高價股 × 小 sleeve 時 floor 系統性低配
        # (Taylor 實測部署率 82~89%),round 誤差對稱 → 長期抵消
        # 三臂回測驗證見 robustness/frac_sizing_report.md (2026-07-05)
        shares = int(round(nav * alloc_sp500 * w / price))
        _add(sym, shares, 'SP500', alloc_sp500 * w)

    # Skew LS 部位（sector ETF / inverse ETF long + XLC short）
    if skewls_wgt:
        for sym, w in skewls_wgt.items():
            price = prices.get(sym, 0)
            if not _valid_price(price):
                log.warning(f"  {sym}: no price, skipping (SkewLS)")
                continue
            # signed: w 可為負 (XLC) → shares 為負 → IB 自動 SHORT
            # int(round(...)) 避免 negative truncation 不對稱
            shares = int(round(nav * alloc_skewls * w / price))
            _add(sym, shares, 'SkewLS', alloc_skewls * w)

    return targets


# ── Telegram 報告組合 ─────────────────────────────────────────────────────────

def _send_tg_report(nav, order_list, placed, trades, fill_results,
                    pos_detail, prices, sr_wgt, sp500_wgt,
                    sr_signal_date, sp500_signal_date, elapsed, error_msg,
                    ctx: AccountCtx,
                    skewls_wgt: dict | None = None,
                    skewls_signal_date: str = "N/A",
                    skewls_triggered: bool | None = None,
                    skewls_delta: float | None = None):
    dry_run     = ctx.dry_run
    acct_name   = ctx.name
    alloc_sr    = ctx.alloc_sr
    alloc_sp500 = ctx.alloc_sp500
    alloc_skewls = ctx.alloc_skewls
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
    ]
    if alloc_skewls > 0:
        lines.append(f"SkewLS Signal: {skewls_signal_date}")

    alloc_line = (f"Alloc: SR {alloc_sr:.0%} + SP500 {alloc_sp500:.0%}"
                  + (f" + SkewLS {alloc_skewls:.0%}" if alloc_skewls > 0 else "")
                  + f" = {leverage:.0%}")
    lines.append(alloc_line)
    lines.append(f"Order: {order_type}  |  Leverage: {leverage}x")

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

    # Skew LS 目標
    if skewls_wgt and alloc_skewls > 0:
        gate_tag = ""
        if skewls_triggered is not None:
            mark = "✅ 觸發" if skewls_triggered else "⏸ 維持"
            gate_tag = f"  [threshold {mark}"
            if skewls_delta is not None:
                gate_tag += f", Δ={skewls_delta:.3f}"
            gate_tag += "]"
        lines.append(f"\n<b>SkewLS 目標 ({alloc_skewls:.0%}){gate_tag}:</b>")
        for sym, w in sorted(skewls_wgt.items(), key=lambda x: -abs(x[1])):
            tag = "SHORT" if w < 0 else "LONG "
            lines.append(f"  {tag} {sym}: {w*100:+.1f}%  "
                         f"(→ {alloc_skewls*w*100:+.1f}% of NAV)")

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


# ── SR ETF 清單 — R1.3：單一來源在 ib_fetcher.SR_ETFS（已 re-export）


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main(mode: str, ctx: AccountCtx):
    """
    mode = "sr"     → 只調 Sector Rotation（週二）；SP500 + SkewLS 維持
    mode = "sp500"  → 只調 SP500 Momentum（週三）；SR + SkewLS 維持
    mode = "skewls" → 只調 Skew LS（每天 threshold check）；SR + SP500 維持
    mode = "both"   → SR + SP500（手動用，legacy）；SkewLS 維持
    mode = "all"    → 三策略全跑（SkewLS 仍有 threshold gate）

    Skew LS 行為：當 mode in ("skewls", "all") 時做 threshold check：
      - Delta > REBAL_THRESHOLD → 用 today_target + 更新 state
      - 否則 → 用 state['last_target']（維持上次持倉），不更新 state
    其他 mode 自動用 state['last_target'] 作為 Skew LS 貢獻，
    確保 SR/SP500 rebal 不會誤砍 Skew LS 倉位（pure-function combine）。

    ctx 為 AccountCtx，從 __main__ block 建立並傳入。
    """
    dry_run     = ctx.dry_run
    order_type  = ctx.order_type
    alloc_sr    = ctx.alloc_sr
    alloc_sp500 = ctx.alloc_sp500
    alloc_skewls = ctx.alloc_skewls
    leverage    = ctx.leverage
    acct_name   = ctx.name

    # SR 訊號是 pure function、跨週 stable，所有 mode 都算 (用於 combine 正確處理 overlap)
    # SP500 訊號可能 mutate state（dip-buy 觸發）→ 只在 sp500/both/all 跑
    # SkewLS threshold check 只在 skewls/all 跑（其他 mode 用 last_target 維持持倉）
    run_sr      = True
    run_sp500   = mode in ("sp500", "both", "all")
    run_skewls  = mode in ("skewls", "all")
    mode_label = {
        "sr": "SR Only", "sp500": "SP500 Only",
        "skewls": "SkewLS Only", "both": "SR+SP500", "all": "All 3",
    }[mode]

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
    skewls_wgt   = {}              # ticker-level signed weights for combine_targets
    skewls_target_today = {}       # raw signal output (for state save)
    skewls_triggered = None        # None = 該 mode 不檢查；T/F = 檢查過
    skewls_delta = None
    sr_signal_date    = "N/A"
    sp500_signal_date = "N/A"
    skewls_signal_date = "N/A"

    log.info(f"{'='*60}")
    log.info(f"  QuantCore Rebalance  {date.today()}  [{mode_label}]")
    log.info(f"  dry_run={dry_run}  order_type={order_type}")
    alloc_summary = (f"  Alloc: SR {alloc_sr:.0%} + SP500 {alloc_sp500:.0%}"
                     + (f" + SkewLS {alloc_skewls:.0%}" if alloc_skewls > 0 else "")
                     + f" = {leverage:.0%}")
    log.info(alloc_summary)
    log.info(f"{'='*60}")

    try:
        # ── 1. 計算 SR 目標權重（不需 IB）─────────────────────────────────────
        if run_sr:
            log.info("Step 1: Computing Sector Rotation target weights...")
            sr_wgt, sr_signal_date = compute_sr_weights(ctx=ctx)
        else:
            log.info("Step 1: SR skipped")

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
            log.info("Step 4: SP500 skipped")
            # 非 SP500 mode：保留 SP500 現有持倉不動
            for sym in current_sp500_holdings:
                sp500_wgt[sym] = 0  # 佔位用，不會產生 order

        # ── 4b. Skew LS：threshold check + 選 today_target or state ─────────
        # 不論哪個 mode，只要 alloc_skewls > 0 就需要把 SkewLS 部位納入 combine_targets，
        # 否則 SR/SP500 rebal 會誤砍 SkewLS 倉位。
        import skewls_state
        from strategies.sector_skew_ls import config as skewls_cfg
        skewls_threshold = skewls_cfg.REBAL_THRESHOLD

        if alloc_skewls > 0:
            state = skewls_state.load_state(acct_name)
            if run_skewls:
                log.info("Step 4b: Computing Skew LS target + threshold check...")
                skewls_target_today, skewls_signal_date = compute_skewls_weights(ctx=ctx)
                skewls_triggered, skewls_delta = skewls_state.should_rebal(
                    state, skewls_target_today, skewls_threshold,
                )
                log.info(f"  SkewLS threshold: Δ={skewls_delta:.4f}  "
                         f"vs {skewls_threshold} → {'TRIGGER' if skewls_triggered else 'hold'}")
                if skewls_triggered:
                    skewls_wgt = skewls_target_today
                else:
                    skewls_wgt = dict(state.get("last_target", {}))
                    skewls_signal_date = state.get("last_target_date") or skewls_signal_date
            else:
                log.info("Step 4b: SkewLS not checked (mode={}), hold last state".format(mode))
                skewls_wgt = dict(state.get("last_target", {}))
                skewls_signal_date = state.get("last_target_date") or "N/A"
        else:
            log.info("Step 4b: SkewLS disabled (alloc_skewls = 0)")
            skewls_wgt = {}

        # ── 5. 即時報價 ─────────────────────────────────────────────────────
        all_syms = list(set(
            list(sr_wgt.keys()) +
            list(sp500_wgt.keys()) +
            list(skewls_wgt.keys()) +
            list(current_pos.keys())
        ))
        log.info(f"Step 5: Getting prices for {len(all_syms)} symbols...")
        prices = get_prices(ib, all_syms, ctx=ctx)

        # ── 6. 合併目標 + 計算差額 ──────────────────────────────────────────
        log.info("Step 6: Computing targets and deltas...")

        # SR 訊號永遠加入 combine（stable 跨週、與 SkewLS sector ETF overlap 需要它）
        # SkewLS 在 step 4b 已決定 skewls_wgt (today_target 或 last_target)
        # SP500 只在 run_sp500=True 時加入，否則 hold 模式（無 SkewLS overlap，可安全 override）
        sp500_wgt_for_combine = sp500_wgt if run_sp500 else {}
        targets = combine_targets(nav, sr_wgt, sp500_wgt_for_combine, prices,
                                   ctx=ctx, skewls_wgt=skewls_wgt)

        # 非 SP500 mode：把 SP500 持股 target 改回 current（hold）
        # SP500 universe 與 SR/SkewLS 完全分離（個股 vs ETF），override 安全
        if not run_sp500:
            for sym in current_sp500_holdings:
                if sym not in targets:
                    targets[sym] = {'shares': int(current_pos[sym]),
                                    'strategy': 'SP500 (hold)', 'alloc_w': 0}
                else:
                    # 罕見：SP500 dip-buy ticker (SPY/SSO) 同時被 SR 或 SkewLS 用
                    # 此時保留 combined target (SR/SkewLS 貢獻)，不額外加 SP500 hold
                    targets[sym]['strategy'] = targets[sym]['strategy'] + '|SP500-hold'

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
            # 例外：清倉（target=0）/ 開倉（current=0）/ 翻向（long↔short）必須執行
            same_direction = (
                target_shares != 0 and curr_shares != 0
                and (target_shares > 0) == (curr_shares > 0)
            )
            if delta != 0 and same_direction:
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

        # ── 10. Skew LS state 更新（成功觸發 + 有下單時）────────────────
        if run_skewls and skewls_triggered and alloc_skewls > 0:
            try:
                state = skewls_state.load_state(acct_name)
                state = skewls_state.update_after_rebal(
                    state,
                    new_target=skewls_target_today,
                    signal_date=skewls_signal_date,
                    rebal_date=str(date.today()),
                )
                skewls_state.save_state(acct_name, state)
                log.info(f"Step 10: SkewLS state updated → "
                         f"last_target_date={skewls_signal_date}")
            except Exception as e:
                log.error(f"  SkewLS state save failed: {e}", exc_info=True)
                # 不 raise — state 寫入失敗不該阻擋交易完成回報

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
            signal_parts = [f"SR:{sr_signal_date}", f"SP500:{sp500_signal_date}"]
            if alloc_skewls > 0:
                signal_parts.append(f"SkewLS:{skewls_signal_date}")
            signal_str = " ".join(signal_parts)
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
            skewls_wgt=skewls_wgt,
            skewls_signal_date=skewls_signal_date,
            skewls_triggered=skewls_triggered,
            skewls_delta=skewls_delta,
        )


if __name__ == '__main__':
    setup_logging("rebalance")
    quiet_ib_console()
    parser = argparse.ArgumentParser(description="QuantCore Rebalance")
    parser.add_argument("--mode", choices=["sr", "sp500", "skewls", "both", "all"],
                        default="both",
                        help="sr=Sector Rotation (Tue), "
                             "sp500=SP500 Momentum (Wed), "
                             "skewls=Sector Skew LS (daily threshold check), "
                             "both=SR+SP500 (legacy), "
                             "all=SR+SP500+SkewLS")
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
