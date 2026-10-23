"""
Tests for signals.py（[[Strategy]] 訊號來源，#232）.

Data-dependent tests use the real data files (SP500 constituents, SPY/SSO CSVs,
ETF price data); on machines without that data they skip gracefully.

Coverage:
  - LiveSource().sr() gross within design bounds (≤1.0; WEIGHT_STEP 取整殘差留現金)
  - LiveSource().sr() non-empty, or verified all-cash (z-threshold 空手為合法輸出)
  - 三個 Strategy 的訊號日期語意（SR 週二、SP500 週三、SkewLS 最後收盤）
  - SP500 state bridge：讀一次、不寫（寫不寫由 caller 決定）；選股引擎一次執行只建一次
  - sp500_universe() contains SPY and SSO (dip-buy tickers)
  - market_regime() returns dict with required keys
  - Sector ETF list (strategy_owner.SECTOR_ETFS) is the 11 SPDR sector ETFs
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
    from strategy_owner import SECTOR_ETFS as SR_ETFS
    expected = {
        'XLK', 'XLF', 'XLV', 'XLE', 'XLI', 'XLY', 'XLB',
        'XLP', 'XLU', 'XLC', 'XLRE',
    }
    assert SR_ETFS == expected


def test_market_regime_missing_benchmark_fails_closed(monkeypatch):
    """Missing SSO must return no state, not leak a NoneType exception."""
    import pandas as pd
    import signals
    from strategies.sp500_momentum import utils

    spy = pd.DataFrame(
        {"Close": [100.0, 101.0]},
        index=pd.to_datetime(["2026-09-21", "2026-09-22"]),
    )

    def fake_load(path):
        return None if Path(path).name == "SSO.csv" else spy

    monkeypatch.setattr(utils, "load_benchmark", fake_load)
    assert signals.LiveSource().market_regime(pd.Timestamp("2026-09-22")) == {}


# ── Data-dependent tests ─────────────────────────────────────────────

@skip_no_data
def test_sr_non_empty_or_verified_all_cash():
    """SR 有持倉，或空手時 z-threshold 空手前置條件確實成立。

    設計（CONTEXT.md [[Strategy]] 詞條；strategies/sector_rotation/signal.py）：
    z-score 全數未過 MOM_ZSCORE_THRESH 時空手（空 dict）是**合法輸出** —
    11.5 年回測中 7%（203/2891）的交易日如此，多為熊市。斷言「至少 1 檔」
    會在熊市空手 regime 常態性失敗（同類問題見 gross bounds 測試）。
    空 dict 時改驗證前置條件「無 ETF 同時 z > thresh 且 score > 0」，
    以區分設計內空手與資料／管線故障造成的意外空輸出。
    """
    from signals import LiveSource

    sig = LiveSource().sr()
    wgt, sig_date = sig.weights, sig.signal_date
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
def test_sr_gross_within_design_bounds():
    """SR gross sum 落在設計界限內：≤ 1.0，且 ≥ 1 − n×(WEIGHT_STEP/2)。

    設計（strategies/sector_rotation/signal.py，回測同源）：
      1. 過 z-threshold 的 ETF 按分數比例 normalize（raw sum = 1.0）
      2. 各權重四捨五入到 WEIGHT_STEP（5%）
      3. 取整後 >100% 會被強制降回；<100% 的殘差**刻意留作現金**
    因此 gross 不是恆等於 1.0 — 歷史上 13% 的交易日 gross = 0.95
    （例：2026-06-29 raw 0.506/0.274/0.220 三檔同時向下取整）。
    全數未過 z-threshold 時空手（空 dict）也是合法輸出。
    """
    from signals import LiveSource
    from strategies.sector_rotation import config as sr_config

    wgt = LiveSource().sr().weights
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
def test_sr_all_are_sector_etfs():
    """Every SR position should be one of the 11 sector ETFs."""
    from signals import LiveSource
    from strategy_owner import SECTOR_ETFS as SR_ETFS

    wgt = LiveSource().sr().weights
    for sym in wgt:
        assert sym in SR_ETFS, f"{sym} is not a known sector ETF"


@skip_no_data
def test_sp500_universe_contains_spy_sso():
    """The universe helper must include SPY + SSO for dip-buy classification."""
    from signals import LiveSource

    universe = LiveSource().sp500_universe()
    assert isinstance(universe, set)
    assert "SPY" in universe
    assert "SSO" in universe


@skip_no_data
def test_sp500_universe_has_500_plus_tickers():
    """S&P 500 should have ~500 constituents (may vary ±10 for rebalance weeks)."""
    from signals import LiveSource

    universe = LiveSource().sp500_universe()
    assert len(universe) > 400, f"SP500 universe too small: {len(universe)}"


@skip_no_data
def test_market_regime_has_required_keys():
    """MarketRegime state dict must expose SPY close / MA200 / DD / ATH."""
    from signals import LiveSource

    state = LiveSource().market_regime()
    assert isinstance(state, dict)
    required_keys = {"SPY_Close", "SPY_MA200", "SPY_DD"}
    missing = required_keys - set(state.keys())
    assert not missing, f"Missing keys in regime state: {missing}"


@skip_no_data
def test_market_regime_numeric_values():
    """SPY_Close and SPY_MA200 should be positive floats."""
    from signals import LiveSource

    state = LiveSource().market_regime()
    assert state["SPY_Close"] > 0
    assert state["SPY_MA200"] > 0
    # DD is negative or zero (drawdown from ATH)
    assert state["SPY_DD"] <= 0.001  # small epsilon


def _fake_sp500(monkeypatch, built):
    """純策略函式與選股引擎換成假的：記下拿到的 as_of 與 selector。"""
    import pandas as pd
    from strategies.sp500_momentum import selection
    from strategies.sp500_momentum import signal as sp_signal

    seen = []

    class Engine:
        constituents_df = pd.DataFrame({"2026-09-01": ["AAPL.txt", "MSFT"]})

        def __init__(self):
            built.append(self)

    def live(holdings, state, as_of_date=None, selector=None):
        seen.append((list(holdings), dict(state), as_of_date, selector))
        return {"AAPL": 1.0}, pd.Timestamp("2026-09-23"), dict(state, bull_weeks_counter=4)

    monkeypatch.setattr(selection, "SelectionEngine", Engine)
    monkeypatch.setattr(sp_signal, "get_latest_weights_live", live)
    return seen


def test_sp500_bridge_reads_state_once_and_leaves_writing_to_the_caller(monkeypatch):
    """SP500 state：讀一次、交回新 state、自己不寫（排程與 Sync 寫，預演與接管不寫，#186）。"""
    import signals
    import sp500_state

    loads, saved, built = [], [], []
    monkeypatch.setattr(sp500_state, "load_state",
                        lambda name: loads.append(name) or {"bull_weeks_counter": 3})
    monkeypatch.setattr(sp500_state, "save_state", lambda name, state: saved.append(name))
    seen = _fake_sp500(monkeypatch, built)

    sig = signals.LiveSource().sp500(["AAPL"], "Taylor")

    assert (sig.weights, sig.signal_date) == ({"AAPL": 1.0}, "2026-09-23")
    assert sig.state == {"bull_weeks_counter": 4}
    assert seen[0][:2] == (["AAPL"], {"bull_weeks_counter": 3})
    assert loads == ["Taylor"] and saved == []


