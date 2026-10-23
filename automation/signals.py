"""
QuantCore — [[Strategy]] 訊號來源（#232；前身是 S7 的訊號統一入口）

「某個 Strategy 某天的權重與訊號日期」只有一個問法：StrategySource。

  sr()                          → Sector Rotation 目標權重
  sp500(holdings, account_name) → SP500 Momentum 目標權重＋這次訊號之後的 state
  skewls()                      → Sector Skew LS 目標權重（ticker-level、有正負）
  sp500_universe(historical)    → [[Universe]]（分類 IB 持倉用）
  market_regime(signal_date)    → SPY／MA200／DD（16:00 日度風控）

LiveSource 從本地 data CSV 算（21:00 調倉、16:00 日度風控、Sync Pos、接管、預演）；
測試給自己的 fake，不必 monkeypatch 本 module。Caller 不 import 策略內部模組
（SelectionEngine / MarketRegime / data_loader / config）。

三個 Strategy 的訊號日期語意不同，並排寫在這裡（as_of=None 時「今天」＝真實今天）：

  SR      上一個排程調倉日（週二，含今天）之前的最後收盤 — 任何一天 Sync 都拿到
          與上次週二排程相同的權重
  SP500   上一個排程調倉日（週三，含今天）之前的最後收盤 — 同上
  SkewLS  最後一筆收盤（每日 threshold 檢查，不截斷）

SP500 的 state（bull counter／dip state）在這裡讀；寫不寫由 caller 決定
（plan_inputs 的 persist_state），所以 sp500() 只回傳新 state、不落地。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Protocol

import pandas as pd

import _repo_root  # noqa: F401  strategies 套件在 repo 根目錄

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StrategySignal:
    """一個 Strategy 的訊號：權重、訊號日期，SP500 另帶這次訊號之後的 state。"""
    weights: dict[str, float]
    signal_date: str                 # 'YYYY-MM-DD'
    state: Optional[dict] = None     # SP500：caller 決定要不要寫回 state 檔


class StrategySource(Protocol):
    """三個 Strategy 的訊號，與分類持倉、日度風控需要的市場資料。"""

    def sr(self) -> StrategySignal: ...
    def sp500(self, holdings: list[str], account_name: str) -> StrategySignal: ...
    def skewls(self) -> StrategySignal: ...
    def sp500_universe(self, historical: bool = True) -> set[str]: ...
    def market_regime(self, signal_date=None) -> dict: ...


class LiveSource:
    """本地 data CSV 算出的訊號。一次執行（一次排程、一次 Sync Pos／接管／預演）用
    同一個：SelectionEngine 只建一次，sp500_universe() 與各帳戶的 sp500() 共用；
    market_regime() 的 SPY／SSO MarketRegime 也只建一次（sp500() 內的牛熊判斷仍由
    策略函式自己讀，沒有改）。

    as_of：以這天當「今天」推算三個 Strategy 的訊號日期；None ＝ 真實今天。
    只影響 sr()／sp500()／skewls()：sp500_universe() 永遠是最新成分股，
    market_regime() 沒給日期時用 SPY 最後一筆。
    """

    def __init__(self, as_of: Optional[pd.Timestamp] = None):
        self.as_of = as_of
        self._selector = None
        self._regime = None

    # ── Sector Rotation ──────────────────────────────────────────────

    def sr(self) -> StrategySignal:
        """SR 目標權重（live 模式：不套 rebalance mask／forward-fill，避免週二盤前
        拿到上週舊訊號；等同回測中「今天是 rebalance day」時的 shift(1) 結果）。"""
        from strategies.sector_rotation.data_loader import load_data
        from strategies.sector_rotation.signal import get_latest_weights_live

        data = load_data()
        etf_close = data['etf_close']
        weights, sig_date = get_latest_weights_live(etf_close, as_of_date=self.as_of)

        wgt = {sym: float(w) for sym, w in weights.items() if w > 0.001}
        log.info(f"SR signal date: {sig_date.date()}  (live, skip rebal mask)")
        for sym, w in sorted(wgt.items(), key=lambda x: -x[1]):
            log.info(f"  SR  {sym}: {w*100:.0f}%")
        return StrategySignal(wgt, str(sig_date.date()))

    # ── SP500 Momentum ───────────────────────────────────────────────

    def sp500(self, holdings: list[str], account_name: str) -> StrategySignal:
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

        holdings：IB 帳戶中屬於 SP500 策略的股票代碼（不含 SR ETFs）。
        account_name：讀 state file 用。回傳的 state 由 caller 決定要不要寫回。

        回傳空權重可能是：熊市未滿足 dip-buy 條件、或牛市尚未確認且 SPY ≥ MA200
        （holding pattern）。
        """
        from strategies.sp500_momentum.signal import get_latest_weights_live
        from sp500_state import load_state

        # Bridge：load state（per-account I/O 屬 automation 層，認得 Account）
        #   → 純策略函式（strategies/，不認得 Account）→ 新 state 交給 caller。
        state = load_state(account_name)
        wgt, signal_date, new_state = get_latest_weights_live(
            holdings, state, as_of_date=self.as_of, selector=self._selection())

        sig_date_str = str(signal_date.date())
        log.info(f"SP500 signal date: {sig_date_str}  "
                 f"holdings={holdings or '(none)'}")
        if wgt:
            for sym, w in sorted(wgt.items(), key=lambda x: -x[1]):
                log.info(f"  SP500 {sym}: {w*100:.1f}%")
        return StrategySignal(wgt, sig_date_str, new_state)

    # ── Sector Skew LS ───────────────────────────────────────────────

    def skewls(self) -> StrategySignal:
        """
        SkewLS 目標權重（ticker-level、有正負）：
          - Long sector ETF (XLK / XLF / ...): 正權重
          - Long inverse ETF (REW / SEF / ...): 正權重 (= 對 sector short 的 proxy)
          - Direct short XLC: 負權重

        **不套 threshold gating** — caller 對照 `skewls_state_{account}.json` 判斷
        是否觸發 rebalance (delta > REBAL_THRESHOLD)。訊號未成熟（rolling window
        不足）時權重為空，caller 視為 no-op。
        """
        from strategies.sector_skew_ls.signal import get_latest_weights_live

        wgt, sig_date = get_latest_weights_live(as_of_date=self.as_of)

        log.info(f"Skew LS signal date: {sig_date.date()}")
        if not wgt:
            log.warning("  Skew LS: signal not ready (insufficient history)")
        else:
            for sym, w in sorted(wgt.items(), key=lambda x: -abs(x[1])):
                direction = "SHORT" if w < 0 else "LONG "
                log.info(f"  Skew {direction} {sym}: {w*100:+5.1f}%")

        return StrategySignal(wgt, str(sig_date.date()))

    # ── SP500 Universe ───────────────────────────────────────────────

    def sp500_universe(self, historical: bool = True) -> set[str]:
        """
        S&P 500 成分股清單（含 SPY/SSO 熊市 dip-buy 標的）。

        historical=True  → 所有歷史日期的成分股聯集（涵蓋曾經在 SP500 但已被踢出的
                           股票，例如 STX 2018 被移除、WDC 2024 被移除）。用於分類 IB 持倉：
                           過去由 SP500 strategy 買進、現在仍持有但已不在當前 SP500 的
                           股票才會歸入「Stock Holdings (MOM)」，而非誤判為「Other」。
        historical=False → 只取最新日期的當前成分股清單。
        """
        from strategies.sp500_momentum import config as sp_cfg, utils
        selector = self._selection()

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

    # ── 日度風控的市場狀態 ─────────────────────────────────────────────

    def market_regime(self, signal_date=None) -> dict:
        """
        MarketRegime 對指定日期的 state dict（含 SPY_Close / SPY_MA200 / SPY_DD /
        SSO_Close 等）；signal_date=None 時用最新一筆資料。SPY／SSO 讀不到時回空 dict。
        """
        loaded = self._market_regime()
        if loaded is None:
            return {}
        regime, spy_last = loaded
        if signal_date is None:
            signal_date = spy_last
        return regime.get_state(signal_date) or {}

    # ── 一次執行內共用 ────────────────────────────────────────────────

    def _selection(self):
        if self._selector is None:
            from strategies.sp500_momentum.selection import SelectionEngine
            self._selector = SelectionEngine()
        return self._selector

    def _market_regime(self):
        """(MarketRegime, SPY 最後一筆日期)；SPY／SSO 讀不到時 None（下次再讀）。"""
        if self._regime is None:
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
                return None
            self._regime = (MarketRegime(spy_df, sso_df), spy_df.index[-1])
        return self._regime
