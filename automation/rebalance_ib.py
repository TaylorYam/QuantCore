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
from ib_insync import IB, MarketOrder, LimitOrder, Order, util
from symbol_contract import canonical_symbol, ib_stock


# ── Telegram 通知（統一由 notify.py 提供；三帳戶合併一則，#26）──────────
from notify import Digest, order_group_lines

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

# ── Rebalance 決策層（分類 / mode 權重選擇 / Combine / 差額訂單的唯一實作）
import rebalance_plan as plan
from rebalance_plan import combine_targets  # re-export：tests 與舊 callers 沿用此路徑


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

        contract = ib_stock(sym)
        ib.qualifyContracts(contract)
        order = make_order(o['action'], qty, o.get('lmt_price', 0), ctx=ctx)

        if ctx.dry_run:
            log.info(f"  [DRY RUN] {ctx.order_type} {o['action']:4s} {qty:5d} {sym}"
                     f"  ({o.get('strategy', '')})")
        else:
            trade = ib.placeOrder(contract, order)
            trades.append(trade)
            # Preserve the broker identity for an idempotent SQLite log write.
            # Excel output remains unchanged because this is metadata on the
            # in-memory order dict only.
            o["order_id"] = str(trade.order.orderId)
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
        sym     = canonical_symbol(trade.contract.symbol)
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


# ── combine_targets：實作移至 rebalance_plan.combine_targets（[[Combine]]，
#    ADR-0008 內部不變），上方 import 已 re-export。


# ── Telegram 調倉通知（#26）──────────────────────────────────────────────────

def rebalance_tg_section(placed: list[dict], fill_results: list[dict], *,
                         order_type: str, dry_run: bool,
                         drift_alarms: list[str] = (),
                         drift_advisories: list[str] = (),
                         error_msg: str | None = None
                         ) -> tuple[str, list[str], str] | None:
    """單一帳戶的 21:00 調倉段落 → (headline, body, level)；不需要發回 None。

    只列送出的單（BUY / SELL 各一行）。發送條件：有送單、對帳警報（🔴 該動
    卻沒送單 / ⚠️ 無報價 / 對帳段失敗）、或錯誤。🟠 合計偏離只附在已經要發
    的段落。MOO 送出時還沒成交，成交由 21:31 moo_fill_check 回報；非 MOO
    （手動 --order-type MKT/LMT）則把沒成交完的單列出來。
    """
    unfilled = ([f for f in fill_results if f.get('status') != 'Filled']
                if order_type != 'MOO' else [])
    if not placed and not drift_alarms and not error_msg:
        return None

    dry = " [DRY RUN]" if dry_run else ""
    if placed:
        headline = f"送出 {len(placed)} 筆 {order_type}{dry}"
        if error_msg:
            headline += "，❌ 之後失敗"
    else:
        headline = f"❌ 失敗{dry}" if error_msg else f"沒有送單{dry}"

    body = order_group_lines(placed)
    body += [f"⚠️ {f['symbol']} 成交 {f['filled']}/{f['qty']} [{f['status']}]"
             for f in unfilled]
    body += [html_mod.escape(ln) for ln in drift_alarms]
    body += [html_mod.escape(ln) for ln in drift_advisories]
    if error_msg:
        body.append(html_mod.escape(str(error_msg)[:300]))   # headline 已帶 ❌

    if error_msg:
        level = "error"
    elif drift_alarms or unfilled:
        level = "warn"
    else:
        level = "ok"
    return headline, body, level


def _add_rebalance_section(digest: Digest, ctx: AccountCtx, **kwargs) -> None:
    """組段落加進 digest。在 finally 內呼叫 — 自身絕不拋例外，避免蓋掉原錯誤。"""
    try:
        section = rebalance_tg_section(order_type=ctx.order_type,
                                       dry_run=ctx.dry_run, **kwargs)
        if section is not None:
            digest.add(ctx.name, *section,
                       token=ctx.tg_token, chat_id=ctx.tg_chat_id)
    except Exception as e:
        log.warning(f"Telegram section build failed: {e}")


