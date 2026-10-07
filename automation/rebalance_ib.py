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

import html as html_mod
import json, logging, sys, urllib.request, urllib.parse
from dataclasses import replace
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

log = logging.getLogger(__name__)

# ── IB 連線工具 ───────────────────────────────────────────────────────────────
from ib_insync import util


# ── Telegram 通知（統一由 notify.py 提供；三帳戶合併一則，#26）──────────
from notify import Digest, error_line, order_group_lines

# ── AccountCtx（S6：ctx 為所有函式的必要參數，模組不再有 mutable globals）
from account_ctx import AccountCtx

# ── 排程執行殼（log、逐帳戶、失敗段落、執行紀錄、exit code；#206）
import run_status
import scheduled_run

# ── IB 連線（#185：經 Broker；讀數與送單都在 broker.Connection 上）
import ib_fetcher
from ib_fetcher import SR_ETFS  # R1.3：SR ETF 常數單一來源

# ── Rebalance 決策層（分類 / mode 權重選擇 / Combine / 差額訂單的唯一實作）
import rebalance_plan as plan
from rebalance_plan import combine_targets  # re-export：tests 與舊 callers 沿用此路徑

# ── 調倉輸入（帳戶快照 + 三策略權重的唯一實作，#186）
import plan_inputs

# ── 手動持倉歸屬與策略送單暫停（#106）
import manual_attribution

# ── 策略單送出與成交彙整（#205）
import strategy_orders
from strategy_orders import make_order  # noqa: F401 — re-export 一輪：舊路徑，之後刪


# ── 21:00 調倉的進度 log（plan_inputs 依步驟呼叫；文字與 #186 之前逐字相同）──

class _RebalanceSteps(plan_inputs.Narrator):
    def __init__(self, ctx: AccountCtx, mode: str):
        self._ctx = ctx
        self._mode = mode

    def sr(self) -> None:
        log.info("Step 1: Computing Sector Rotation target weights...")

    def nav(self, nav: float) -> None:
        log.info(f"Step 3: NAV = ${nav:,.2f}  →  Position Size = ${nav*self._ctx.leverage:,.0f}")

    def positions(self, positions: dict[str, float]) -> None:
        log.info(f"Current positions: {positions if positions else '(none)'}")

    def holdings(self, split) -> None:
        log.info(f"  SR holdings:    {split.sr or '(none)'}")
        log.info(f"  SP500 holdings: {split.sp500 or '(none)'}")
        if split.other:
            log.info(f"  OTHER holdings: {split.other} (excluded from strategy)")
            log.info(f"    -> These are manual/discretionary trades, NOT rebalanced")

    def sp500(self, run: bool) -> None:
        log.info("Step 4: Computing SP500 Momentum weights (live, rank-aware)..." if run
                 else "Step 4: SP500 skipped")

    def skewls(self, step: str) -> None:
        log.info({
            "check": "Step 4b: Computing Skew LS target + threshold check...",
            "hold": f"Step 4b: SkewLS not checked (mode={self._mode}), hold last state",
            "disabled": "Step 4b: SkewLS disabled (alloc_skewls = 0)",
        }[step])

    def skewls_gate(self, sk) -> None:
        log.info(f"  SkewLS threshold: Δ={sk.delta:.4f}  "
                 f"vs {sk.threshold} → {'TRIGGER' if sk.triggered else 'hold'}")

    def prices(self, symbols: list[str]) -> None:
        log.info(f"Step 5: Getting prices for {len(symbols)} symbols...")


class _RebalanceSend(strategy_orders.Narrator):
    """Step 7／8 的進度 log（文字與 #205 之前逐字相同）。"""

    def __init__(self, order_type: str):
        self._order_type = order_type

    def placing(self, count: int) -> None:
        log.info(f"Step 7: Placing {count} orders ({self._order_type})...")

    def checking_fills(self) -> None:
        log.info("Step 8: Checking fills...")


# ── combine_targets：實作移至 rebalance_plan.combine_targets（[[Combine]]，
#    ADR-0008 內部不變），上方 import 已 re-export。


# ── Telegram 調倉通知（#26）──────────────────────────────────────────────────

