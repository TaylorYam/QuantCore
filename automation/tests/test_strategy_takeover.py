"""
單一標的交給策略接管（#107）。

以 in-memory Broker（tests/fake_broker.py）走 strategy_takeover 的真實流程：預覽（Account、標的、現有股數、
策略整數目標、差額與方向；目標 0 = 賣出現有股數）、取消、未結案手動單與
掛單的拒絕、確認前重算讓舊預覽失效、成功、拒單、部分成交、逾時後重啟再
完成、per-Strategy 取整，以及其他 Account／標的不受影響（Panel 畫面見
test_qt_takeover）。全部離線：不連 IB、不發 Telegram、不寫 SP500 state。
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace as NS

import pytest

import manual_attribution as ma
import strategy_takeover as st
from sync_position import SyncRecords
from tests.fake_broker import FakeBroker

UNIVERSE = {"AAPL", "MSFT", "SPY", "SSO"}
JOINED = UNIVERSE | {"XYZ", "ABC"}          # XYZ、ABC 之後被加入 S&P 500


def _ctx(name="Rosa", dry_run=False, alloc=(0.0, 0.5, 0.0)):
    from account_ctx import AccountCtx
    return AccountCtx(
        name=name, account={"Rosa": "U2", "Taylor": "U1"}[name], host="127.0.0.1",
        port=4999, client_id=98, dry_run=dry_run, order_type="MOO",
        outside_rth=False, wait_fill_sec=3, alloc_sr=alloc[0], alloc_sp500=alloc[1],
        alloc_skewls=alloc[2], price_feed_port=None, tg_token="T", tg_chat_id="C",
    )


def _manual_order(path, ref, account, symbol, action, filled, status, quantity=10):
    from manual_orders import ManualOrderStore
    ManualOrderStore(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO manual_orders (
                   order_ref, account_name, account_id, symbol, action, quantity,
                   limit_price, tif, outside_rth, contract_id, status,
                   status_message, filled_quantity, created_at, updated_at)
               VALUES (?, ?, 'U0', ?, ?, ?, 10, 'DAY', 0, 1, ?, '', ?, 't', 't')""",
            (ref, account, symbol, action, quantity, status, filled))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Rosa 手動持有 XYZ 10 股、ABC 5 股，兩檔之後都進了 SP500 Universe。"""
    import sp500_state
    from tests.fake_signals import FakeSource

    # IB 這一側是 in-memory Broker：權益、持倉、掛單、送單行為直接設在 env 上
    env = FakeBroker(managed=["U2"], nav=100_000.0,
                     positions={"AAPL": 100.0, "XYZ": 10.0, "ABC": 5.0})
    env.sp500, env.sr, env.skewls = {"AAPL": 0.5, "XYZ": 0.5}, {}, {}
    env.state_dir, env.orders = tmp_path / "state", tmp_path / "manual_orders.sqlite3"
    env.state_dir.mkdir()
    monkeypatch.setattr(ma, "DEFAULT_LEDGER_PATH", tmp_path / "ledger.sqlite3")
    monkeypatch.setattr(ma, "DEFAULT_MANUAL_ORDERS_PATH", env.orders)
    monkeypatch.setattr(sp500_state, "STATE_DIR", env.state_dir)

    def sp500(holdings, name):
        env.sp500_holdings = list(holdings)
        return dict(env.sp500), "2026-09-22"

    # 訊號來源（#232）：固定訊號，Universe 已含 XYZ、ABC
    env.source = FakeSource(sr=lambda: (dict(env.sr), "2026-09-28"), sp500=sp500,
                            skewls=lambda: (dict(env.skewls), "2026-09-28"),
                            universe=JOINED, sp500_state={"touched": True})
    # 實盤送單目標給的連線與紀錄（#183：沒有預設，一律明確給）
    env.records = SyncRecords(connect=env.connect,
                              attribution_ledger=tmp_path / "ledger.sqlite3",
                              manual_orders_store=env.orders,
                              strategy_source=env.source)
    # 啟用時 XYZ、ABC 還是策略外 = 手動
    ma.evaluate("Rosa", dict(env.positions), UNIVERSE)
    ma.evaluate("Rosa", dict(env.positions), JOINED)
    return env


# ── 預覽 ─────────────────────────────────────────────────────────────

def test_preview_shows_account_symbol_target_delta_and_direction(env):
    p = st.build_preview(_ctx(), "XYZ", records=env.records)
    assert (p.account_name, p.symbol, p.current, p.manual) == ("Rosa", "XYZ", 10.0, 10.0)
    assert (p.target, p.parts, p.delta, p.action, p.quantity) == (
        250, (("SP500", 250),), 240, "BUY", 240)
    text = "\n".join(p.lines())
    assert "IB 現有股數：10" in text and "策略整數目標：250 股（SP500 250）" in text
    assert "差額：+240 股 — 買進 240 股" in text
    assert env.sp500_holdings == ["AAPL", "XYZ", "ABC"]      # 與 Sync Pos 同一份持股輸入
    assert list(env.state_dir.iterdir()) == []               # 不寫 SP500 state


def test_zero_target_means_selling_the_existing_shares(env):
    env.sp500 = {"AAPL": 1.0}
    p = st.build_preview(_ctx(), "XYZ", records=env.records)
    assert (p.target, p.delta, p.action, p.quantity) == (0, -10, "SELL", 10)
    assert "策略目標為 0，將賣出現有 10 股" in "\n".join(p.lines())


def test_cancelled_preview_sends_nothing_and_keeps_the_pause(env):
    st.build_preview(_ctx(), "XYZ", records=env.records)     # 使用者按 Cancel
    assert env.placed == []
    assert set(ma.paused_symbols("Rosa")) == {"XYZ", "ABC"}
    assert ma.pending_takeover("Rosa", "XYZ") is None


def test_unresolved_manual_order_refuses_takeover(env):
    _manual_order(env.orders, "M:1", "Rosa", "XYZ", "SELL", 0, "ACCEPTED")
    with pytest.raises(st.TakeoverRefused, match="未結案"):
        st.build_preview(_ctx(), "XYZ", records=env.records)


def test_open_order_on_the_symbol_refuses_takeover(env):
    tws = NS(order=NS(account="U2", action="SELL", totalQuantity=3),
             contract=NS(symbol="XYZ"), orderStatus=NS(status="Submitted"))
    other = NS(order=NS(account="U2", action="BUY", totalQuantity=1),
               contract=NS(symbol="AAPL"), orderStatus=NS(status="Submitted"))
    env.open_orders = [other]
    st.build_preview(_ctx(), "XYZ", records=env.records)     # 別的標的的掛單不擋
    env.open_orders = [tws, other]
    with pytest.raises(st.TakeoverRefused, match="1 筆掛單（SELL 3）"):
        st.build_preview(_ctx(), "XYZ", records=env.records)


def test_symbol_that_is_not_paused_cannot_be_taken_over(env):
    with pytest.raises(st.TakeoverRefused, match="沒有被手動持倉暫停"):
        st.build_preview(_ctx(), "AAPL", records=env.records)


def test_per_strategy_rounding_is_kept(env):
    """同一檔被 SR 與 Skew LS 同時持有：各自取整後相加（ADR-0008），不是合權重再取整。"""
    _manual_order(env.orders, "M:1", "Rosa", "XLK", "BUY", 5, "FILLED", 5)
    env.positions["XLK"] = 5.0
    env.sr = {"XLK": 0.003625}          # 100000 × 0.8 × w / 100 = 2.9  → int  → 2
    env.skewls = {"XLK": 0.0046667}     # 100000 × 0.3 × w / 100 ≈ 1.4  → round → 1
    p = st.build_preview(_ctx(alloc=(0.8, 0.5, 0.3)), "XLK", records=env.records)
    assert p.parts == (("SR", 2), ("SkewLS", 1)) and p.target == 3   # 合權重 4.3 → 4


# ── 送單 ─────────────────────────────────────────────────────────────

def test_changed_position_invalidates_the_old_preview(env):
    p = st.build_preview(_ctx(), "XYZ", records=env.records)
    env.positions["XYZ"] = 12.0                               # 預覽後在 App 又買 2 股
    with pytest.raises(st.TakeoverStale) as info:
        st.submit(p, _ctx(), records=env.records)
    assert info.value.fresh.current == 12.0 and info.value.fresh.delta == 238
    assert env.placed == [] and ma.pending_takeover("Rosa", "XYZ") is None


def test_success_sends_only_that_symbol_and_releases_only_its_pause(env):
    ma.evaluate("Taylor", {"XYZ": 7.0}, UNIVERSE)
    ma.evaluate("Taylor", {"XYZ": 7.0}, JOINED)

    result = st.submit(st.build_preview(_ctx(), "XYZ", records=env.records), _ctx(),
                       records=env.records)

    assert result.status == "COMPLETED", result.message
    [o] = env.placed
    assert (o.symbol, o.action, o.qty, o.lmt, o.outside_rth, o.account) == (
        "XYZ", "BUY", 240, round(100.1 * 1.003, 2), True, "U2")
    assert o.ref.startswith("QUANTCORE_TAKEOVER:ROSA:")
    assert set(ma.paused_symbols("Rosa")) == {"ABC"}           # 只解除 XYZ
    assert set(ma.paused_symbols("Taylor")) == {"XYZ"}         # 其他 Account 不受影響
    assert ma.gate_orders("Rosa", [{"symbol": "XYZ"}])[1] == []
    assert list(env.state_dir.iterdir()) == []

    sent = []
    assert st.notify(_ctx(), send=lambda m: sent.append(m) or True, records=env.records)
    assert "✅ XYZ 已交給策略接管" in sent[0]
    assert not st.notify(_ctx(), send=lambda m: sent.append(m) or True, records=env.records)   # 只通知一次

    # 之後的觀察：XYZ 全歸策略，不再暫停
    after = ma.evaluate("Rosa", dict(env.positions), JOINED)
    assert "XYZ" not in after.paused and "XYZ" not in after.manual


def test_preview_and_submit_without_records_refuse_before_connecting(env):
    """#183：沒有「預設實盤」— 忘了給連線與紀錄就報錯，不會不知情地繞過送單閘門。"""
    with pytest.raises(TypeError):
        st.build_preview(_ctx(), "XYZ")
    with pytest.raises(TypeError, match="SyncRecords"):
        st.build_preview(_ctx(), "XYZ", records=None)
    p = st.build_preview(_ctx(), "XYZ", records=env.records)
    with pytest.raises(TypeError):
        st.submit(p, _ctx())
    with pytest.raises(TypeError, match="SyncRecords"):
        st.submit(p, _ctx(), records=None)
    with pytest.raises(TypeError):
        st.notify(_ctx(), lambda m: True)
    assert env.placed == [] and ma.pending_takeover("Rosa", "XYZ") is None


