"""21:00 調倉送單後的失敗怎麼處理（#236 補考卷，行為不改）。

送單之後的 Step 9b 對帳、Step 10 SkewLS state 寫入失敗都只記下、不往外拋：
單已經送出，不能讓回報跟著消失。收尾寫交易紀錄失敗時，已經在往外拋的例外
（例如 Ctrl+C）優先，訊息照樣列出送出的單與寫入失敗。

真的 rebalance_ib.main：in-memory Broker（每檔 $100、NAV $100k）、固定訊號、
state 與紀錄都在 tmp_path。不連 IB、不發 Telegram。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace as NS

import pytest

SESSION = "2026-10-22"


@pytest.fixture
def env(monkeypatch, tmp_path):
    import account_state
    import manual_attribution as ma
    import rebalance_ib
    import strategy_orders
    from tests.fake_broker import FakeBroker, install_connect

    monkeypatch.setattr(account_state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(ma, "DEFAULT_LEDGER_PATH", tmp_path / "ledger.sqlite3")
    monkeypatch.setattr(ma, "DEFAULT_MANUAL_ORDERS_PATH", tmp_path / "orders.sqlite3")
    monkeypatch.setattr(strategy_orders.time, "sleep", lambda _s: None)
    env = NS(brokers=[], trade_log=[])
    monkeypatch.setattr(rebalance_ib, "append_trade_log",
                        lambda *a, **k: env.trade_log.append(a))

    def connect(ctx, **_kw):
        fake = FakeBroker(managed=["U00000001"], positions={"AAPL": 100.0})
        env.brokers.append(fake)
        return fake.connect(ctx)

    install_connect(monkeypatch, connect)
    return env


def _rebalance(digest=None):
    """跑一次 21:00 sp500 調倉，回傳 digest（往外拋時 caller 自己帶 digest 進來看）。"""
    import rebalance_ib
    from account_ctx import AccountCtx
    from notify import Digest
    from tests.fake_signals import FakeSource

    ctx = AccountCtx(
        name="Rosa", account="U00000001", host="127.0.0.1", port=4999, client_id=99,
        dry_run=False, order_type="MOO", outside_rth=False, wait_fill_sec=0,
        alloc_sr=0.0, alloc_sp500=1.0, alloc_skewls=0.0,
        price_feed_port=None)
    source = FakeSource(sp500=({"AAPL": 0.5, "MSFT": 0.5}, SESSION),
                        universe={"AAPL", "MSFT"})
    digest = digest if digest is not None else Digest("調倉 T", icon="📤")
    rebalance_ib.main(mode="sp500", ctx=ctx, digest=digest, source=source)
    return digest.render()


def _sent(env):
    return sorted((p.symbol, p.action, p.qty) for b in env.brokers for p in b.placed)


ORDERS = [("AAPL", "BUY", 400), ("MSFT", "BUY", 500)]


def test_drift_verification_failure_is_reported_not_raised(env, monkeypatch):
    import rebalance_ib

    def boom(*_a, **_k):
        raise RuntimeError("positions <timeout>")

    monkeypatch.setattr(rebalance_ib.plan, "verify_execution", boom)   # 只有 Step 9b 用
    text = _rebalance()

    assert _sent(env) == ORDERS
    assert len(env.trade_log) == 2                      # 交易紀錄照寫
    assert "⚠️ 對帳段失敗: positions &lt;timeout&gt;" in text
    assert "❌" not in text


def test_skewls_state_save_failure_is_logged_not_raised(env, monkeypatch, caplog):
    import plan_inputs

    def boom(self):
        raise OSError("state locked")

    monkeypatch.setattr(plan_inputs.PlanInputs, "record_skewls_rebalance", boom)
    with caplog.at_level(logging.ERROR):
        text = _rebalance()

    assert _sent(env) == ORDERS
    assert "SkewLS state save failed: state locked" in caplog.text
    assert "送出 2 筆 MOO" in text and "❌" not in text


def test_interrupt_outranks_trade_log_failure_and_orders_stay_in_the_message(
        env, monkeypatch):
    """Ctrl+C 在對帳時打斷（Step 9b 只接 Exception）、交易紀錄又被鎖住：往外拋的是
    KeyboardInterrupt，不是 Excel 錯誤；訊息仍列出已送出的單與寫入失敗。"""
    import rebalance_ib

    def interrupt(*_a, **_k):
        raise KeyboardInterrupt()

    def locked(*_a, **_k):
        raise PermissionError("trade_log.xlsx is locked")

    from notify import Digest
    monkeypatch.setattr(rebalance_ib.plan, "verify_execution", interrupt)
    monkeypatch.setattr(rebalance_ib, "append_trade_log", locked)
    digest = Digest("調倉 T", icon="📤")
    with pytest.raises(KeyboardInterrupt):
        _rebalance(digest)

    text = digest.render()
    assert _sent(env) == ORDERS
    assert "送出 2 筆 MOO，❌ 之後失敗" in text
    assert "🟢 BUY" in text
    assert "交易紀錄寫入失敗: trade_log.xlsx is locked" in text
