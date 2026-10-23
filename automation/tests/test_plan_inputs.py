"""
plan_inputs（[[調倉輸入]]，#186）— 從 interface 測：start → read → record_skewls_rebalance。

不連 IB、不開 Qt：帳戶快照用假的 adapter，訊號用固定值，state 目錄指到 tmp。
驗證 mode 語意（排程 mode 的 SP500 hold 與 SkewLS threshold gate、SYNC 的最新
訊號）、寫不寫 state、手動持倉歸屬在 SP500 訊號之前、SkewLS state 更新規則。
"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

import plan_inputs


def _ctx(alloc_sr=0.5, alloc_sp500=0.5, alloc_skewls=0.3):
    from account_ctx import AccountCtx
    return AccountCtx(
        name="Test", account="U00000000", host="127.0.0.1", port=4001, client_id=20,
        dry_run=True, order_type="MOO", outside_rth=False, wait_fill_sec=0,
        alloc_sr=alloc_sr, alloc_sp500=alloc_sp500, alloc_skewls=alloc_skewls,
        price_feed_port=None, tg_token="", tg_chat_id="")


class FakeAccount:
    def __init__(self, nav=100_000.0, positions=None):
        self._nav = nav
        self._positions = dict(positions or {"XLK": 10.0, "AAPL": 5.0, "BTC": 1.0})
        self.price_requests = []

    def nav(self):
        return self._nav

    def positions(self):
        return dict(self._positions)

    def prices(self, symbols):
        self.price_requests.append(list(symbols))
        return {s: 100.0 for s in symbols}


class Recorder(plan_inputs.Narrator):
    def __init__(self):
        self.events = []

    def sr(self): self.events.append("sr")
    def skewls_latest(self): self.events.append("skewls_latest")
    def nav(self, nav): self.events.append("nav")
    def positions(self, positions): self.events.append("positions")
    def holdings(self, split): self.events.append("holdings")
    def sp500(self, run): self.events.append(f"sp500:{run}")
    def skewls(self, step): self.events.append(f"skewls:{step}")
    def skewls_gate(self, decision): self.events.append("skewls_gate")
    def prices(self, symbols): self.events.append("prices")


SKEWLS_TODAY = {"REW": 0.5, "XLC": -0.5}


@pytest.fixture
def env(monkeypatch, tmp_path):
    """固定訊號（假的訊號來源，#232）；state 目錄在 tmp。"""
    import skewls_state
    import sp500_state
    from tests.fake_signals import FakeSource

    monkeypatch.setattr(sp500_state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(skewls_state, "STATE_DIR", tmp_path)
    calls = NS(order=[])

    def sp500_weights(holdings, name):
        calls.order.append("sp500_signal")
        return {"AAPL": 0.6, "MSFT": 0.4}, "2026-10-02"

    source = FakeSource(sr=({"XLK": 1.0}, "2026-09-28"), sp500=sp500_weights,
                        skewls=lambda: (dict(SKEWLS_TODAY), "2026-10-02"),
                        universe={"AAPL", "MSFT"},
                        sp500_state={"bull_weeks_counter": 2, "dip_state": {}})
    return NS(calls=calls, source=source, state_dir=tmp_path)


def _sp500_saved(env):
    path = env.state_dir / "sp500_state_Test.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _skewls_state(env, last_target, last_date="2026-09-01"):
    import skewls_state
    skewls_state.save_state("Test", {**skewls_state.DEFAULT_STATE,
                                     "last_target": last_target,
                                     "last_target_date": last_date})
    return (env.state_dir / "skewls_state_Test.json").read_text(encoding="utf-8")


def _read_skewls(env):
    return json.loads((env.state_dir / "skewls_state_Test.json").read_text(encoding="utf-8"))


# ── 排程 mode ────────────────────────────────────────────────────────

def test_sp500_mode_recomputes_sp500_writes_state_and_holds_skewls(env):
    _skewls_state(env, {"REW": 0.3})
    say = Recorder()
    inputs = plan_inputs.start(_ctx(), "sp500", persist_state=True, narrator=say, source=env.source)
    account = FakeAccount()
    inputs.read(account, attribution=False)

    assert say.events == ["sr", "nav", "positions", "holdings", "sp500:True",
                          "skewls:hold", "prices"]
    assert env.source.called("sp500") == [(["AAPL"], "Test")]
    assert _sp500_saved(env) == env.source.sp500_state       # 寫回這次訊號之後的 state
    assert inputs.sp500_for_combine == {"AAPL": 0.6, "MSFT": 0.4}
    assert inputs.skewls_wgt == {"REW": 0.3}                  # 沿用 last_target
    assert inputs.skewls_decision.triggered is None           # 不檢查
    assert env.source.called("skewls") == []
    assert inputs.skipped_state_writes == []
    assert account.price_requests == [["AAPL", "BTC", "MSFT", "REW", "XLK"]]
    assert inputs.record_skewls_rebalance() is None           # 沒檢查就不記


def test_sr_mode_holds_sp500_without_calling_the_signal(env):
    inputs = plan_inputs.start(_ctx(alloc_skewls=0), "sr", persist_state=True, source=env.source)
    inputs.read(FakeAccount(), attribution=False)

    assert env.source.called("sp500") == []                               # ADR-0004
    assert not inputs.run_sp500
    assert inputs.sp500_wgt == {"AAPL": 0}                     # 只為帶進報價清單
    assert inputs.sp500_for_combine == {}
    assert inputs.sp500_date == "N/A"


def test_skewls_disabled_skips_state_and_signal(env):
    say = Recorder()
    inputs = plan_inputs.start(_ctx(alloc_skewls=0), "all", persist_state=True,
                               narrator=say, source=env.source)
    inputs.read(FakeAccount(), attribution=False)

    assert "skewls:disabled" in say.events
    assert env.source.called("skewls") == []
    assert (inputs.skewls_wgt, inputs.skewls_date, inputs.skewls_decision) == ({}, "N/A", None)
    assert not (env.state_dir / "skewls_state_Test.json").exists()


def test_skewls_gate_records_todays_target_only_when_triggered(env):
    _skewls_state(env, {"REW": 1.0})                           # 和今日差很多 → 觸發
    say = Recorder()
    inputs = plan_inputs.start(_ctx(), "skewls", persist_state=True, narrator=say, source=env.source)
    inputs.read(FakeAccount(), attribution=False)

    assert say.events[-3:] == ["skewls:check", "skewls_gate", "prices"]
    assert inputs.skewls_decision.triggered is True
    assert inputs.skewls_wgt == SKEWLS_TODAY
    assert inputs.record_skewls_rebalance() == "2026-10-02"
    saved = _read_skewls(env)
    assert saved["last_target"] == SKEWLS_TODAY
    assert saved["last_target_date"] == "2026-10-02"


def test_skewls_gate_not_triggered_leaves_state_alone(env):
    before = _skewls_state(env, dict(SKEWLS_TODAY), last_date="2026-09-30")
    inputs = plan_inputs.start(_ctx(), "all", persist_state=True, source=env.source)
    inputs.read(FakeAccount(), attribution=False)

    assert inputs.skewls_decision.triggered is False
    assert inputs.skewls_date == "2026-09-30"                  # 維持上次目標的日期
    assert inputs.record_skewls_rebalance() is None
    assert (env.state_dir / "skewls_state_Test.json").read_text(encoding="utf-8") == before


# ── SYNC（Sync Pos／接管）─────────────────────────────────────────────

def test_sync_uses_latest_signals_before_connecting(env):
    _skewls_state(env, {"REW": 1.0})
    say = Recorder()
    inputs = plan_inputs.start(_ctx(), plan_inputs.SYNC, persist_state=True, narrator=say, source=env.source)

    assert say.events == ["sr", "skewls_latest"]               # 連 IB 之前就算好
    inputs.read(FakeAccount(), attribution=False)
    assert say.events[2:] == ["nav", "positions", "holdings", "sp500:True", "prices"]
    assert inputs.skewls_wgt == SKEWLS_TODAY                   # 不套 threshold gate
    assert inputs.skewls_decision is None
    assert inputs.record_skewls_rebalance() == "2026-10-02"    # 強制同步：有目標就記
    assert _read_skewls(env)["last_target"] == SKEWLS_TODAY


def test_sync_without_persisting_writes_nothing_and_reports_the_skip(env):
    before = _skewls_state(env, {"REW": 1.0})
    inputs = plan_inputs.start(_ctx(), plan_inputs.SYNC, persist_state=False, source=env.source)
    inputs.read(FakeAccount(), attribution=False)

    assert env.source.called("sp500") == [(["AAPL"], "Test")]
    assert _sp500_saved(env) is None                           # 不寫 SP500 state
    assert inputs.skipped_state_writes == ["sp500_state:Test"]
    assert inputs.record_skewls_rebalance() is None
    assert (env.state_dir / "skewls_state_Test.json").read_text(encoding="utf-8") == before


def test_price_symbols_limits_the_quote_request(env):
    account = FakeAccount()
    inputs = plan_inputs.start(_ctx(alloc_skewls=0), plan_inputs.SYNC, persist_state=False, source=env.source)
    inputs.read(account, attribution=False, price_symbols=["AAPL"])

    assert account.price_requests == [["AAPL"]]
    assert inputs.prices == {"AAPL": 100.0}


# ── 手動持倉歸屬在 SP500 訊號之前 ─────────────────────────────────────

def test_attribution_and_vet_run_before_the_sp500_signal(env, tmp_path):
    import manual_attribution

    seen = []

    def vet(inputs):
        seen.append(inputs.attribution)
        env.calls.order.append("vet")
        raise RuntimeError("refused")

    inputs = plan_inputs.start(_ctx(alloc_skewls=0), "sp500", persist_state=True, source=env.source)
    with pytest.raises(RuntimeError, match="refused"):
        inputs.read(FakeAccount(), ledger_path=tmp_path / "ledger.sqlite3",
                    manual_orders_path=tmp_path / "orders.sqlite3", vet=vet)

    assert isinstance(seen[0], manual_attribution.Attribution)
    assert env.calls.order == ["vet"]                          # SP500 訊號沒跑、沒寫 state
    assert _sp500_saved(env) is None
    assert inputs.attribution is seen[0]                       # 已讀到的部分留著
    assert inputs.sp500_date == "N/A"


def test_attribution_failure_stops_before_the_sp500_signal(env, monkeypatch):
    import manual_attribution

    def broken(*_a, **_k):
        raise manual_attribution.ManualAttributionError("ledger locked")

    monkeypatch.setattr(manual_attribution, "evaluate", broken)
    inputs = plan_inputs.start(_ctx(alloc_skewls=0), "sp500", persist_state=True, source=env.source)
    with pytest.raises(manual_attribution.ManualAttributionError):
        inputs.read(FakeAccount())
    assert env.source.called("sp500") == []


def test_unknown_mode_is_rejected(env):
    with pytest.raises(ValueError):
        plan_inputs.start(_ctx(), "weekly", persist_state=True, source=env.source)
