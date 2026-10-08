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
# 這個 module 可能被 Panel → portfolio_snapshot → signals 這條路徑第一個用到，
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
                      account_name: str, *, persist: bool = True) -> tuple[dict, str]:
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
    persist : bool
        False = 只讀 state、不寫回

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
    from strategies.sp500_momentum.signal import get_latest_weights_live
    from sp500_state import load_state, save_state

    # Bridge：load state（per-account I/O 屬 automation 層，認得 Account）
    #   → 純策略函式（strategies/，不認得 Account）→ save state。
    # 散落在舊實作的 6 個 save_state 收斂成這裡「1 次 load、1 次 save」。
    state = load_state(account_name)
    wgt, signal_date, new_state = get_latest_weights_live(
        current_sp500_holdings, state)
    if persist:
        save_state(account_name, new_state)

    sig_date_str = str(signal_date.date())
    log.info(f"SP500 signal date: {sig_date_str}  "
             f"holdings={current_sp500_holdings or '(none)'}")
    if wgt:
        for sym, w in sorted(wgt.items(), key=lambda x: -x[1]):
            log.info(f"  SP500 {sym}: {w*100:.1f}%")
    return wgt, sig_date_str


# ── Sector Skew LS ────────────────────────────────────────────────────────

def get_skewls_weights(as_of_date: Optional[pd.Timestamp] = None
                        ) -> tuple[dict, str]:
    """
    取 Sector Skew LS 最新目標權重（live 模式）。

    回傳 ticker-level signed weights：
      - Long sector ETF (XLK / XLF / ...): 正權重
      - Long inverse ETF (REW / SEF / ...): 正權重 (= 對 sector short 的 proxy)
      - Direct short XLC: 負權重

    **不套 threshold gating** — caller (rebalance_ib) 對照 `skewls_state_{account}.json`
    state file 判斷是否觸發 rebalance (delta > REBAL_THRESHOLD)。

    Parameters
    ----------
    as_of_date : pd.Timestamp | None
        模擬「在某個歷史日期計算訊號」用，預設 None = 用最新可用資料。
        主要供 sync_position 和 unit test 使用。

    Returns
    -------
    (weights_dict, signal_date_str)
        weights_dict: {symbol: signed_weight}
            正值 = 多單 (sector ETF 或 inverse ETF)
            負值 = 空單 (XLC direct short)
        signal_date_str: 'YYYY-MM-DD' (= 用來算訊號的最後一筆 close 日期)

    Notes
    -----
    當訊號未成熟 (rolling window 不足) 回傳空 dict，caller 應視為 "no-op"。
    """
    from strategies.sector_skew_ls.signal import get_latest_weights_live

    wgt, sig_date = get_latest_weights_live(as_of_date=as_of_date)

    log.info(f"Skew LS signal date: {sig_date.date()}")
    if not wgt:
        log.warning("  Skew LS: signal not ready (insufficient history)")
    else:
        for sym, w in sorted(wgt.items(), key=lambda x: -abs(x[1])):
            direction = "SHORT" if w < 0 else "LONG "
            log.info(f"  Skew {direction} {sym}: {w*100:+5.1f}%")

    return wgt, str(sig_date.date())


# ── SP500 Universe helper（常被 callers 重複實作）─────────────────────────

def get_sp500_universe(historical: bool = True) -> set[str]:
    """
    回傳 S&P 500 成分股清單（含 SPY/SSO 熊市 dip-buy 標的）。

    Parameters
    ----------
    historical : bool, default True
        True  → 所有歷史日期的成分股聯集（涵蓋曾經在 SP500 但已被踢出的股票，
                例如 STX 2018 被移除、WDC 2024 被移除）。用於分類 IB 持倉。
        False → 只取最新日期的當前成分股清單。用於 SP500 strategy 選股。

    用於 callers 分類 IB 持倉歸屬：歷史聯集確保使用者過去由 SP500 strategy 買進、
    現在仍持有但已不在當前 SP500 的股票會被正確歸入「Stock Holdings (MOM)」
    bucket，而非誤判為「Other」。
    """
    from strategies.sp500_momentum.selection import SelectionEngine
    from strategies.sp500_momentum import config as sp_cfg, utils
    selector = SelectionEngine()

    universe: set[str] = set()
    df = selector.constituents_df
    if historical and not df.empty:
        # 聯集所有歷史日期的成分股
        for val in df.values.flatten():
            if isinstance(val, str):
                universe.add(val.replace(".txt", "").strip())
    else:
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

    # MarketRegime needs both benchmark frames to construct its state.  The
    # CSV loader uses None as its missing/malformed sentinel; convert that
    # sentinel into the existing empty-state contract so callers can halt
    # safely instead of leaking AttributeError/TypeError from the constructor.
    invalid = []
    for label, frame in (("SPY", spy_df), ("SSO", sso_df)):
        if frame is None or frame.empty or "Close" not in frame.columns:
            invalid.append(label)
    if invalid:
        log.error("Market regime benchmark unavailable: %s", ", ".join(invalid))
        return {}

    regime = MarketRegime(spy_df, sso_df)

    if signal_date is None:
        signal_date = spy_df.index[-1]
    return regime.get_state(signal_date) or {}