def test_zero_target_success_sells_everything(env):
    env.sp500 = {"AAPL": 1.0}
    result = st.submit(st.build_preview(_ctx(), "XYZ", records=env.records), _ctx(),
                       records=env.records)
    assert result.status == "COMPLETED"
    assert [(o.action, o.qty) for o in env.placed] == [("SELL", 10)]
    assert env.positions["XYZ"] == 0


def test_no_difference_hands_over_without_an_order(env):
    env.sp500 = {"AAPL": 0.98, "XYZ": 0.02}                  # 100000 × 0.5 × 0.02 / 100 = 10
    result = st.submit(st.build_preview(_ctx(), "XYZ", records=env.records), _ctx(),
                       records=env.records)
    assert result.status == "COMPLETED" and env.placed == []
    assert "XYZ" not in ma.paused_symbols("Rosa")


def test_rejected_order_keeps_the_pause(env):
    env.behavior = "reject"
    result = st.submit(st.build_preview(_ctx(), "XYZ", records=env.records), _ctx(),
                       records=env.records)
    assert result.status == "FAILED" and "IB 拒單（Order rejected - reason: margin）" in result.message
    assert "XYZ" in ma.paused_symbols("Rosa")
    assert ma.pending_takeover("Rosa", "XYZ") is None
    lines = [n.line for n in ma.pending_notices("Rosa")]
    assert any(line.startswith("❌ XYZ 接管未完成：IB 拒單") for line in lines)