def _log_trades_and_report(digest: Digest, ctx: AccountCtx, *,
                           placed: list[dict], fill_results: list[dict],
                           signal_str: str, drift_alarms: list[str],
                           drift_advisories: list[str],
                           error_msg: str | None) -> Exception | None:
    """main() 收尾：寫 Excel 交易紀錄，再加 Telegram 段落。回傳 Excel 寫入例外。

    Excel 寫入失敗（例如 trade_log.xlsx 被 Excel 開著鎖住）時，單已經送出了 —
    不能讓它們從訊息裡消失：記下錯誤、段落照加並標 ❌，例外交給 caller 拋出。
    """
    excel_error: Exception | None = None
    if placed:
        try:
            for o in placed:
                strat = o.get('strategy', 'Unknown')
                append_trade_log([o], fill_results, signal_str,
                                 ctx.order_type, ctx.leverage, ctx.dry_run,
                                 strategy=strat, account_name=ctx.name)
            log.info("Trade log written -> trade_log.xlsx")
        except Exception as e:
            excel_error = e
            log.error(f"Trade log write failed: {e}", exc_info=True)

    if excel_error is not None:
        note = f"交易紀錄寫入失敗: {excel_error}"
        error_msg = f"{error_msg}；{note}" if error_msg else note
    _add_rebalance_section(
        digest, ctx,
        placed=placed,
        fill_results=fill_results,
        drift_alarms=drift_alarms,
        drift_advisories=drift_advisories,
        error_msg=error_msg,
    )
    return excel_error


def run_accounts(names: list[str], mode: str,
                 order_type: str | None = None,
                 digest: Digest | None = None) -> list[tuple[str, str]]:
    """依序調倉多個帳戶，Telegram 合併一則（#26）。回傳失敗清單 [(帳戶, 錯誤)]。

    某帳戶失敗（連不上 Gateway、送單後查成交或寫 Excel 出錯…）→ 寫 log、訊息
    標 ❌，繼續下一個帳戶（#29）：三帳戶的 Gateway port、clientId、state 檔互相
    獨立，前一個帳戶的 Gateway 掛掉不該拖累其他帳戶。exit code 由 __main__
    依回傳的失敗清單決定。Ctrl+C（KeyboardInterrupt）仍立即中止，剩下的帳戶
    標「未執行」。digest 由 caller 傳入時，跑完仍由這裡發送，caller 可再讀
    各帳戶結果（#38 執行紀錄）。
    """
    from dataclasses import replace as _replace
    from account_utils import ROLE_REBALANCE, telegram_dest

    if digest is None:
        digest = Digest(f"調倉 {date.today()}", icon="📤")
    failures: list[tuple[str, str]] = []
    started: list[str] = []
    try:
        for name in names:
            started.append(name)
            try:
                ctx = AccountCtx.from_name(name, ROLE_REBALANCE)
                if order_type:
                    ctx = _replace(ctx, order_type=order_type)
                log.info(f">>> Account: {ctx.name} ({ctx.account}) port={ctx.port} <<<")
                main(mode=mode, ctx=ctx, digest=digest)
            except Exception as e:
                failures.append((name, str(e)))
                log.error(f"Account {name} failed (continuing to next): {e}")
                if not digest.has(name):
                    digest.add(name, "❌ 失敗", [html_mod.escape(str(e)[:300])],
                               "error", **telegram_dest(name))
    except BaseException:
        for name in names[len(started):]:
            digest.add(name, "⚠️ 未執行", ["手動中止，本輪未執行"],
                       "warn", **telegram_dest(name))
        raise
    finally:
        digest.send()
    return failures


