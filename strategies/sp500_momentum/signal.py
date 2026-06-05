"""
SP500 Momentum — live 訊號入口（get_latest_weights_live）

對齊 sector_rotation / sector_skew_ls：策略套件自己 export 一個
`get_latest_weights_live(...)`，把「今日資料 + 狀態 → 目標權重」整段邏輯
收在 strategy 套件內，不再散落到 automation/signals.py。

設計重點
--------
- **純函式（對 state 而言）**：state 由 caller 傳入、new_state 回傳，本函式
  不做任何檔案 I/O。每個 Account 的 state 持久化（load/save）屬於 automation 層
  （automation/sp500_state.py），因為「state 檔以 account 命名」這件事認得 Account，
  而 Strategy 不該認得 Account（見 CONTEXT.md）。
- 因此本函式 idempotent：同樣的 (holdings, state, as_of_date) 與資料 → 同樣輸出，
  且不會有「呼叫一次就推進 counter」的隱藏副作用（副作用在回傳的 new_state 裡，
  由 caller 決定要不要存）。
- `as_of_date` 預設今天；測試可指定歷史日期以重現任一分支（牛市 rotation / 熊市
  dip-buy / 等待）。

回測邏輯對齊
------------
  1. 牛市確認：連續 2 個 rebal day SPY>MA200 才做 rotation（bull_weeks_counter）
  2. 未確認牛市 + SPY < MA200 → 熊市，執行 dip-buy（15/20/25% DD）
  3. 未確認牛市 + SPY ≥ MA200 → 等待，不動作
  4. REBALANCE_WEEKS gate：只有 rotation week 才執行 rotation
  5. rotation 流程：保留 top 20 內且未跌破 EMA50 的持股，補滿 TARGET_HOLDINGS
  6. ATR 反比例權重
"""

from __future__ import annotations

import copy
import logging

import pandas as pd

from . import config as sp_cfg
from . import utils
from .selection import SelectionEngine
from .market_regime import MarketRegime
from .state import (update_bull_counter, is_bull_confirmed, reset_dip_state,
                    is_dip_triggered, mark_dip_triggered)

log = logging.getLogger(__name__)