def test_partial_fill_stays_paused_and_completes_after_restart(env):
    env.behavior = "partial"
    result = st.submit(st.build_preview(_ctx(), "XYZ", records=env.records), _ctx(),
                       records=env.records)
    assert result.status == "PENDING"
    reason = ma.paused_symbols("Rosa")["XYZ"]
    assert reason.startswith("接管中：已送 BUY 240 股（接管後預計 250 股）")
    assert ma.gate_orders("Rosa", [{"symbol": "XYZ"}])[0] == []   # 策略單仍被擋

    # Panel 關掉、隔天 21:00 調倉時剩下的 140 股已成交 — 新行程只剩紀錄檔
    env.positions["XYZ"] = 250.0
    later = ma.evaluate("Rosa", dict(env.positions), JOINED)
    assert later.completed == ["XYZ"] and "XYZ" not in later.paused
    assert any(n.line.startswith("✅ XYZ 已交給策略接管") for n in later.new_notices)


def test_working_order_times_out_as_pending(env):
    env.behavior = "working"
    result = st.submit(st.build_preview(_ctx(), "XYZ", records=env.records), _ctx(),
                       records=env.records)
    assert result.status == "PENDING" and "維持暫停" in result.message
    assert ma.pending_takeover("Rosa", "XYZ").expected_after == 250.0


