"""
QuantCore — 策略訊號統一入口（S7）

把散落在 rebalance_ib / portfolio_calc / sp500_daily_check / control_panel / ... 的
策略訊號計算收斂到單一模組：

  get_sr_weights()           → Sector Rotation 最新目標權重
  get_sp500_weights(...)     → SP500 Momentum 最新目標權重（含 state machine）

Caller 不用再 import `SelectionEngine` / `MarketRegime` / `data_loader` /
`sp_cfg` 等策略內部模組 — 只 import 這兩個函式就好。

純函式，不依賴 AccountCtx（策略訊號本身不是 per-account）：
  - SR 只依賴 strategies.sector_rotation 的 config
  - SP500 依賴帳戶的 current_holdings + state file（state file 用 account_name 命名）
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

# 確保 strategies 套件可 import（QuantCore root 要在 sys.path）
# 這個 module 可能被 control_panel → ib_fetcher → signals 這條路徑第一個用到，
# 而 rebalance_ib.py 的 sys.path 設定是懶載入的
_ROOT_DIR = Path(__file__).parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

log = logging.getLogger(__name__)


# ── Sector Rotation ──────────────────────────────────────────────────────

def get_sr_weights(as_of_date: Optional[pd.Timestamp] = None
                    ) -> tuple[dict, str]:
    """
    取 Sector Rotation 最新目標權重（live 模式）。

    使用 `get_latest_weights_live()` 從最新資料直接算訊號 — 不套用
    rebalance mask / forward-fill，避免週二盤前拿到上週舊訊號。等同回測
    中「今天是 rebalance day」時的 shift(1) 結果。

    Parameters
    ----------
    as_of_date : pd.Timestamp | None
        目前未使用，保留給未來需要「在特定歷史日期重算訊號」時。
        （strategies.sector_rotation.signal 的 get_latest_weights_live 目前
        只支援 live mode。）

    Returns
    -------
    (weights_dict, signal_date_str)
        weights_dict: {symbol: weight_0to1}
        signal_date_str: 'YYYY-MM-DD'
    """
    from strategies.sector_rotation.data_loader import load_data
    from strategies.sector_rotation.signal import get_latest_weights_live

    data = load_data()
    etf_close = data['etf_close']
    weights, sig_date = get_latest_weights_live(etf_close)

    wgt = {sym: float(w) for sym, w in weights.items() if w > 0.001}
    log.info(f"SR signal date: {sig_date.date()}  (live, skip rebal mask)")
    for sym, w in sorted(wgt.items(), key=lambda x: -x[1]):
        log.info(f"  SR  {sym}: {w*100:.0f}%")
    return wgt, str(sig_date.date())


# ── SP500 Momentum ────────────────────────────────────────────────────────

def get_sp500_weights(current_sp500_holdings: list[str],
                      account_name: str) -> tuple[dict, str]:
    """
    基於 IB 帳戶的實際 SP500 持股，產生 live 目標權重（與 backtester 一致）。

    對齊回測的邏輯：
      1. 牛市確認：連續 2 個 rebal day SPY>MA200 才做 rotation
         （bull_weeks_counter，持久化於 sp500_state_{account}.json）
      2. 未確認牛市 + SPY < MA200 → 熊市，執行 dip-buy（15/20/25% DD）
      3. 未確認牛市 + SPY ≥ MA200 → 等待，不動作
      4. REBALANCE_WEEKS gate：只有 rotation week 才執行 rotation
      5. rotation 流程：保留 top 20 內且未跌破 EMA50 的持股，補滿 TARGET_HOLDINGS
      6. ATR 反比例權重

    Parameters
    ----------
    current_sp500_holdings : list[str]
        IB 帳戶中屬於 SP500 策略的股票代碼（不含 SR ETFs）
    account_name : str
        帳戶名稱，用於讀取 / 寫入 state file（必填）

    Returns
    -------
    (weights_dict, signal_date_str)

    Note
    ----
    當回傳空 dict 時可能是下列情況之一：
      - 熊市未滿足 dip-buy 條件（權重由熊市邏輯處理）
      - 牛市尚未確認且 SPY ≥ MA200 (holding pattern)
    主流程會根據 SR+SP500 合併結果計算 orders。
    """
    from strategies.sp500_momentum.selection    import SelectionEngine
    from strategies.sp500_momentum.market_regime import MarketRegime
    from strategies.sp500_momentum import config as sp_cfg, utils
    from sp500_state import (load_state, save_state, update_bull_counter,
                             is_bull_confirmed, reset_dip_state,
                             is_dip_triggered, mark_dip_triggered)

    # 初始化選股引擎
    selector = SelectionEngine()
    spy_df   = utils.load_benchmark(sp_cfg.ETF_DIR / "SPY.csv")
    sso_df   = utils.load_benchmark(sp_cfg.ETF_DIR / "SSO.csv")
    regime   = MarketRegime(spy_df, sso_df)

    # ── signal_date = 上一個排程調倉日的前一天收盤 ──────────────────────
    # 保證任何日期 sync 都會產生與「上次排程 rebalance」完全一致的結果，
    # 所有帳戶在任何時間 sync 都會看到相同的目標持倉。
    today       = pd.Timestamp.today().normalize()
    rebal_wday  = sp_cfg.REBALANCE_WEEKDAY  # 2 = Wednesday
    days_back   = (today.dayofweek - rebal_wday) % 7
    last_rebal  = today - pd.Timedelta(days=days_back)

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
    log.info(f"SP500 current IB holdings: {current_sp500_holdings or '(none)'}")

    # ── 讀取持久化狀態 ──────────────────────────────────────────────────
    state = load_state(account_name)
    log.info(f"  state: bull_counter={state['bull_weeks_counter']}  "
             f"dip={state['dip_state']}  last_rebal={state.get('last_rebal_date')}")

    # ── 市場狀態 ────────────────────────────────────────────────────────
    mkt = regime.get_state(signal_date)
    if not mkt:
        log.warning("  SP500: no market regime data")
        return {}, sig_date_str

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

        save_state(account_name, state)

        if spy_above:
            log.info(f"  SP500: waiting for bull confirmation (bull_counter < 2)")
        else:
            log.info(f"  SP500: BEAR market → dip_total={total_dip_alloc*100:.0f}%")

        # 清個股，只保留 dip 部位
        if total_dip_alloc > 0:
            log.info(f"  → {dip_ticker} 權重 = {total_dip_alloc*100:.0f}%")
            return {dip_ticker: total_dip_alloc}, sig_date_str
        return {}, sig_date_str

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
        kept = [t for t in current_sp500_holdings
                if t != sp_cfg.DIP_BUY_TICKER]
        log.info(f"  non-rotation week → re-weight kept holdings: {kept}")
        save_state(account_name, state)
        if not kept:
            return {}, sig_date_str
        metrics = [selector.calculate_metrics(t, signal_date, sp_cfg.LOOKBACK)
                   for t in kept]
        metrics = [m for m in metrics if m and m.get("atr_pct", 0) > 0]
        if not metrics:
            return {}, sig_date_str
        inv_sum = sum(1 / m["atr_pct"] for m in metrics)
        wgt = {m["ticker"]: (1 / m["atr_pct"]) / inv_sum for m in metrics}
        return wgt, sig_date_str

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
    non_dip_holdings = [t for t in current_sp500_holdings
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
        save_state(account_name, state)
        return {}, sig_date_str

    metrics = []
    for t in final_tickers:
        m = next((x for x in ranked if x["ticker"] == t), None) or \
            selector.calculate_metrics(t, signal_date, sp_cfg.LOOKBACK)
        if m and m.get("atr_pct", 0) > 0:
            metrics.append(m)

    if not metrics:
        log.info("  SP500: no valid metrics for weight calculation")
        save_state(account_name, state)
        return {}, sig_date_str

    inv_sum = sum(1 / m["atr_pct"] for m in metrics)
    wgt = {m["ticker"]: (1 / m["atr_pct"]) / inv_sum for m in metrics}

    log.info(f"  SP500 target weights (ATR inverse):")
    for sym, w in sorted(wgt.items(), key=lambda x: -x[1]):
        log.info(f"    {sym}: {w*100:.1f}%")

    save_state(account_name, state)
    return wgt, sig_date_str


# ── SP500 Universe helper（常被 callers 重複實作）─────────────────────────

def get_sp500_universe() -> set[str]:
    """
    回傳當前 S&P 500 成分股清單 + SPY/SSO（熊市 dip-buy 標的）。

    用於 callers 分類 IB 持倉歸屬哪個策略。
    """
    from strategies.sp500_momentum.selection import SelectionEngine
    from strategies.sp500_momentum import config as sp_cfg, utils
    selector = SelectionEngine()
    spy_df = utils.load_benchmark(sp_cfg.ETF_DIR / "SPY.csv")
    universe = set(selector.get_constituents(spy_df.index[-1]))
    universe |= {'SPY', 'SSO'}
    return universe


# ── 供 daily risk check 使用的 low-level helpers ─────────────────────────
#
# sp500_daily_check.py 需要做 stop loss / gap exit / dip-buy 觸發等
# 「日度風控」檢查 — 這些邏輯跟「計算目標權重」不同，需要：
#   - 單一 ticker 的歷史 Close/Open（用於上一日跌破成本、gap 判斷）
#   - 最新的市場 regime state（SPY 跌破 MA200 觸發 dip-buy）
# 這些不放在「signal」層級，但為了讓 daily check 不用直接 import
# SelectionEngine / MarketRegime 等內部模組，在這裡包成 helper。

def get_ticker_history(ticker: str):
    """
    回傳單一 ticker 的完整歷史 DataFrame（含 Open/High/Low/Close/Volume）。

    用途：daily_check 的停損、gap exit 檢查。
    """
    from strategies.sp500_momentum.selection import SelectionEngine
    selector = SelectionEngine()
    # _get_ticker_data 是 SelectionEngine 的內部 method，這裡包一層避免 callers
    # 直接碰 private API
    return selector._get_ticker_data(ticker)


def get_market_regime_state(signal_date=None) -> dict:
    """
    回傳 MarketRegime 對指定日期的 state dict
    （含 SPY_Close / SPY_MA200 / SPY_DD / SSO_Close 等）。

    signal_date=None 時使用最新一筆資料。
    """
    from strategies.sp500_momentum.market_regime import MarketRegime
    from strategies.sp500_momentum import config as sp_cfg, utils

    spy_df = utils.load_benchmark(sp_cfg.ETF_DIR / "SPY.csv")
    sso_df = utils.load_benchmark(sp_cfg.ETF_DIR / "SSO.csv")
    regime = MarketRegime(spy_df, sso_df)

    if signal_date is None:
        signal_date = spy_df.index[-1]
    return regime.get_state(signal_date) or {}