# ── SR ETF 清單 — R1.3：單一來源在 ib_fetcher.SR_ETFS（已 re-export）


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main(mode: str, ctx: AccountCtx, digest: Digest | None = None):
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
    Telegram 段落加進 digest（__main__ 三帳戶合併一則發送）；沒給 digest
    時自己建一則並在結束時發送。
    """
    own_digest = digest is None
    if own_digest:
        digest = Digest(f"調倉 {date.today()}", icon="📤")
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
    drift_alarms: list[str] = []      # Step 9b 對帳警報（TG 必發）
    drift_advisories: list[str] = []  # Step 9b 🟠 合計偏離（TG 附帶）

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
        _add_rebalance_section(digest, ctx, placed=placed,
                               fill_results=fill_results, error_msg=error_msg)
        if own_digest:
            digest.send()
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

        # 分辨當前持股（[[Universe]] 語意；唯一實作 = rebalance_plan.classify_holdings）
        split = plan.classify_holdings(current_pos, _sp500_universe)
        current_sr_holdings = split.sr
        current_sp500_holdings = split.sp500
        current_other_holdings = split.other
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
            # 非 SP500 mode：佔位權重只為把持股帶進報價清單，不會產生 order
            sp500_wgt = plan.sp500_placeholder_weights(current_sp500_holdings)

        # ── 4b. Skew LS：threshold check + 選 today_target or state ─────────
        # 不論哪個 mode，只要 alloc_skewls > 0 就需要把 SkewLS 部位納入 combine_targets，
        # 否則 SR/SP500 rebal 會誤砍 SkewLS 倉位。
        import skewls_state

        if alloc_skewls > 0:
            state = skewls_state.load_state(acct_name)
            if run_skewls:
                log.info("Step 4b: Computing Skew LS target + threshold check...")
                skewls_target_today, skewls_signal_date = compute_skewls_weights(ctx=ctx)
            else:
                log.info("Step 4b: SkewLS not checked (mode={}), hold last state".format(mode))
            sk = plan.select_skewls_weights(
                run_skewls, alloc_skewls, state,
                today_target=skewls_target_today,
                today_signal_date=skewls_signal_date,
            )
            skewls_wgt = sk.wgt
            skewls_signal_date = sk.signal_date
            skewls_triggered = sk.triggered
            skewls_delta = sk.delta
            if run_skewls:
                log.info(f"  SkewLS threshold: Δ={sk.delta:.4f}  "
                         f"vs {sk.threshold} → {'TRIGGER' if sk.triggered else 'hold'}")
        else:
            log.info("Step 4b: SkewLS disabled (alloc_skewls = 0)")
            skewls_wgt = {}

        # ── 5. 即時報價 ─────────────────────────────────────────────────────
        all_syms = plan.plan_symbols(sr_wgt, sp500_wgt, skewls_wgt, current_pos)
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
        targets = plan.apply_sp500_hold(targets, run_sp500,
                                        current_sp500_holdings, current_pos)

        planned = plan.build_order_list(
            targets=targets,
            current_pos=current_pos,
            current_other_holdings=current_other_holdings,
            prices=prices,
            nav=nav,
            log_fn=log.info,
            warn_fn=log.warning,
        )
        # LMT 保護價（execution 關切，留在 shell）：BUY −0.05% / SELL +0.05%
        order_list = [{
            'symbol':    o.symbol,
            'action':    o.action,
            'qty':       o.qty,
            'lmt_price': round(o.price * (0.9995 if o.action == 'BUY' else 1.0005), 2),
            'strategy':  o.strategy,
        } for o in planned]

        # ── 7. 下單（先取消既有掛單，避免重複）─────────────────────────
        if not order_list:
            log.info("Step 7: No changes needed.")
        else:
            # 取消該帳戶既有掛單
            if not dry_run:
                from manual_orders import strategy_orders_to_cancel
                open_trades = strategy_orders_to_cancel(ib.openTrades())
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

        # ── 9b. 理論 vs 實際對帳（執行驗證）─────────────────────────────
        # MOO 開盤才成交 → 不比成交、比「送單覆蓋」：算出該動卻一張單都
        # 沒送 = 🔴（2026-06 inverse 凍結類 bug 的當場警鈴）。
        log.info("Step 9b: Drift verification (theory vs actual)...")
        try:
            fresh_pos = get_positions(ib, ctx=ctx)
            drift_rows = plan.verify_execution(
                plan.evaluate_deltas(targets, fresh_pos,
                                     current_other_holdings, prices, nav),
                placed,
            )
            drift_lines = plan.drift_report_lines(drift_rows)
            for ln in drift_lines:
                if ln:
                    log.info(f"  {ln}")
            drift_alarms, drift_advisories = plan.drift_alert_lines(drift_rows)
        except Exception as e:
            log.error(f"  drift verification failed: {e}", exc_info=True)
            drift_alarms = [f"⚠️ 對帳段失敗: {e}"]
            # 不 raise — 對帳失敗不該阻擋交易完成回報

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

        # ── Excel 交易記錄 + Telegram 段落 ────────────────────────────────
        signal_parts = [f"SR:{sr_signal_date}", f"SP500:{sp500_signal_date}"]
        if alloc_skewls > 0:
            signal_parts.append(f"SkewLS:{skewls_signal_date}")
        excel_error = _log_trades_and_report(
            digest, ctx,
            placed=placed,
            fill_results=fill_results,
            signal_str=" ".join(signal_parts),
            drift_alarms=drift_alarms,
            drift_advisories=drift_advisories,
            error_msg=error_msg,
        )

        elapsed = (datetime.now() - start_ts).seconds
        log.info(f"Elapsed: {elapsed}s")
        if own_digest:
            digest.send()
        # 已經在往外拋的例外（含 Ctrl+C）優先；沒有時才拋 Excel 錯誤（run_accounts 記為失敗）
        if excel_error is not None and sys.exc_info()[1] is None:
            raise excel_error


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

    from account_utils import load_accounts
    import run_status

    all_accounts = args.account.lower() == 'all'
    names: list[str] = []
    digest = Digest(f"調倉 {date.today()}", icon="📤")
    # 當日排程總結用的執行紀錄（#38）：只記排程的 --account all
    with run_status.recording("rebalance", lambda: digest.outcomes(names),
                              enabled=all_accounts):
        names.extend([a['name'] for a in load_accounts()] if all_accounts
                     else [args.account])
        failures = run_accounts(names, mode=args.mode, order_type=args.order_type,
                                digest=digest)
    if failures:
        log.error(f"Rebalance finished with {len(failures)} failure(s): "
                  + "; ".join(f"{n}={msg}" for n, msg in failures))
        sys.exit(1)