def test_one_source_builds_the_selection_engine_once(monkeypatch):
    """同一個來源：Universe 與三個帳戶的 SP500 訊號共用一個選股引擎（#232）。"""
    import signals
    import sp500_state

    built = []
    monkeypatch.setattr(sp500_state, "load_state", lambda name: {})
    seen = _fake_sp500(monkeypatch, built)

    source = signals.LiveSource()
    assert source.sp500_universe() == {"AAPL", "MSFT", "SPY", "SSO"}
    for name in ("Taylor", "Rosa", "Yvonne"):
        source.sp500([], name)

    assert len(built) == 1
    assert all(s[3] is built[0] for s in seen)
    signals.LiveSource().sp500_universe()          # 新的來源（下一次執行）重建
    assert len(built) == 2


def test_as_of_reaches_all_three_strategies(monkeypatch):
    """as_of 是三個 Strategy 共同的「今天」；None ＝ 真實今天（由策略自己取）。"""
    import pandas as pd
    import signals
    import sp500_state
    from strategies.sector_rotation import data_loader as sr_data
    from strategies.sector_rotation import signal as sr_signal
    from strategies.sector_skew_ls import signal as sk_signal

    day = pd.Timestamp("2026-10-01")
    got = {}

    def sr_live(close, zscore_thresh=None, as_of_date=None):
        got["sr"] = (close, as_of_date)
        return pd.Series({"XLK": 1.0}), pd.Timestamp("2026-09-28")

    def skewls_live(as_of_date=None):
        got["skewls"] = as_of_date
        return {}, pd.Timestamp("2026-09-30")

    monkeypatch.setattr(sr_data, "load_data", lambda: {"etf_close": "closes"})
    monkeypatch.setattr(sr_signal, "get_latest_weights_live", sr_live)
    monkeypatch.setattr(sk_signal, "get_latest_weights_live", skewls_live)
    monkeypatch.setattr(sp500_state, "load_state", lambda name: {})
    seen = _fake_sp500(monkeypatch, [])

    source = signals.LiveSource(as_of=day)
    assert source.sr().signal_date == "2026-09-28"
    assert source.skewls().signal_date == "2026-09-30"
    source.sp500([], "Taylor")

    assert got == {"sr": ("closes", day), "skewls": day}
    assert seen[0][2] == day


@skip_no_data
@pytest.mark.parametrize("as_of, sr_date, sp500_date", [
    ("2026-10-01", "2026-09-28", "2026-09-29"),     # 週四：SR 截到週二前、SP500 截到週三前
    ("2026-09-29", "2026-09-28", "2026-09-22"),     # 週二當天：SR 用前一天，SP500 用上週二
    ("2026-09-30", "2026-09-28", "2026-09-29"),     # 週三當天
])
def test_signal_dates_follow_each_strategys_schedule(as_of, sr_date, sp500_date, monkeypatch,
                                                     tmp_path):
    """訊號日期語意（真資料）：SR 上一個週二之前、SP500 上一個週三之前、SkewLS 截至 as_of
    的最後收盤。"""
    import pandas as pd
    import sp500_state
    from signals import LiveSource

    monkeypatch.setattr(sp500_state, "STATE_DIR", tmp_path)
    source = LiveSource(as_of=pd.Timestamp(as_of))
    assert source.sr().signal_date == sr_date
    assert source.sp500([], "Test").signal_date == sp500_date
    assert source.skewls().signal_date == as_of          # 交易日：當天收盤就是最後一筆
