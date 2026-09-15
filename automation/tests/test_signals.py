"""
Integration tests for signals.py.

These tests depend on real data files (SP500 constituents, SPY/SSO CSVs, ETF
price data). On machines without that data, they skip gracefully.

Coverage:
  - get_sr_weights() gross within design bounds (≤1.0; WEIGHT_STEP 取整殘差留現金)
  - get_sr_weights() non-empty, or verified all-cash (z-threshold 空手為合法輸出)
  - get_sp500_universe() contains SPY and SSO (dip-buy tickers)
  - get_market_regime_state() returns dict with required keys
  - SR_ETFS constant is single source of truth across modules
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest


def _has_sp500_data() -> bool:
    """Check if the SP500 constituents + ETF data files exist locally."""
    root = Path(__file__).resolve().parent.parent.parent
    ref = root / "data" / "reference" / "sp500_constituents_daily.xlsx"
    spy = root / "data" / "etfs" / "SPY.csv"
    return ref.exists() and spy.exists()


skip_no_data = pytest.mark.skipif(
    not _has_sp500_data(),
    reason="SP500 constituents / ETF data files missing",
)


# ── Pure (no data) tests ─────────────────────────────────────────────

def test_sr_etfs_constant_is_11_sector_etfs():
    """SR_ETFS should contain exactly the 11 SPDR sector ETFs."""
    from ib_fetcher import SR_ETFS
    expected = {
        'XLK', 'XLF', 'XLV', 'XLE', 'XLI', 'XLY', 'XLB',
        'XLP', 'XLU', 'XLC', 'XLRE',
    }
    assert SR_ETFS == expected


def test_sr_etfs_is_single_source_across_modules():
    """rebalance_ib should re-export ib_fetcher.SR_ETFS.

    daily_report 自 #26 起不再分類持倉（持倉報告不推播），已不引用 SR_ETFS。
    """
    import ib_fetcher
    import rebalance_ib

    assert rebalance_ib.SR_ETFS is ib_fetcher.SR_ETFS


# ── Data-dependent tests ─────────────────────────────────────────────

@skip_no_data
def test_get_sr_weights_non_empty_or_verified_all_cash():
    """SR 有持倉，或空手時 z-threshold 空手前置條件確實成立。

    設計（CONTEXT.md [[Strategy]] 詞條；strategies/sector_rotation/signal.py）：
    z-score 全數未過 MOM_ZSCORE_THRESH 時空手（空 dict）是**合法輸出** —
    11.5 年回測中 7%（203/2891）的交易日如此，多為熊市。斷言「至少 1 檔」
    會在熊市空手 regime 常態性失敗（同類問題見 gross bounds 測試）。
    空 dict 時改驗證前置條件「無 ETF 同時 z > thresh 且 score > 0」，
    以區分設計內空手與資料／管線故障造成的意外空輸出。
    """
    from signals import get_sr_weights

    wgt, sig_date = get_sr_weights()
    assert isinstance(wgt, dict)
    assert sig_date  # non-empty string

    if wgt:
        # 有訊號日：至少 1 檔、權重皆為正（原斷言語義）
        assert all(w > 0 for w in wgt.values())
        return

    # ── 空手：獨立重算 signal_date 當日橫截面 z-score，驗證確實無 ETF 過門檻 ──
    import pandas as pd
    from strategies.sector_rotation import config as sr_config
    from strategies.sector_rotation.data_loader import load_data
    from strategies.sector_rotation.signal import compute_mom_score

    etf_close = load_data()["etf_close"]
    trimmed = etf_close[etf_close.index <= pd.Timestamp(sig_date)]
    assert len(trimmed) > 0, f"signal_date {sig_date} 之前無資料 — 資料載入異常"
    assert str(trimmed.index[-1].date()) == sig_date, \
        f"資料最後一列 {trimmed.index[-1].date()} ≠ signal_date {sig_date}"

    score = compute_mom_score(trimmed).iloc[-1]
    std = score.std()  # ddof=1，與 signal.py 的 score.std(axis=1) 同源
    z = (score - score.mean()) / (std if std else float("nan"))
    passing = [sym for sym in score.index
               if z[sym] > sr_config.MOM_ZSCORE_THRESH and score[sym] > 0]
    assert not passing, (
        f"SR 回傳空手，但 {passing} z-score 過門檻"
        f"（thresh={sr_config.MOM_ZSCORE_THRESH}）且動能分數 > 0 — "
        f"空輸出不是設計內空手，疑似訊號管線故障"
    )


@skip_no_data
def test_get_sr_weights_gross_within_design_bounds():
    """SR gross sum 落在設計界限內：≤ 1.0，且 ≥ 1 − n×(WEIGHT_STEP/2)。

    設計（strategies/sector_rotation/signal.py，回測同源）：
      1. 過 z-threshold 的 ETF 按分數比例 normalize（raw sum = 1.0）
      2. 各權重四捨五入到 WEIGHT_STEP（5%）
      3. 取整後 >100% 會被強制降回；<100% 的殘差**刻意留作現金**
    因此 gross 不是恆等於 1.0 — 歷史上 13% 的交易日 gross = 0.95
    （例：2026-06-29 raw 0.506/0.274/0.220 三檔同時向下取整）。
    全數未過 z-threshold 時空手（空 dict）也是合法輸出。
    """
    from signals import get_sr_weights
    from strategies.sector_rotation import config as sr_config

    wgt, _ = get_sr_weights()
    total = sum(wgt.values())
    step = sr_config.WEIGHT_STEP

    assert total <= 1.0 + 1e-9, \
        f"SR gross must never exceed 1.0, got {total:.4f}"

    if wgt:  # 空手（全現金）是 z-threshold 下的合法輸出
        lower = 1.0 - len(wgt) * step / 2
        assert total >= lower - 1e-9, \
            (f"SR gross {total:.4f} below rounding floor {lower:.4f} "
             f"(n={len(wgt)}, step={step}) — 超出取整殘差能解釋的範圍")
        for sym, w in wgt.items():
            assert abs(w / step - round(w / step)) < 1e-6, \
                f"{sym} weight {w} is not a multiple of WEIGHT_STEP {step}"


@skip_no_data
def test_get_sr_weights_all_are_sector_etfs():
    """Every SR position should be one of the 11 sector ETFs."""
    from signals import get_sr_weights
    from ib_fetcher import SR_ETFS

    wgt, _ = get_sr_weights()
    for sym in wgt:
        assert sym in SR_ETFS, f"{sym} is not a known sector ETF"


@skip_no_data
def test_get_sp500_universe_contains_spy_sso():
    """The universe helper must include SPY + SSO for dip-buy classification."""
    from signals import get_sp500_universe

    universe = get_sp500_universe()
    assert isinstance(universe, set)
    assert "SPY" in universe
    assert "SSO" in universe


@skip_no_data
def test_get_sp500_universe_has_500_plus_tickers():
    """S&P 500 should have ~500 constituents (may vary ±10 for rebalance weeks)."""
    from signals import get_sp500_universe

    universe = get_sp500_universe()
    assert len(universe) > 400, f"SP500 universe too small: {len(universe)}"


@skip_no_data
def test_get_market_regime_state_has_required_keys():
    """MarketRegime state dict must expose SPY close / MA200 / DD / ATH."""
    from signals import get_market_regime_state

    state = get_market_regime_state()
    assert isinstance(state, dict)
    required_keys = {"SPY_Close", "SPY_MA200", "SPY_DD"}
    missing = required_keys - set(state.keys())
    assert not missing, f"Missing keys in regime state: {missing}"


@skip_no_data
def test_get_market_regime_numeric_values():
    """SPY_Close and SPY_MA200 should be positive floats."""
    from signals import get_market_regime_state

    state = get_market_regime_state()
    assert state["SPY_Close"] > 0
    assert state["SPY_MA200"] > 0
    # DD is negative or zero (drawdown from ATH)
    assert state["SPY_DD"] <= 0.001  # small epsilon