def rebalance_tg_section(placed: list[dict], fill_results: list[dict], *,
                         order_type: str, dry_run: bool,
                         drift_alarms: list[str] = (),
                         drift_advisories: list[str] = (),
                         pause_notices: list[str] = (),
                         error_msg: str | None = None
                         ) -> tuple[str, list[str], str] | None:
    """單一帳戶的 21:00 調倉段落 → (headline, body, level)；不需要發回 None。

    只列送出的單（BUY / SELL 各一行）。發送條件：有送單、對帳警報（🔴 該動
    卻沒送單 / ⚠️ 無報價 / 對帳段失敗）、新的手動持倉暫停（#106，同一次
    暫停只通知一次）、或錯誤。🟠 合計偏離只附在已經要發的段落。MOO 送出時
    還沒成交，成交由 21:31 moo_fill_check 回報；非 MOO（手動 --order-type
    MKT/LMT）則把沒成交完的單列出來。
    """
    unfilled = ([f for f in fill_results if f.get('status') != 'Filled']
                if order_type != 'MOO' else [])
    if not placed and not drift_alarms and not pause_notices and not error_msg:
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
    body += [html_mod.escape(ln) for ln in pause_notices]
    body += [html_mod.escape(ln) for ln in drift_alarms]
    body += [html_mod.escape(ln) for ln in drift_advisories]
    if error_msg:
        body.append(error_line(error_msg))   # headline 已帶 ❌

    if error_msg:
        level = "error"
    elif drift_alarms or unfilled or pause_notices:
        level = "warn"
    else:
        level = "ok"
    return headline, body, level


def _add_rebalance_section(digest: Digest, ctx: AccountCtx,
                           on_delivered=None, **kwargs) -> None:
    """組段落加進 digest。在 finally 內呼叫 — 自身絕不拋例外，避免蓋掉原錯誤。

    on_delivered：訊息確定送達後呼叫（手動持倉暫停記已通知，#106）。
    """
    try:
        section = rebalance_tg_section(order_type=ctx.order_type,
                                       dry_run=ctx.dry_run, **kwargs)
        if section is not None:
            digest.add(ctx.name, *section,
                       token=ctx.tg_token, chat_id=ctx.tg_chat_id,
                       on_delivered=on_delivered)
    except Exception as e:
        log.warning(f"Telegram section build failed: {e}")


def _log_trades_and_report(digest: Digest, ctx: AccountCtx, *,
                           placed: list[dict], fill_results: list[dict],
                           signal_str: str, drift_alarms: list[str],
                           drift_advisories: list[str],
                           error_msg: str | None,
                           pause_notices: list[str] = (),
                           on_delivered=None) -> Exception | None:
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
        pause_notices=pause_notices,
        error_msg=error_msg,
        on_delivered=on_delivered,
    )
    return excel_error