def test_unknown_send_result_keeps_the_record(env):
    env.behavior = "raise"
    result = st.submit(st.build_preview(_ctx(), "XYZ", records=env.records), _ctx(),
                       records=env.records)
    assert result.status == "PENDING" and "送單結果不確定" in result.message
    assert ma.pending_takeover("Rosa", "XYZ") is not None and "XYZ" in ma.paused_symbols("Rosa")


def test_dry_run_changes_nothing(env):
    result = st.submit(st.build_preview(_ctx(dry_run=True), "XYZ", records=env.records),
                       _ctx(dry_run=True), records=env.records)
    assert result.status == "DRY_RUN" and env.placed == []
    assert ma.pending_takeover("Rosa", "XYZ") is None and "XYZ" in ma.paused_symbols("Rosa")


def test_other_records_keep_the_live_ledger_untouched(env, tmp_path, monkeypatch):
    """接管的連線、歸屬紀錄、手動單紀錄都用傳進來的那一組（#183），不碰預設的實盤檔。"""
    live_ledger = tmp_path / "ledger.sqlite3"
    live_before = live_ledger.read_bytes()
    other = NS(ledger=tmp_path / "paper_ledger.sqlite3",
               orders=tmp_path / "paper_orders.sqlite3", connects=[])
    _manual_order(other.orders, "M:9", "Rosa", "ABC", "BUY", 0, "ACCEPTED")

    def connect(ctx):
        other.connects.append(ctx.client_id)
        return env.connect(ctx)

    records = SyncRecords(connect=connect, attribution_ledger=other.ledger,
                          manual_orders_store=other.orders, strategy_source=env.source)
    ma.evaluate("Rosa", dict(env.positions), UNIVERSE, ledger_path=other.ledger,
                manual_orders_path=other.orders)
    ma.evaluate("Rosa", dict(env.positions), JOINED, ledger_path=other.ledger,
                manual_orders_path=other.orders)
    with pytest.raises(st.TakeoverRefused, match="未結案"):     # 讀 other 的手動單紀錄
        st.build_preview(_ctx(), "ABC", records=records)

    env.behavior = "reject"
    result = st.submit(st.build_preview(_ctx(), "XYZ", records=records), _ctx(),
                       records=records)
    assert result.status == "FAILED"
    assert "XYZ" in ma.paused_symbols("Rosa", path=other.ledger)
    assert [n.line for n in ma.pending_notices("Rosa", path=other.ledger)
            if n.key.startswith("event:")] == [
        "❌ XYZ 接管未完成：IB 拒單（Order rejected - reason: margin）；維持暫停"]

    env.behavior = "fill"
    result = st.submit(st.build_preview(_ctx(), "XYZ", records=records), _ctx(),
                       records=records)
    assert result.status == "COMPLETED", result.message
    assert "XYZ" not in ma.paused_symbols("Rosa", path=other.ledger)
    assert other.connects == [98] * 5
    assert live_ledger.read_bytes() == live_before              # 實盤紀錄一個 byte 都沒動
    assert ma.pending_takeover("Rosa", "XYZ") is None


def test_takeover_orders_survive_strategy_cancel_all():
    takeover = NS(order=NS(orderRef="QUANTCORE_TAKEOVER:ROSA:abc"))
    strategy = NS(order=NS(orderRef=""))
    assert ma.strategy_open_trades([takeover, strategy]) == [strategy]
