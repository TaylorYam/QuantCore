"""
每個 Account 的 state 檔（account_state，#233）＋ state 讀不出來時各入口停下的方式。

讀寫規則：檔不存在 → 預設值、缺鍵補齊、路徑清洗、原子寫入；損毀 → 備份成
.corrupt.YYYYMMDD.json、原檔留著、拋 StateCorrupted（不再默默回預設值）。

損毀時（使用者 2026-10-22 選 C）：該帳戶該策略這一輪不算訊號、不送它的單，
其他策略照常，發 Telegram 要人工處理。
  - SP500：持股維持現有股數，state 檔不被覆寫（21:00、Sync Pos、預演）；接管拒絕；
    16:00 不做 dip-buy 檢查
  - SkewLS（排程）：類股 ETF＋反向 ETF 全部維持現有股數
  - SkewLS（Sync Pos）：照常同步，送出後用新目標覆寫 state

不連 IB、不發 Telegram：Broker 是 in-memory 的 FakeBroker，訊號是固定值。
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace as NS

import pytest

import account_state
from account_state import StateCorrupted, StateSpec

SPEC = StateSpec(prefix="demo_state", defaults={"a": 1, "nested": {"b": []}},
                 label="Demo state", owner="Demo")
BROKEN = b'{"bull_weeks_counter": 2,'          # 寫到一半斷電的樣子


@pytest.fixture
def state_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(account_state, "STATE_DIR", tmp_path)
    return tmp_path


# ── 讀寫規則 ─────────────────────────────────────────────────────────

def test_missing_file_gives_a_fresh_copy_of_the_defaults(state_dir):
    state = account_state.load(SPEC, "Rosa")
    assert state == {"a": 1, "nested": {"b": []}}
    state["nested"]["b"].append(1)
    assert SPEC.defaults == {"a": 1, "nested": {"b": []}}      # 預設值不被改到
    assert list(state_dir.iterdir()) == []


def test_missing_keys_are_filled_from_the_defaults(state_dir):
    (state_dir / "demo_state_Rosa.json").write_text('{"a": 5}', encoding="utf-8")
    assert account_state.load(SPEC, "Rosa") == {"a": 5, "nested": {"b": []}}


def test_save_then_load_round_trips_and_leaves_no_temp_file(state_dir):
    account_state.save(SPEC, "Rosa", {"a": 2, "nested": {"b": [1]}}, what="demo")
    assert account_state.load(SPEC, "Rosa") == {"a": 2, "nested": {"b": [1]}}
    assert [p.name for p in state_dir.iterdir()] == ["demo_state_Rosa.json"]


def test_save_failure_is_logged_and_raised(state_dir, monkeypatch, caplog):
    import json_state

    def boom(path, state):
        raise OSError("disk full")

    monkeypatch.setattr(json_state, "save_atomic", boom)
    with pytest.raises(OSError, match="disk full"):
        account_state.save(SPEC, "Rosa", {"a": 2}, what="demo state")
    assert "Failed to save demo state for Rosa: disk full" in caplog.text


@pytest.mark.parametrize("name, file", [
    ("Rosa", "demo_state_Rosa.json"),
    ("a/b\\c:d e", "demo_state_a_b_c_d_e.json"),
    ("", "demo_state_default.json"),
    ("..", "demo_state_default.json"),
])
def test_account_name_is_sanitized_into_the_file_name(state_dir, name, file):
    assert account_state.path(SPEC, name) == state_dir / file


def test_corrupt_file_is_backed_up_kept_and_raised(state_dir, caplog):
    from datetime import date
    p = state_dir / "demo_state_Rosa.json"
    p.write_bytes(BROKEN)

    with pytest.raises(StateCorrupted) as err:
        account_state.load(SPEC, "Rosa")

    backup = state_dir / f"demo_state_Rosa.corrupt.{date.today():%Y%m%d}.json"
    assert err.value.backup == backup and backup.read_bytes() == BROKEN
    assert p.read_bytes() == BROKEN                       # 原檔留著：修好之前每一輪都停
    assert err.value.account_name == "Rosa"
    assert err.value.where() == f"demo_state_Rosa.json（已備份為 {backup.name}）"
    assert "Demo state corrupted" in caplog.text
    assert "fix or delete demo_state_Rosa.json" in caplog.text


def test_corrupt_file_whose_backup_fails_still_raises(state_dir, monkeypatch):
    import shutil
    (state_dir / "demo_state_Rosa.json").write_bytes(BROKEN)

    def no_copy(*_a):
        raise PermissionError("locked")

    monkeypatch.setattr(shutil, "copy2", no_copy)
    with pytest.raises(StateCorrupted) as err:
        account_state.load(SPEC, "Rosa")
    assert err.value.backup is None
    assert "備份失敗，原檔仍在" in err.value.where()


def test_both_strategies_share_the_rules_with_their_own_prefix(state_dir):
    import skewls_state
    import sp500_state
    assert sp500_state._state_path("Rosa") == state_dir / "sp500_state_Rosa.json"
    assert skewls_state._state_path("Rosa") == state_dir / "skewls_state_Rosa.json"
    for module in (sp500_state, skewls_state):
        (state_dir / module._state_path("Rosa").name).write_bytes(BROKEN)
        with pytest.raises(StateCorrupted):
            module.load_state("Rosa")


# ── 決策層：類股＋反向 ETF 凍結 ─────────────────────────────────────

def test_etf_freeze_holds_every_strategy_etf_at_current_shares():
    from rebalance_plan import apply_etf_freeze
    targets = {"XLK": {"shares": 250, "strategy": "SkewLS", "alloc_w": 0.5},
               "SEF": {"shares": 500, "strategy": "SkewLS", "alloc_w": 0.5},
               "AAPL": {"shares": 500, "strategy": "SP500", "alloc_w": 0.5}}
    positions = {"XLK": 20.0, "REW": 10.0, "XLC": -5.0, "AAPL": 100.0, "BTC": 1.5}

    assert apply_etf_freeze(dict(targets), False, positions) == targets
    frozen = apply_etf_freeze(dict(targets), True, positions)
    assert {s: t["shares"] for s, t in frozen.items()} == {
        "XLK": 20, "REW": 10, "XLC": -5,          # 現有股數（含 XLC 空單）
        "AAPL": 500}                              # SP500 個股照常；SEF 不建新倉
    assert frozen["REW"]["strategy"] == "ETF (frozen)"


# ── plan_inputs ─────────────────────────────────────────────────────

def _ctx(name="Rosa", alloc_sr=0.0, alloc_sp500=0.5, alloc_skewls=0.5):
    from account_ctx import AccountCtx
    return AccountCtx(
        name=name, account="U00000001", host="127.0.0.1", port=4999, client_id=99,
        dry_run=False, order_type="MOO", outside_rth=False, wait_fill_sec=0,
        alloc_sr=alloc_sr, alloc_sp500=alloc_sp500, alloc_skewls=alloc_skewls,
        price_feed_port=None)


SKEWLS_TODAY = {"XLK": 0.5, "SEF": 0.5}
POSITIONS = {"AAPL": 100.0, "XLK": 20.0, "REW": 10.0, "XLC": -5.0}


@pytest.fixture
def env(state_dir, monkeypatch, tmp_path):
    """真的 21:00／Sync Pos 流程：in-memory Broker（每檔 $100、NAV $100k）、固定訊號。
    SP500 要 AAPL 500 股；SkewLS 今天要 XLK 250、SEF 250（反向 ETF 依槓桿縮）。"""
    import manual_attribution as ma
    import rebalance_ib
    import strategy_orders
    from sync_position import SyncRecords
    from tests.fake_broker import FakeBroker, install_connect
    from tests.fake_signals import FakeSource

    monkeypatch.setattr(ma, "DEFAULT_LEDGER_PATH", tmp_path / "ledger.sqlite3")
    monkeypatch.setattr(ma, "DEFAULT_MANUAL_ORDERS_PATH", tmp_path / "orders.sqlite3")
    monkeypatch.setattr(rebalance_ib, "append_trade_log", lambda *a, **k: None)
    monkeypatch.setattr(strategy_orders.time, "sleep", lambda _s: None)
    env = NS(brokers=[], state_dir=state_dir, positions=dict(POSITIONS))

    def connect(ctx, **_kw):
        fake = FakeBroker(managed=["U00000001"], positions=dict(env.positions))
        env.brokers.append(fake)
        return fake.connect(ctx)

    install_connect(monkeypatch, connect)
    env.source = FakeSource(sp500=({"AAPL": 1.0}, "2026-10-21"),
                            skewls=(dict(SKEWLS_TODAY), "2026-10-22"),
                            universe={"AAPL", "MSFT"},
                            sp500_state={"bull_weeks_counter": 3})
    env.records = SyncRecords(connect=connect, attribution_ledger=tmp_path / "ledger.sqlite3",
                              manual_orders_store=tmp_path / "orders.sqlite3",
                              strategy_source=env.source)
    return env


def _corrupt(env, prefix):
    p = env.state_dir / f"{prefix}_Rosa.json"
    p.write_bytes(BROKEN)
    return p


def _sent(env):
    return sorted((p.symbol, p.action, p.qty) for b in env.brokers for p in b.placed)


def _rebalance(env, mode):
    import rebalance_ib
    from notify import Digest
    digest = Digest("調倉 T", icon="📤")
    rebalance_ib.main(mode=mode, ctx=_ctx(), digest=digest, source=env.source)
    return digest.render()


def test_rebalance_with_both_states_readable_trades_everything(env):
    """對照組：state 正常時 SP500 與 SkewLS 都照訊號送單。"""
    _rebalance(env, "all")
    assert _sent(env) == [("AAPL", "BUY", 400), ("REW", "SELL", 10), ("SEF", "BUY", 250),
                          ("XLC", "BUY", 5), ("XLK", "BUY", 230)]


def test_rebalance_with_corrupt_skewls_state_freezes_etfs_and_alerts(env, caplog):
    path = _corrupt(env, "skewls_state")
    caplog.set_level(logging.INFO)
    text = _rebalance(env, "all")

    assert _sent(env) == [("AAPL", "BUY", 400)]               # SP500 照常，ETF 一張都不動
    assert env.source.called("skewls") == []                  # SkewLS 不算訊號
    assert path.read_bytes() == BROKEN                        # 不覆寫、不記新目標
    assert "⚠️ SkewLS state 讀不出來：skewls_state_Rosa.json（已備份為" in text
    assert "類股 ETF＋反向 ETF 全部維持現有股數" in text
    assert "🔴" not in text                                    # 凍結不是「該動卻沒送」
    assert "Step 4b: SkewLS halted" in caplog.text


@pytest.mark.parametrize("mode", ["sr", "sp500"])
def test_skewls_hold_modes_also_freeze_when_its_state_is_corrupt(env, mode):
    """沒檢查 threshold 的 mode 也要讀 last_target — 讀不出來同樣凍結。"""
    _corrupt(env, "skewls_state")
    text = _rebalance(env, mode)
    assert [s for s in _sent(env) if s[0] != "AAPL"] == []
    assert "SkewLS state 讀不出來" in text


def test_rebalance_with_corrupt_sp500_state_holds_sp500_and_keeps_the_file(env):
    path = _corrupt(env, "sp500_state")
    text = _rebalance(env, "all")

    assert ("AAPL", "BUY", 400) not in _sent(env)              # SP500 持股不動
    assert ("SEF", "BUY", 250) in _sent(env)                   # SkewLS 照常
    assert path.read_bytes() == BROKEN                         # 沒用預設值蓋掉
    assert "⚠️ SP500 state 讀不出來：sp500_state_Rosa.json（已備份為" in text
    assert "持股維持不動" in text


def test_corrupt_state_alert_is_sent_even_when_no_order_goes_out(env):
    """沒單、沒對帳警報也要發 — 每一輪都告警，直到有人處理。"""
    _corrupt(env, "sp500_state")
    env.positions = {"AAPL": 100.0}
    text = _rebalance(env, "sp500")
    assert _sent(env) == []
    assert "沒有送單，⚠️ 有策略停下（state 讀不出來）" in text
    assert "SP500 state 讀不出來" in text


def test_sync_with_corrupt_skewls_state_syncs_and_rewrites_the_state(env):
    import sync_position
    from datetime import date
    path = _corrupt(env, "skewls_state")

    plan = sync_position.compute_sync_plan(_ctx(), records=env.records)
    assert plan.state_notices == [
        f"⚠️ SkewLS state 讀不出來：skewls_state_Rosa.json（已備份為 "
        f"skewls_state_Rosa.corrupt.{date.today():%Y%m%d}.json）。這次照常同步，"
        "送出後用新目標覆寫 state（損毀檔的備份保留）"]
    assert {o.symbol for o in plan.orders} == {"AAPL", "REW", "SEF", "XLC", "XLK"}

    sync_position.submit_sync_orders(plan)
    state = json.loads(path.read_text(encoding="utf-8"))
    assert state["last_target"] == SKEWLS_TODAY
    assert state["last_target_date"] == "2026-10-22"
    assert (env.state_dir / f"skewls_state_Rosa.corrupt.{date.today():%Y%m%d}.json"
            ).read_bytes() == BROKEN


def test_sync_with_corrupt_sp500_state_leaves_sp500_holdings_alone(env):
    import sync_position
    path = _corrupt(env, "sp500_state")

    plan = sync_position.compute_sync_plan(_ctx(), records=env.records)
    assert "AAPL" not in {o.symbol for o in plan.orders}
    assert plan.targets["AAPL"]["shares"] == 100
    assert plan.sp500_wgt == {}                         # 預覽不列假的 SP500 目標
    assert "SP500 state 讀不出來" in plan.state_notices[0]
    sync_position.submit_sync_orders(plan)
    assert path.read_bytes() == BROKEN


def test_sync_that_will_not_write_state_does_not_promise_an_overwrite(env):
    """接管／paper（不寫 state）讀到損毀的 SkewLS state：告警說這次不會覆寫。"""
    import plan_inputs
    _corrupt(env, "skewls_state")
    inputs = plan_inputs.start(_ctx(), plan_inputs.SYNC, persist_state=False,
                               source=env.source)
    [line] = inputs.state_notices()
    assert "這次不會覆寫，下次 21:00 SkewLS 仍會停下" in line


def test_preview_with_corrupt_skewls_state_shows_the_freeze(state_dir):
    from preview_rebalance import compute_preview, render_report, result_to_dict
    from tests.fake_signals import FakeSource
    (state_dir / "skewls_state_Rosa.json").write_bytes(BROKEN)
    source = FakeSource(skewls=(dict(SKEWLS_TODAY), "2026-10-22"), universe={"AAPL"})

    res = compute_preview("all", _ctx(alloc_sp500=0.0), nav=100_000,
                          positions={"XLK": 20.0, "REW": 10.0},
                          snapshot_prices={"XLK": 100.0, "REW": 100.0, "SEF": 100.0},
                          source=source)
    assert res.orders == []
    assert "SkewLS state 讀不出來" in res.state_notices[0]
    assert res.state_notices[0] in render_report(res)
    assert result_to_dict(res)["state_notices"] == res.state_notices


def test_preview_with_readable_states_has_no_state_notice_key(state_dir):
    """沒事時預演的 JSON 輸出與 #233 之前相同。"""
    from preview_rebalance import compute_preview, result_to_dict
    from tests.fake_signals import FakeSource
    res = compute_preview("sr", _ctx(alloc_skewls=0.0), nav=100_000, positions={},
                          source=FakeSource())
    assert "state_notices" not in result_to_dict(res)
