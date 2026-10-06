"""
Unit tests for sp500_daily_check helpers + related logging/report fixes.

Scope note (ADR-0004): the fixed 20% stop-loss and 50% gap-exit were removed —
8-year backtest showed the stop added nothing (and the gap never fired), and
they created a live-only stop→stale-rebuy whipsaw. So `evaluate_exits` is gone;
daily_check now only does the bear dip-buy check. What remains here:

  - _prev_trading_day  : staleness guard helper (still used for dip-buy freshness)
  - daily_report mark_at_reference_close : EOD mark = CSV close, IB fallback (ADR-0003)
  - logging: import must not hijack root; quiet_ib_console keeps INFO file logs

No IB Gateway, no network.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace as NS

import pytest


# ── staleness guard helper ──────────────────────────────────────────

def test_prev_trading_day_monday_returns_friday():
    from sp500_daily_check import _prev_trading_day
    assert _prev_trading_day(date(2026, 6, 8)) == date(2026, 6, 5)  # Mon → Fri


def test_prev_trading_day_skips_weekend_and_juneteenth():
    # Mon 2026-06-22: back past Sun/Sat and Fri 6/19 (Juneteenth) → Thu 6/18
    from sp500_daily_check import _prev_trading_day
    assert _prev_trading_day(date(2026, 6, 22)) == date(2026, 6, 18)


# ── 失敗要發 ❌（#38：以前連不上 IB、讀不到資料、例外都只寫 log）─────────
# 06-15、08-10、08-17、08-24、08-26 16:00 三帳戶都連不上 IB，沒有任何通知。

class _Tue0915(date):
    @classmethod
    def today(cls):
        return date(2026, 9, 15)


@pytest.fixture
def daily_check_env(fake_config, monkeypatch):
    """跑到連 IB 為止的真實流程：SPY 資料、市場狀態用假的，連 IB 一律被拒。"""
    pytest.importorskip("ib_insync")
    import pandas as pd
    import ib_fetcher
    import signals
    import sp500_daily_check
    from strategies.sp500_momentum import utils

    env = NS(
        spy=pd.DataFrame({"Close": [750.0, 757.3]},
                         index=pd.to_datetime(["2026-09-11", "2026-09-14"])),
        regime={"SPY_Close": 757.3, "SPY_MA200": 714.5, "SPY_DD": -0.022},
    )
    monkeypatch.setattr(sp500_daily_check, "date", _Tue0915)
    monkeypatch.setattr(utils, "load_benchmark", lambda path: env.spy)
    monkeypatch.setattr(signals, "get_market_regime_state", lambda ref: env.regime)

    def refused(ctx):
        raise ConnectionRefusedError("[WinError 1225] <refused>")

    monkeypatch.setattr(ib_fetcher, "connect", refused)
    return env


def _check(name="Taylor"):
    import sp500_daily_check
    from notify import Digest
    d = Digest("日度風控 2026-09-15", icon="📤")
    sp500_daily_check.run_daily_check(name, d)
    return d


def test_ib_connect_failure_is_reported(daily_check_env):
    assert _check().render() == (
        "<b>❌ 日度風控 2026-09-15</b>\n\n"
        "<b>Taylor</b> ❌ IB 連線失敗\n[WinError 1225] &lt;refused&gt;")


def test_missing_spy_history_is_reported(daily_check_env):
    daily_check_env.spy = daily_check_env.spy.iloc[-1:]
    assert ("<b>Taylor</b> ❌ 日度風控失敗\nSPY 資料不足，本次沒有做 dip-buy 檢查"
            in _check().render())


def test_unreadable_spy_data_is_reported_without_none_type_error(daily_check_env):
    """A missing/unreadable benchmark must halt with an actionable alert."""
    daily_check_env.spy = None
    rendered = _check().render()
    assert ("<b>Taylor</b> ❌ 日度風控失敗\n"
            "SPY 資料不可用（檔案不存在或格式錯誤），本次沒有做 dip-buy 檢查"
            in rendered)
    assert "NoneType" not in rendered


def test_missing_market_regime_is_reported(daily_check_env):
    daily_check_env.regime = {}
    assert ("<b>Taylor</b> ❌ 日度風控失敗\n讀不到市場狀態資料（SPY / MA200），本次沒有做 dip-buy 檢查"
            in _check().render())


def test_nyse_holiday_stays_silent(daily_check_env, monkeypatch):
    import sp500_daily_check

    class _LaborDay(date):
        @classmethod
        def today(cls):
            return date(2026, 9, 7)

    monkeypatch.setattr(sp500_daily_check, "date", _LaborDay)
    assert _check().render() is None


def test_run_all_exception_is_reported_and_continues(fake_config, monkeypatch):
    import notify
    import sp500_daily_check
    from notify import Digest
    sent, ran = [], []
    monkeypatch.setattr(notify, "tg_send",
                        lambda msg, *, token, chat_id: sent.append((msg, token, chat_id)))

    def fake_check(name, digest):
        ran.append(name)
        if name == "Rosa":
            raise RuntimeError("state <broken>")

    monkeypatch.setattr(sp500_daily_check, "run_daily_check", fake_check)
    d = Digest("日度風控 2026-09-15", icon="📤")
    sp500_daily_check.run_all(["Taylor", "Rosa", "Yvonne"], d)
    assert ran == ["Taylor", "Rosa", "Yvonne"]
    assert sent == [("<b>❌ 日度風控 2026-09-15</b>\n\n<b>Rosa</b> ❌ 日度風控失敗\nstate &lt;broken&gt;",
                     "FAKE_TOKEN", "FAKE_CHAT")]
    assert d.outcomes(["Taylor", "Rosa"]) == {"Taylor": ("ok", None),
                                               "Rosa": ("error", "❌ 日度風控失敗")}


# ── 熊市 dip-buy 的限價：Broker 的送單報價＋統一限價規則（#185 第三階段）──────

@pytest.fixture
def dip_buy(daily_check_env, fake_config, monkeypatch, tmp_path):
    """SPY 跌 16%、在 MA200 下：觸發第一階 dip-buy，真的送單到 in-memory FakeBroker。"""
    import account_utils
    import ib_fetcher
    import rebalance_ib
    import signals
    import sp500_state
    from tests.fake_broker import FakeBroker

    base, _accounts = fake_config
    daily_check_env.regime = {"SPY_Close": 600.0, "SPY_MA200": 700.0, "SPY_DD": -0.16}
    monkeypatch.setattr(account_utils, "load_base_config",
                        lambda: {**base, "dry_run": False})
    monkeypatch.setattr(sp500_state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(signals, "get_sp500_universe", lambda: {"AAPL"})
    monkeypatch.setattr(rebalance_ib.time, "sleep", lambda _s: None)
    fake = FakeBroker(managed=["U17800779"], positions={"AAPL": 10.0}, price=500.0)
    monkeypatch.setattr(ib_fetcher, "connect", lambda ctx: fake.connect(ctx))
    return fake


def _dip_limit(fake):
    _check()
    [order] = fake.placed
    assert (order.symbol, order.action, order.order_type) == ("SPY", "BUY", "LMT")
    return order.lmt


def test_dip_buy_limit_is_ask_times_1_003(dip_buy):
    assert _dip_limit(dip_buy) == round(100.1 * 1.003, 2)


def test_dip_buy_without_bid_or_ask_falls_back_to_mid_then_last_then_close(dip_buy):
    dip_buy.quote = {"bid": None, "ask": None, "mid": None, "last": 99.0, "close": 98.0}
    assert _dip_limit(dip_buy) == round(99.0 * 1.003, 2)


def test_dip_buy_without_any_quote_uses_the_reference_price(dip_buy):
    dip_buy.quote = {"bid": None, "ask": None, "mid": None, "last": None, "close": None}
    assert _dip_limit(dip_buy) == round(500.0 * 1.003, 2)


# ── #2: daily_report marks at Reference Close, IB only as fallback ──

def test_daily_report_marks_at_reference_close_then_ib_fallback(monkeypatch):
    pytest.importorskip("ib_insync")
    import price_utils
    import daily_report
    # CIEN present in CSV (official close), STX absent → must fall back to IB
    monkeypatch.setattr(price_utils, "load_csv_close",
                        lambda s: {"CIEN": 488.21}.get(s, 0.0))
    asked = []
    conn = NS(prices=lambda syms: asked.append(list(syms)) or {"STX": 912.0})
    marks = daily_report.mark_at_reference_close(conn, ["CIEN", "STX"])
    assert asked == [["STX"]]         # 只有 CSV 缺的才問 IB
    assert marks["CIEN"] == 488.21    # official close, NOT a stale weekend tick
    assert marks["STX"] == 912.0      # CSV missing → IB fallback


# ── #3: logging — library import must not hijack the root logger ────

def test_importing_rebalance_ib_does_not_hijack_root_logger():
    import logging
    import importlib
    pytest.importorskip("ib_insync")
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    saved = root.handlers[:]
    root.handlers[:] = [sentinel]
    try:
        import rebalance_ib
        importlib.reload(rebalance_ib)   # re-run module-level code
        assert sentinel in root.handlers, "import hijacked root logger"
        assert not any(isinstance(h, logging.FileHandler) for h in root.handlers), \
            "import added a FileHandler to root"
    finally:
        root.handlers[:] = saved


def test_quiet_ib_console_preserves_info_file_logging(tmp_path):
    """quiet_ib_console must NOT suppress INFO in files (regression 2026-06-08).

    ib_insync.util.logToConsole(WARNING) lowers root to WARNING; the helper
    restores INFO so daily_report / moo_fill / rebalance keep full audit logs.
    """
    import logging
    pytest.importorskip("ib_insync")
    from log_setup import quiet_ib_console
    logf = tmp_path / "t.log"
    saved = logging.root.handlers[:]
    saved_level = logging.root.level
    try:
        logging.basicConfig(level=logging.INFO,
                            handlers=[logging.FileHandler(logf, encoding="utf-8")],
                            force=True)
        quiet_ib_console()
        assert logging.root.level == logging.INFO
        logging.getLogger("audit").info("position-dump-should-be-logged")
        for h in logging.root.handlers:
            h.flush()
        assert "position-dump-should-be-logged" in logf.read_text(encoding="utf-8")
    finally:
        for h in logging.root.handlers:
            try:
                h.close()
            except Exception:
                pass
        logging.root.handlers[:] = saved
        logging.root.setLevel(saved_level)