def get_latest_weights_live(
    current_holdings: list[str],
    state: dict,
    as_of_date: pd.Timestamp | None = None,
) -> tuple[dict, pd.Timestamp, dict]:
    """
    取 SP500 Momentum 最新目標權重（live 模式）。

    Parameters
    ----------
    current_holdings : list[str]
        該帳戶目前屬於 SP500 策略的持股代碼（不含 SR ETFs、不含 dip 標的判斷）。
        rotation 流程會以此決定哪些保留、哪些賣出。
    state : dict
        SP500 狀態（bull_weeks_counter / dip_state / last_rebal_date）。
        本函式 deep-copy 後在副本上操作，不會 mutate 傳入的 dict。
    as_of_date : pd.Timestamp | None
        以此日期作為「今天」推算上一個排程調倉日；預設為真實今天。

    Returns
    -------
    (weights, signal_date, new_state)
        weights     : {ticker: weight_0to1}，gross sum ≈ 1.0（熊市 dip 情況下
                      可能 < 1.0，剩餘為現金）；無部位時為空 dict。
        signal_date : pd.Timestamp，用來計算訊號的實際最後一個交易日。
        new_state   : 更新後的 state（caller 負責持久化）。

    Note
    ----
    回傳空 weights 可能是：熊市未滿足 dip-buy 條件、或牛市尚未確認且 SPY ≥ MA200
    （holding pattern）。
    """
    state = copy.deepcopy(state)  # 純函式：不 mutate caller 的 dict

    # 初始化選股引擎 + 市場狀態
    selector = SelectionEngine()
    spy_df   = utils.load_benchmark(sp_cfg.ETF_DIR / "SPY.csv")
    sso_df   = utils.load_benchmark(sp_cfg.ETF_DIR / "SSO.csv")
    regime   = MarketRegime(spy_df, sso_df)

    # ── signal_date = 上一個排程調倉日的前一天收盤 ──────────────────────
    # 保證任何日期 sync 都會產生與「上次排程 rebalance」完全一致的結果，
    # 所有帳戶在任何時間 sync 都會看到相同的目標持倉。
    if as_of_date is None:
        as_of_date = pd.Timestamp.today().normalize()
    else:
        as_of_date = pd.Timestamp(as_of_date).normalize()

    rebal_wday  = sp_cfg.REBALANCE_WEEKDAY  # 2 = Wednesday
    days_back   = (as_of_date.dayofweek - rebal_wday) % 7
    last_rebal  = as_of_date - pd.Timedelta(days=days_back)

    # 資料截斷到 < last_rebal，取最後一筆作為 signal_date
    trimmed_idx = spy_df.index[spy_df.index < last_rebal]
    if len(trimmed_idx) > 0:
        signal_date = trimmed_idx[-1]
        log.info(f"SP500 last_rebal={last_rebal.date()} → signal_date={signal_date.date()}")
    else:
        signal_date = spy_df.index[-1]
        log.warning(f"  SP500: not enough history, using latest {signal_date.date()}")
    sig_date_str = str(signal_date.date())
    log.info(f"SP500 signal date: {sig_date_str}")
    log.info(f"SP500 current IB holdings: {current_holdings or '(none)'}")

    log.info(f"  state: bull_counter={state['bull_weeks_counter']}  "
             f"dip={state['dip_state']}  last_rebal={state.get('last_rebal_date')}")

    # ── 市場狀態 ────────────────────────────────────────────────────────
    mkt = regime.get_state(signal_date)
    if not mkt:
        log.warning("  SP500: no market regime data")
        return {}, signal_date, state

    spy_close  = mkt["SPY_Close"]
    spy_ma200  = mkt["SPY_MA200"]
    spy_dd     = abs(mkt["SPY_DD"])
    spy_above  = spy_close > spy_ma200

    log.info(f"  SPY=${spy_close:.2f}  MA200=${spy_ma200:.2f}  DD={spy_dd*100:.1f}%  "
             f"{'BULL' if spy_above else 'BEAR'}")

    # ── 牛市計數器更新 ─────────────────────────────────────────────────
    # 每個 signal_date 只計數一次，避免手動 Sync / 重複執行造成計數失真。
    # last_rebal_date 記錄上次計數的日期；同一天重跑不再更新 counter。
    last_counted = state.get("last_rebal_date")
    if last_counted != sig_date_str:
        state = update_bull_counter(state, spy_above, is_rebal_day=True)
        state["last_rebal_date"] = sig_date_str
        log.info(f"  counter updated (new signal_date): "
                 f"bull_counter={state['bull_weeks_counter']}")
    else:
        log.info(f"  counter unchanged (already counted {last_counted}): "
                 f"bull_counter={state['bull_weeks_counter']}")

    bull_confirmed = is_bull_confirmed(state)
    log.info(f"  bull_confirmed={bull_confirmed}")

    # ── 熊市 / 等待分支：bull 未確認 ────────────────────────────────────
    # 計算已觸發的 dip 階層總配置（相對於 SP500 配額）
    ALLOC_MAP = sp_cfg.DIP_BUY_ALLOCATIONS

    if not bull_confirmed:
        dip_ticker = sp_cfg.DIP_BUY_TICKER

        # 如果 SPY < MA200 且啟用 dip-buy，檢查是否要觸發新的階層
        if not spy_above and sp_cfg.ENABLE_BEAR_DIP_BUY:
            for level in (0.15, 0.20, 0.25):
                if spy_dd >= level and not is_dip_triggered(state, level):
                    alloc = ALLOC_MAP[level]
                    log.info(f"  DIP-BUY trigger: -{level*100:.0f}% "
                             f"(actual DD={spy_dd*100:.1f}%) → +{alloc*100:.0f}% 買 {dip_ticker}")
                    state = mark_dip_triggered(state, level)

        # 累積所有已觸發階層的總配置
        total_dip_alloc = 0.0  # 相對於 SP500 配額的比例
        for level, alloc in ALLOC_MAP.items():
            if is_dip_triggered(state, level):
                total_dip_alloc += alloc

        if spy_above:
            log.info(f"  SP500: waiting for bull confirmation (bull_counter < 2)")
        else:
            log.info(f"  SP500: BEAR market → dip_total={total_dip_alloc*100:.0f}%")

        # 清個股，只保留 dip 部位
        if total_dip_alloc > 0:
            log.info(f"  → {dip_ticker} 權重 = {total_dip_alloc*100:.0f}%")
            return {dip_ticker: total_dip_alloc}, signal_date, state
        return {}, signal_date, state

    # ── 牛市已確認：清掉任何 dip 部位 ───────────────────────────────────
    if any(state["dip_state"].values()):
        log.info("  BULL confirmed → reset dip_state")
        state = reset_dip_state(state)

    # ── REBALANCE_WEEKS gate：非 rotation week 只維持現狀 ──────────────
    iso_week = signal_date.isocalendar()[1]
    is_rotation_week = (iso_week % sp_cfg.REBALANCE_WEEKS == 0)
    log.info(f"  iso_week={iso_week}  rotation_week={is_rotation_week} "
             f"(REBALANCE_WEEKS={sp_cfg.REBALANCE_WEEKS})")

    if not is_rotation_week:
        # 非 rotation week：只重新計算現有持股的 ATR 權重
        kept = [t for t in current_holdings
                if t != sp_cfg.DIP_BUY_TICKER]
        log.info(f"  non-rotation week → re-weight kept holdings: {kept}")
        if not kept:
            return {}, signal_date, state
        metrics = [selector.calculate_metrics(t, signal_date, sp_cfg.LOOKBACK)
                   for t in kept]
        metrics = [m for m in metrics if m and m.get("atr_pct", 0) > 0]
        if not metrics:
            return {}, signal_date, state
        inv_sum = sum(1 / m["atr_pct"] for m in metrics)
        wgt = {m["ticker"]: (1 / m["atr_pct"]) / inv_sum for m in metrics}
        return wgt, signal_date, state

    # ── Rotation week：完整 rotation 流程 ───────────────────────────────
    ranked = selector.scan_market(signal_date, sp_cfg.LOOKBACK)
    top_tickers = [x["ticker"] for x in ranked][:sp_cfg.SELL_RANK_THRESHOLD]

    # 過熱過濾（買入候選用）
    ranked_filtered = [x for x in ranked
                       if x.get("adj_slope", 999) < sp_cfg.MAX_ADJ_SLOPE]
    entry_tickers = [x["ticker"] for x in ranked_filtered]

    log.info(f"  Top {sp_cfg.SELL_RANK_THRESHOLD} tickers: {top_tickers[:10]}...")

    # ── Step 1: 判斷現有持股保留 or 賣出 ─────────────────────────────────
    # 排除 dip 標的（牛市確認時已 reset，但留個保險）
    non_dip_holdings = [t for t in current_holdings
                        if t != sp_cfg.DIP_BUY_TICKER]
    kept = []
    for ticker in non_dip_holdings:
        reason = None
        if ticker not in top_tickers:
            reason = f"排名跌出前{sp_cfg.SELL_RANK_THRESHOLD}名"
        else:
            m = next((x for x in ranked if x["ticker"] == ticker), None)
            if m and m["price"] < m["exit_ema"]:
                reason = f"跌破EMA{sp_cfg.EXIT_EMA}"

        if reason:
            log.info(f"  SELL {ticker}: {reason}")
        else:
            log.info(f"  KEEP {ticker}: still in top {sp_cfg.SELL_RANK_THRESHOLD}")
            kept.append(ticker)

    # ── Step 2: 補進新股（如果持股不足 TARGET_HOLDINGS）───────────────────
    buy_candidates = []
    needed = sp_cfg.TARGET_HOLDINGS - len(kept)
    if needed > 0:
        candidates = [t for t in entry_tickers if t not in kept][:needed]
        for ticker in candidates:
            m = next((x for x in ranked_filtered if x["ticker"] == ticker), None)
            if m and m.get("max_gap", 0) <= sp_cfg.SKIP_MAX_GAP_PCT:
                log.info(f"  BUY  {ticker}: rank={entry_tickers.index(ticker)+1}"
                         f"  adj_slope={m['adj_slope']:.3f}")
                buy_candidates.append(ticker)

    # ── Step 3: ATR 反比例權重 ───────────────────────────────────────────
    final_tickers = kept + buy_candidates
    if not final_tickers:
        log.info("  SP500: no positions to hold")
        return {}, signal_date, state

    metrics = []
    for t in final_tickers:
        m = next((x for x in ranked if x["ticker"] == t), None) or \
            selector.calculate_metrics(t, signal_date, sp_cfg.LOOKBACK)
        if m and m.get("atr_pct", 0) > 0:
            metrics.append(m)

    if not metrics:
        log.info("  SP500: no valid metrics for weight calculation")
        return {}, signal_date, state

    inv_sum = sum(1 / m["atr_pct"] for m in metrics)
    wgt = {m["ticker"]: (1 / m["atr_pct"]) / inv_sum for m in metrics}

    log.info(f"  SP500 target weights (ATR inverse):")
    for sym, w in sorted(wgt.items(), key=lambda x: -x[1]):
        log.info(f"    {sym}: {w*100:.1f}%")

    return wgt, signal_date, state