# ── SR ETF 清單 — R1.3：單一來源在 ib_fetcher.SR_ETFS（已 re-export）


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main(mode: str, ctx: AccountCtx, digest: Digest):
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

    ctx 為 AccountCtx，由排程執行殼（scheduled_run）建立並傳入。
    Telegram 段落加進 digest（排程執行殼三帳戶合併一則發送）。
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
    # — mode 語意的唯一實作在 plan_inputs（#186）
    mode_label = {
        "sr": "SR Only", "sp500": "SP500 Only",
        "skewls": "SkewLS Only", "both": "SR+SP500", "all": "All 3",
    }[mode]

    start_ts     = datetime.now()
    placed       = []
    fill_results = []
    pos_detail   = []
    error_msg    = None
    inputs: plan_inputs.PlanInputs | None = None   # Step 1–5 調倉輸入（中途失敗時保留已讀到的部分）
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
        inputs = plan_inputs.start(ctx, mode, persist_state=True,
                                   narrator=_RebalanceSteps(ctx, mode))

        # ── 2. 連線 IB ───────────────────────────────────────────────────────
        log.info("Step 2: Connecting to IB Gateway...")
        conn = ib_fetcher.connect(ctx)

    except Exception as e:
        error_msg = str(e)
        log.error(f"ERROR in signal / IB connect: {e}", exc_info=True)
        _add_rebalance_section(digest, ctx, placed=placed,
                               fill_results=fill_results, error_msg=error_msg)
        raise

    try:
        # ── 3–5. 帳戶資訊 + 持倉歸屬 → SP500 → Skew LS → 即時報價 ──────────
        # 唯一實作 = plan_inputs（#186）：classify_holdings（[[Universe]] 語意）、
        # 手動持倉歸屬（#106，讀寫失敗拋例外 = 本帳戶這輪策略單全部不送，且在
        # Step 4 算訊號寫 state 之前）、SP500 只在 sp500/both/all 重算（否則佔位
        # 權重只為帶進報價清單）、SkewLS threshold gate（alloc_skewls > 0 就納入
        # combine，否則 SR/SP500 rebal 會誤砍 SkewLS 倉位）。
        inputs.read(conn)
        nav = inputs.nav
        current_pos = inputs.positions
        current_other_holdings = inputs.split.other
        prices = inputs.prices
        attribution = inputs.attribution

        # ── 6. 合併目標 + 計算差額 ──────────────────────────────────────────
        log.info("Step 6: Computing targets and deltas...")

        # SR 訊號永遠加入 combine（stable 跨週、與 SkewLS sector ETF overlap 需要它）
        # SkewLS 在 step 4b 已決定 skewls_wgt (today_target 或 last_target)
        # SP500 只在 run_sp500=True 時加入，否則 hold 模式（無 SkewLS overlap，可安全 override）
        targets = combine_targets(nav, inputs.sr_wgt, inputs.sp500_for_combine, prices,
                                   ctx=ctx, skewls_wgt=inputs.skewls_wgt)
        targets = plan.apply_sp500_hold(targets, inputs.run_sp500,
                                        inputs.split.sp500, current_pos)

        planned = plan.build_order_list(
            targets=targets,
            current_pos=current_pos,
            current_other_holdings=current_other_holdings,
            prices=prices,
            nav=nav,
            log_fn=log.info,
            warn_fn=log.warning,
        )
        # ── 7–8. 下單、確認成交（strategy_orders，#205）──────────────────
        # 手動持倉暫停（#106）的標的不送，其他照常；送單前撤掉該帳戶既有策略
        # 掛單（Panel 手動單除外）。以 LMT 下單時限價走統一規則（#185）；MOO／MKT
        # 不帶限價，交易紀錄的預估價 = 算股數用的參考價
        order_list, held_orders = attribution.split_orders(planned, key=lambda o: o.symbol)
        for o in held_orders:
            log.warning(f"  ⏸ {o.symbol} {o.action} {o.qty} 不送出："
                        f"{attribution.paused[o.symbol]}")
        if not order_list:
            log.info("Step 7: No changes needed.")
        else:
            sent = strategy_orders.send(conn, order_list, ctx,
                                        narrator=_RebalanceSend(order_type))
            placed, fill_results = sent.placed, sent.fills

        # ── 9. 帳戶現況 + 損益 ──────────────────────────────────────────────
        log.info("Step 9: Fetching positions detail for P&L...")
        pos_detail = conn.positions_detail()

        # ── 9b. 理論 vs 實際對帳（執行驗證）─────────────────────────────
        # MOO 開盤才成交 → 不比成交、比「送單覆蓋」：算出該動卻一張單都
        # 沒送 = 🔴（2026-06 inverse 凍結類 bug 的當場警鈴）。
        log.info("Step 9b: Drift verification (theory vs actual)...")
        try:
            fresh_pos = conn.positions()
            drift_rows = plan.mark_paused(
                plan.verify_execution(
                    plan.evaluate_deltas(targets, fresh_pos,
                                         current_other_holdings, prices, nav),
                    placed,
                ),
                attribution.paused,
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

        # ── 10. Skew LS state 更新（threshold 觸發時；規則在 plan_inputs）────
        try:
            recorded = inputs.record_skewls_rebalance()
            if recorded is not None:
                log.info(f"Step 10: SkewLS state updated → "
                         f"last_target_date={recorded}")
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
        conn.close()
        log.info("Disconnected from IB Gateway.")

        # ── Excel 交易記錄 + Telegram 段落 ────────────────────────────────
        signal_parts = [f"SR:{inputs.sr_date}", f"SP500:{inputs.sp500_date}"]
        if alloc_skewls > 0:
            signal_parts.append(f"SkewLS:{inputs.skewls_date}")
        attribution = inputs.attribution
        new_notices = attribution.new_notices if attribution else []
        notice_keys = [n.key for n in new_notices]
        excel_error = _log_trades_and_report(
            digest, ctx,
            placed=placed,
            fill_results=fill_results,
            signal_str=" ".join(signal_parts),
            drift_alarms=drift_alarms,
            drift_advisories=drift_advisories,
            error_msg=error_msg,
            pause_notices=manual_attribution.pause_notice_lines(new_notices),
            # 暫停／接管結果只通知一次：訊息確定送達才記；沒送到下一輪再附上
            on_delivered=((lambda: manual_attribution.mark_notified(
                acct_name, notice_keys)) if notice_keys else None),
        )

        elapsed = (datetime.now() - start_ts).seconds
        log.info(f"Elapsed: {elapsed}s")
        # 已經在往外拋的例外（含 Ctrl+C）優先；沒有時才拋 Excel 錯誤（執行殼記為失敗）
        if excel_error is not None and sys.exc_info()[1] is None:
            raise excel_error


def run(argv: list[str] | None = None) -> int:
    """排程入口：解析 --mode／--order-type／--account，交給排程執行殼（#206）。"""
    parser = scheduled_run.account_parser("QuantCore Rebalance")
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
    args = parser.parse_args(argv)

    def per_account(ctx: AccountCtx, digest: Digest) -> None:
        if args.order_type:
            ctx = replace(ctx, order_type=args.order_type)
        main(mode=args.mode, ctx=ctx, digest=digest)

    return scheduled_run.run_job(run_status.REBALANCE_JOB, per_account, account=args.account)


if __name__ == '__main__':
    sys.exit(run())
