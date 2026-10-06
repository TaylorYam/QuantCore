"""
手動持倉歸屬與策略送單暫停（#106）。

歸屬（manual_attribution）：啟用時的既有策略外持倉、Panel 手動單的實際成交
（部分成交、賣出、放空、回補）、IB App 交易、重啟、Universe 變化、對帳不一致、
讀檔失敗、帳戶／標的隔離。送單入口：rebalance_ib.main、Sync Pos、
sp500_daily_check 用 fake IB 走真實流程 — 暫停標的不送、其他單照舊、
Panel 手動單不被「全部取消」、暫停只通知一次。

全部離線：假 IB、假訊號、tmp_path 的 SQLite；不連 IB、不發 Telegram。
"""

from __future__ import annotations

import sqlite3
from datetime import date
from types import SimpleNamespace as NS

import pytest

import manual_attribution as ma

UNIVERSE = {"AAPL", "MSFT", "SPY", "SSO"}
JOINED = UNIVERSE | {"XYZ"}          # XYZ 被加入 S&P 500 之後


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """每個測試自己的歸屬紀錄與手動單資料庫（預設路徑也指過去）。"""
    ledger = tmp_path / "manual_attribution.sqlite3"
    orders = tmp_path / "manual_orders.sqlite3"
    monkeypatch.setattr(ma, "DEFAULT_LEDGER_PATH", ledger)
    monkeypatch.setattr(ma, "DEFAULT_MANUAL_ORDERS_PATH", orders)
    return NS(ledger=ledger, orders=orders)


def _manual_order(path, ref, account, symbol, action, filled, status,
                  quantity=None):
    """寫一筆 Panel 手動單 — 用 manual_orders 自己的 schema 建表。"""
    from manual_orders import ManualOrderStore

    ManualOrderStore(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO manual_orders (
                   order_ref, account_name, account_id, symbol, action, quantity,
                   limit_price, tif, outside_rth, contract_id, status,
                   status_message, filled_quantity, created_at, updated_at)
               VALUES (?, ?, 'U0', ?, ?, ?, 10, 'DAY', 0, 1, ?, '', ?, ?, ?)
               ON CONFLICT(order_ref) DO UPDATE SET
                   status=excluded.status,
                   filled_quantity=excluded.filled_quantity""",
            (ref, account, symbol, action, quantity or max(filled, 1), status,
             filled, f"2026-09-28T00:00:{len(ref):02d}", "2026-09-28T00:00:00"))


def _eval(account, positions, universe=UNIVERSE):
    return ma.evaluate(account, positions, universe)


# ── 歸屬 ─────────────────────────────────────────────────────────────

def test_enable_records_existing_non_strategy_holdings(paths):
    """啟用 = 第一次觀察：當下已是策略外的持倉（含碎股）記為手動，不暫停任何標的。"""
    r = _eval("Rosa", {"BTC": 65.1783, "XLK": 15.0, "AAPL": 3.0, "SEF": 3.0})
    assert r.manual == {"BTC": 65.1783}
    assert r.paused == {} and r.new_notices == []


def test_no_ledger_means_nothing_paused_and_no_file_created(paths):
    kept, held = ma.gate_orders("Rosa", [{"symbol": "AAPL"}, {"symbol": "XYZ"}])
    assert [o["symbol"] for o in kept] == ["AAPL", "XYZ"] and held == []
    assert not paths.ledger.exists()
    assert ma.read_manual_fills("Rosa") == [] and not paths.orders.exists()


def test_ib_app_trades_on_non_strategy_symbol_follow_ib_silently(paths, caplog):
    """IB App／TWS 買賣策略外標的：股數跟著 IB，不暫停、不通知（使用者 09-28 決定）。"""
    _eval("Rosa", {"BTC": 65.1783})
    r = _eval("Rosa", {"BTC": 70.0, "SKHY": 120.0})
    assert r.manual == {"BTC": 70.0, "SKHY": 120.0}
    assert r.paused == {} and r.new_notices == []
    r = _eval("Rosa", {"SKHY": 120.0})          # BTC 在 App 賣光
    assert r.manual == {"SKHY": 120.0}


def test_collision_pauses_only_that_account_and_symbol(paths):
    """手動持倉的標的進入 SP500 Universe → 只停 Rosa／XYZ；Taylor 的 XYZ 是策略部位。"""
    _eval("Rosa", {"XYZ": 10.0, "AAPL": 5.0})
    _eval("Taylor", {"AAPL": 2.0})
    rosa = _eval("Rosa", {"XYZ": 10.0, "AAPL": 5.0}, JOINED)
    taylor = _eval("Taylor", {"AAPL": 2.0, "XYZ": 4.0}, JOINED)

    assert list(rosa.paused) == ["XYZ"]
    assert "手動持倉 10 股已進入 SP500 Universe" in rosa.paused["XYZ"]
    assert rosa.new_notices == [ma.Notice("pause:XYZ", f"⏸ XYZ 策略單暫停：{rosa.paused['XYZ']}")]
    assert taylor.paused == {}

    orders = [{"symbol": "XYZ", "action": "SELL"}, {"symbol": "AAPL", "action": "BUY"}]
    assert [o["symbol"] for o in ma.gate_orders("Rosa", orders)[0]] == ["AAPL"]
    assert [o["symbol"] for o in ma.gate_orders("Taylor", orders)[0]] == ["XYZ", "AAPL"]


def test_pause_survives_restart_and_notifies_once(paths):
    _eval("Rosa", {"XYZ": 10.0})
    first = _eval("Rosa", {"XYZ": 10.0}, JOINED)
    ma.mark_notified("Rosa", [n.key for n in first.new_notices])
    # 重啟 = 新行程只剩檔案
    again = _eval("Rosa", {"XYZ": 10.0}, JOINED)
    assert again.paused == first.paused and again.new_notices == []
    assert ma.paused_symbols("Rosa") == first.paused


def test_fills_update_attribution_not_submissions(paths):
    """送出不算、成交才算；部分成交照實際股數；觀察後的成交只加差額。"""
    _manual_order(paths.orders, "M:1", "Rosa", "XYZ", "BUY", 0, "ACCEPTED", 10)
    r = _eval("Rosa", {}, UNIVERSE)
    assert r.manual == {}                                # 送出但未成交
    _manual_order(paths.orders, "M:1", "Rosa", "XYZ", "BUY", 4, "PARTIAL", 10)
    r = _eval("Rosa", {"XYZ": 4.0})
    assert r.manual == {"XYZ": 4.0}
    _manual_order(paths.orders, "M:1", "Rosa", "XYZ", "BUY", 7, "CANCELLED", 10)
    # 成交紀錄更新了、還沒再觀察 IB 就碰撞：4（觀察）+ 3（之後的成交）
    r = _eval("Rosa", {"XYZ": 7.0}, JOINED)
    assert r.manual == {"XYZ": 7.0}
    assert "手動持倉 7 股" in r.paused["XYZ"]
    assert "對不上" not in r.paused["XYZ"]


def test_fills_before_any_observation_count_in_full(paths):
    """手動單成交後、還沒被觀察就碰撞：成交量全算手動。"""
    _manual_order(paths.orders, "M:1", "Rosa", "XYZ", "BUY", 10, "FILLED")
    r = _eval("Rosa", {"XYZ": 10.0}, JOINED)
    assert r.manual == {"XYZ": 10.0} and "手動持倉 10 股" in r.paused["XYZ"]


def test_sell_short_and_cover_signs(paths):
    _manual_order(paths.orders, "M:1", "Rosa", "XYZ", "SELL_SHORT", 5, "FILLED")
    r = _eval("Rosa", {"XYZ": -5.0}, JOINED)
    assert r.manual == {"XYZ": -5.0} and "手動持倉 -5 股" in r.paused["XYZ"]
    _manual_order(paths.orders, "M:2", "Rosa", "XYZ", "COVER", 5, "FILLED")
    r = _eval("Rosa", {}, JOINED)
    assert r.manual == {} and r.paused == {}


def test_manual_sell_to_zero_releases_and_a_new_episode_notifies_again(paths):
    _manual_order(paths.orders, "M:1", "Rosa", "XYZ", "BUY", 10, "FILLED")
    _eval("Rosa", {"XYZ": 10.0})
    _manual_order(paths.orders, "M:2", "Rosa", "XYZ", "SELL", 0, "ACCEPTED", 10)
    r = _eval("Rosa", {"XYZ": 10.0}, JOINED)
    assert "未結案" in r.paused["XYZ"]
    ma.mark_notified("Rosa", ["pause:XYZ"])

    _manual_order(paths.orders, "M:2", "Rosa", "XYZ", "SELL", 10, "FILLED", 10)
    r = _eval("Rosa", {}, JOINED)
    assert r.paused == {} and ma.paused_symbols("Rosa") == {}

    _manual_order(paths.orders, "M:3", "Rosa", "XYZ", "BUY", 0, "SUBMISSION_UNKNOWN", 3)
    r = _eval("Rosa", {}, JOINED)
    assert [n.key for n in r.new_notices] == ["pause:XYZ"]


def test_ib_differs_from_manual_record_stays_paused_with_both_numbers(paths):
    """碰撞後在 App 賣掉一部分：紀錄對不上 → 照樣暫停，原因寫出兩邊股數（等 #107）。"""
    _eval("Rosa", {"XYZ": 10.0})
    r = _eval("Rosa", {"XYZ": 7.0}, JOINED)
    assert "手動持倉 10 股" in r.paused["XYZ"] and "IB 實際 7 股" in r.paused["XYZ"]
    r = _eval("Rosa", {}, JOINED)                    # 全賣光也不自動解除
    assert "IB 實際 0 股" in r.paused["XYZ"]


def test_unknown_status_or_action_is_unresolved(paths):
    _manual_order(paths.orders, "M:1", "Rosa", "XYZ", "BUY", 0, "SOMETHING_NEW")
    assert "未結案" in _eval("Rosa", {}, JOINED).paused["XYZ"]


def test_manual_orders_of_other_accounts_are_ignored(paths):
    _manual_order(paths.orders, "M:1", "Taylor", "XYZ", "BUY", 10, "FILLED")
    assert _eval("Rosa", {"XYZ": 3.0}, JOINED).paused == {}
    assert "XYZ" in _eval("Taylor", {"XYZ": 10.0}, JOINED).paused


def test_empty_universe_keeps_previous_pauses_and_ledger(paths):
    """成分股讀檔異常（只剩 SPY/SSO）：不把 SP500 持股誤記成手動、既有暫停不解除。"""
    _eval("Rosa", {"XYZ": 10.0})
    paused = _eval("Rosa", {"XYZ": 10.0}, JOINED).paused
    r = _eval("Rosa", {"XYZ": 10.0, "AAPL": 5.0}, {"SPY", "SSO"})
    assert r.paused == paused
    r = _eval("Rosa", {"XYZ": 10.0, "AAPL": 5.0}, JOINED)
    assert "AAPL" not in r.manual and r.paused == paused


def test_corrupt_ledger_fails_closed(paths):
    paths.ledger.write_bytes(b"not a sqlite database" * 100)
    with pytest.raises(ma.ManualAttributionError):
        _eval("Rosa", {"BTC": 1.0})
    with pytest.raises(ma.ManualAttributionError):
        ma.gate_orders("Rosa", [{"symbol": "AAPL"}])


def test_empty_ledger_file_has_no_pauses(paths):
    """第一次寫入沒完成（只留下空檔）不等於損毀：沒有暫停，下次照常啟用。"""
    sqlite3.connect(paths.ledger).close()
    assert ma.paused_symbols("Rosa") == {}
    assert _eval("Rosa", {"BTC": 1.0}).manual == {"BTC": 1.0}


def test_corrupt_manual_orders_fails_closed(paths):
    paths.orders.write_bytes(b"not a sqlite database" * 100)
    with pytest.raises(ma.ManualAttributionError):
        _eval("Rosa", {"BTC": 1.0})


def test_manual_orders_reader_is_read_only(paths):
    _manual_order(paths.orders, "M:1", "Rosa", "BTC", "BUY", 1, "FILLED")
    before = paths.orders.read_bytes()
    fills = ma.read_manual_fills("Rosa")
    assert [(f.symbol, f.action, f.filled_quantity, f.status) for f in fills] == [
        ("BTC", "BUY", 1, "FILLED")]
    assert paths.orders.read_bytes() == before


def test_strategy_open_trades_skip_manual_orders():
    manual = NS(order=NS(orderRef="QUANTCORE_MANUAL:ROSA:abc"))
    strategy = NS(order=NS(orderRef=""))
    assert ma.strategy_open_trades([manual, strategy]) == [strategy]


# ── 對帳報表（rebalance_plan）────────────────────────────────────────

def _row(sym, category, delta=5):
    from rebalance_plan import DeltaRow
    return DeltaRow(sym, category, "r", 10, 10 - delta, delta, 10.0, 0.05,
                    "SP500", 0.03)


def test_mark_paused_is_identity_without_pauses():
    import rebalance_plan as plan
    rows = [_row("AAPL", "actionable")]
    assert plan.mark_paused(rows, {}) is rows


def test_paused_rows_are_not_daily_red_alarms():
    import rebalance_plan as plan
    rows = plan.mark_paused(
        [_row("XYZ", "actionable"), _row("AAPL", "aligned", 0), _row("BTC", "manual", 0)],
        {"XYZ": "手動持倉 10 股已進入 SP500 Universe", "BTC": "x"})
    assert [r.category for r in rows] == ["paused", "aligned", "manual"]
    alarms, advisories = plan.drift_alert_lines(rows)
    assert alarms == [] and advisories == []
    lines = plan.drift_report_lines(rows)
    assert "⏸ 手動持倉暫停 1 檔: XYZ+5" in lines
    assert lines[-1] == "✅ 對齊 1 檔"


def test_tg_section_sent_for_new_pause_only():
    import rebalance_ib
    assert rebalance_ib.rebalance_tg_section([], [], order_type="MOO",
                                             dry_run=False) is None
    head, body, level = rebalance_ib.rebalance_tg_section(
        [], [], order_type="MOO", dry_run=False,
        pause_notices=["⏸ XYZ 策略單暫停：手動持倉 10 股 <已進入>"])
    assert head == "沒有送單" and level == "warn"
    assert body == ["⏸ XYZ 策略單暫停：手動持倉 10 股 &lt;已進入&gt;"]


# ── 送單入口：fake IB 走真實流程 ─────────────────────────────────────

def _sent(broker):
    return [(p.symbol, p.action, p.qty) for p in broker.placed]


def _cancelled(broker):
    return [o.orderId for o in broker.cancelled]


def _fake_broker(positions, behavior="fill"):
    """每條連線一個 in-memory Broker：手動單 7 與策略單 8 掛在 Gateway 上。
    behavior="working"：送出的單一直掛著、沒有成交（MOO 開盤前的樣子）。"""
    from tests.fake_broker import FakeBroker
    fake = FakeBroker(managed=["U00000001"], positions=positions, behavior=behavior)
    fake.open_orders = [MANUAL_OPEN, STRATEGY_OPEN]
    fake.detail = []
    return fake


MANUAL_OPEN = NS(order=NS(orderId=7, orderRef="QUANTCORE_MANUAL:ROSA:abc"),
                 contract=NS(symbol="BTC"))
STRATEGY_OPEN = NS(order=NS(orderId=8, orderRef=""), contract=NS(symbol="AAPL"))


def _ctx(name="Rosa", dry_run=False, tg=("", "")):
    from account_ctx import AccountCtx
    return AccountCtx(
        name=name, account="U00000001", host="127.0.0.1", port=4999,
        client_id=99, dry_run=dry_run, order_type="MOO", outside_rth=False,
        wait_fill_sec=0, alloc_sr=0.0, alloc_sp500=0.5, alloc_skewls=0.0,
        price_feed_port=None, tg_token=tg[0], tg_chat_id=tg[1],
    )


@pytest.fixture
def rebalance_env(paths, monkeypatch):
    """rebalance_ib.main 的真實流程；訊號固定：SP500 想要 AAPL 250 股、XYZ 250 股。"""
    import notify
    import rebalance_ib
    import signals

    env = NS(sp500_calls=[], broker=None, positions={"AAPL": 100.0, "XYZ": 10.0, "BTC": 1.5},
             sent=[], tg_ok=True)

    def tg_send(msg, *, token, chat_id):
        env.sent.append(msg)
        return env.tg_ok

    monkeypatch.setattr(notify, "tg_send", tg_send)

    def connect(ctx, **_kw):
        env.broker = _fake_broker(env.positions, behavior="working")
        return env.broker.connect(ctx)

    def sp500_weights(holdings, account_name, persist=True):
        env.sp500_calls.append(list(holdings))
        return {"AAPL": 0.5, "XYZ": 0.5}, "2026-09-23"

    import ib_fetcher
    monkeypatch.setattr(ib_fetcher, "connect", connect)
    monkeypatch.setattr(rebalance_ib, "append_trade_log", lambda *a, **k: None)
    monkeypatch.setattr(signals, "get_sr_weights", lambda: ({}, "N/A"))
    monkeypatch.setattr(signals, "get_sp500_weights", sp500_weights)
    monkeypatch.setattr(signals, "get_sp500_universe", lambda historical=True: set(JOINED))
    return env


def _run_rebalance(name="Rosa", mode="sp500"):
    """跑一個帳戶並把那一則 Telegram 發出去（tg_send 是假的）。"""
    import rebalance_ib
    from notify import Digest
    digest = Digest("調倉 T", icon="📤")
    rebalance_ib.main(mode=mode, ctx=_ctx(name, tg=("T", "C")), digest=digest)
    digest.send()
    return digest


def test_rebalance_holds_only_the_paused_symbol(rebalance_env, caplog):
    # 啟用時 XYZ 還是策略外（手動）；之後 XYZ 進入 SP500 Universe
    ma.evaluate("Rosa", {"AAPL": 100.0, "XYZ": 10.0, "BTC": 1.5}, UNIVERSE)
    caplog.set_level("INFO")
    digest = _run_rebalance()

    assert _sent(rebalance_env.broker) == [("AAPL", "BUY", 150)]      # XYZ +240 被擋
    assert _cancelled(rebalance_env.broker) == [8]                       # 手動單 7 沒被取消
    assert rebalance_env.sp500_calls == [["AAPL", "XYZ"]]          # 訊號輸入不變
    text = digest.render()
    assert "⏸ XYZ 策略單暫停：手動持倉 10 股已進入 SP500 Universe" in text
    assert "🔴" not in text
    assert "⏸ 手動持倉暫停 1 檔: XYZ+240" in caplog.text

    assert len(rebalance_env.sent) == 1

    # 同一次暫停只通知一次；AAPL 之後已對齊 → 安靜日沒有訊息
    rebalance_env.positions["AAPL"] = 250.0
    assert _run_rebalance().render() is None
    assert _sent(rebalance_env.broker) == [] and len(rebalance_env.sent) == 1


def test_rebalance_pause_notice_retried_when_telegram_fails(rebalance_env):
    """21:00 那則沒送到 → 暫停不算已通知，下一輪再附上；送到後才停。"""
    ma.evaluate("Rosa", {"AAPL": 100.0, "XYZ": 10.0, "BTC": 1.5}, UNIVERSE)
    rebalance_env.positions["AAPL"] = 250.0
    rebalance_env.tg_ok = False
    assert "⏸ XYZ" in _run_rebalance().render()
    rebalance_env.tg_ok = True
    assert "⏸ XYZ" in _run_rebalance().render()
    assert _run_rebalance().render() is None


def test_rebalance_without_manual_record_is_unchanged(rebalance_env):
    """沒有手動紀錄的帳戶：XYZ 是策略持股，照常送單（啟用當下就在 Universe 內）。"""
    _run_rebalance("Taylor")
    assert _sent(rebalance_env.broker) == [("AAPL", "BUY", 150), ("XYZ", "BUY", 240)]


def test_rebalance_unreadable_ledger_stops_account_before_state(rebalance_env, paths):
    paths.ledger.write_bytes(b"corrupt" * 100)
    import rebalance_ib
    from notify import Digest
    digest = Digest("調倉 T", icon="📤")
    with pytest.raises(ma.ManualAttributionError):
        rebalance_ib.main(mode="sp500", ctx=_ctx(), digest=digest)
    assert _sent(rebalance_env.broker) == [] and rebalance_env.sp500_calls == []
    assert digest.outcomes(["Rosa"])["Rosa"][0] == "error"


# 21:00 的限價（#185 第三階段）：MOO 不帶限價，交易紀錄的預估價 = 算股數用的參考價；
# 以 LMT 下單時走 Broker 的統一限價規則（原本的 −0.05%／+0.05% 已刪）

def test_rebalance_moo_records_the_reference_price_as_the_estimate(rebalance_env, monkeypatch):
    import rebalance_ib
    logged = []
    monkeypatch.setattr(rebalance_ib, "append_trade_log",
                        lambda placed, *a, **k: logged.extend(placed))
    _run_rebalance("Taylor")
    assert [(o["symbol"], o["lmt_price"]) for o in logged] == [("AAPL", 100.0), ("XYZ", 100.0)]
    assert {p.order_type for p in rebalance_env.broker.placed} == {"MKT"}      # MOO


def test_rebalance_lmt_run_uses_the_unified_limit_rule(rebalance_env):
    import dataclasses

    import rebalance_ib
    from notify import Digest
    ctx = dataclasses.replace(_ctx("Taylor"), order_type="LMT")
    rebalance_ib.main(mode="sp500", ctx=ctx, digest=Digest("調倉 T", icon="📤"))
    placed = rebalance_env.broker.placed
    assert [(p.symbol, p.order_type, p.lmt) for p in placed] == [
        ("AAPL", "LMT", round(100.1 * 1.003, 2)), ("XYZ", "LMT", round(100.1 * 1.003, 2))]


def test_send_gate_holds_persisted_pause_even_if_caller_forgot(paths):
    import strategy_orders
    ma.evaluate("Rosa", {"XYZ": 10.0}, UNIVERSE)
    ma.evaluate("Rosa", {"XYZ": 10.0}, JOINED)
    fake = _fake_broker({})
    placed = strategy_orders.send(
        fake.connect(_ctx()), [{"symbol": "XYZ", "action": "SELL", "qty": 10},
                               {"symbol": "AAPL", "action": "BUY", "qty": 1}], _ctx()).placed
    assert [o["symbol"] for o in placed] == ["AAPL"]
    assert _sent(fake) == [("AAPL", "BUY", 1)]


@pytest.fixture
def sync_env(paths, monkeypatch):
    import signals

    import sync_position

    env = NS(broker=None, positions={"AAPL": 100.0, "XYZ": 10.0}, sent=[])

    def connect(ctx):
        env.broker = _fake_broker(env.positions)
        return env.broker.connect(ctx)

    # 實盤送單目標給的連線與紀錄（#183：沒有預設，一律明確給）
    env.records = sync_position.SyncRecords(
        connect=connect, attribution_ledger=paths.ledger, manual_orders_store=paths.orders,
        persist_strategy_state=True)
    monkeypatch.setattr(signals, "get_sr_weights", lambda: ({}, "N/A"))
    monkeypatch.setattr(signals, "get_sp500_weights",
                        lambda holdings, name: ({"AAPL": 0.5, "XYZ": 0.5}, "2026-09-23"))
    monkeypatch.setattr(signals, "get_sp500_universe", lambda historical=True: set(JOINED))
    return env


def test_sync_pos_previews_paused_orders_and_never_sends_them(sync_env):
    import sync_position
    ma.evaluate("Rosa", {"AAPL": 100.0, "XYZ": 10.0}, UNIVERSE)
    plan = sync_position.compute_sync_plan(_ctx(), records=sync_env.records)
    assert [(o.symbol, o.qty) for o in plan.orders] == [("AAPL", 150)]
    assert [(o.symbol, o.qty) for o in plan.paused_orders] == [("XYZ", 240)]
    assert "手動持倉 10 股" in plan.paused["XYZ"]

    sent = []
    assert sync_position.notify_new_pauses(plan, send=lambda m: sent.append(m) or True)
    assert "XYZ 策略單暫停" in sent[0]
    assert sync_position.compute_sync_plan(_ctx(), records=sync_env.records).pause_notices == []

    placed, _ = sync_position.submit_sync_orders(plan)          # 沿用計算時的連線與紀錄
    assert [o["symbol"] for o in placed] == ["AAPL"]
    assert _sent(sync_env.broker) == [("AAPL", "BUY", 150)]
    assert [o.orderId for o in sync_env.broker.cancelled] == [8]



def test_paper_sync_records_never_touch_live_records(sync_env, paths, tmp_path, monkeypatch):
    """新 Panel 的 paper Sync Pos（#152）：用 paper 自己的歸屬紀錄與手動單紀錄、
    SP500／SkewLS state 只讀不寫；實盤帳戶的紀錄一個 byte 都不變。"""
    import signals
    import skewls_state
    import sync_position
    from dataclasses import replace

    ma.evaluate("Rosa", {"AAPL": 100.0, "XYZ": 10.0}, UNIVERSE)
    ma.evaluate("Rosa", {"AAPL": 100.0, "XYZ": 10.0}, JOINED)      # 實盤：XYZ 暫停中
    assert "XYZ" in ma.paused_symbols("Rosa")
    live_before = paths.ledger.read_bytes()
    persist_args, skew_saves, paper_ibs = [], [], []
    monkeypatch.setattr(signals, "get_sp500_weights",
                        lambda holdings, name, persist=True: (
                            persist_args.append(persist)
                            or ({"AAPL": 0.5, "XYZ": 0.5}, "2026-09-23")))
    monkeypatch.setattr(signals, "get_skewls_weights", lambda: ({"SH": 0.1}, "2026-09-23"))
    monkeypatch.setattr(skewls_state, "save_state", lambda *a: skew_saves.append(a))

    def paper_connect(ctx):
        paper_ibs.append(_fake_broker({"AAPL": 100.0, "XYZ": 10.0}))
        return paper_ibs[-1].connect(ctx)

    records = sync_position.SyncRecords(
        connect=paper_connect, attribution_ledger=tmp_path / "paper_attribution.sqlite3",
        manual_orders_store=tmp_path / "paper_orders.sqlite3", persist_strategy_state=False)
    plan = sync_position.compute_sync_plan(replace(_ctx(), alloc_skewls=0.2), records=records)
    assert plan.paused == {}                    # 實盤 Rosa 的暫停不套到 paper
    placed, _ = sync_position.submit_sync_orders(plan)

    assert {o["symbol"] for o in placed} >= {"XYZ"}
    assert ("XYZ", "BUY", 240) in _sent(paper_ibs[-1])
    assert sync_env.broker is None              # 沒有經過實盤連線
    assert persist_args == [False] and skew_saves == []
    assert paths.ledger.read_bytes() == live_before
    assert (tmp_path / "paper_attribution.sqlite3").exists()

_PREVIEW_SCRIPT = r"""
import json, sys, tkinter as tk
sys.path[:0] = sys.argv[1:3]
from account_ctx import AccountCtx
from rebalance_plan import PlannedOrder
from sync_position import SyncPlan
import sync_preview_window as spw

tk.Toplevel.grab_set = lambda self: None
root = tk.Tk(); root.withdraw()
ctx = AccountCtx(name="Rosa", account="U0", host="127.0.0.1", port=1, client_id=1,
                 dry_run=True, order_type="LMT", outside_rth=True, wait_fill_sec=0,
                 alloc_sr=0.0, alloc_sp500=0.5, alloc_skewls=0.0, price_feed_port=None,
                 tg_token="", tg_chat_id="")
plan = SyncPlan(
    ctx=ctx, orders=[PlannedOrder("AAPL", "BUY", 150, 100.0, "SP500", 250, 100)],
    nav=100000.0, sr_wgt={}, sp500_wgt={"AAPL": 0.5, "XYZ": 0.5}, sr_date="N/A",
    sp500_date="D", prices={"AAPL": 100.0, "XYZ": 100.0},
    targets={"AAPL": {"shares": 250, "strategy": "SP500", "alloc_w": 0.25},
             "XYZ": {"shares": 250, "strategy": "SP500", "alloc_w": 0.25}},
    current_pos={"AAPL": 100.0, "XYZ": 10.0}, current_other_holdings=[],
    paused={"XYZ": "手動持倉 10 股已進入 SP500 Universe"},
    paused_orders=[PlannedOrder("XYZ", "BUY", 240, 100.0, "SP500", 250, 10)])
logs = []
theme = dict.fromkeys(("bg", "bg2", "bg3", "accent", "accent_hl", "text",
                       "text_dim", "blue", "green", "red", "btn_bg"), "#000000")
w = spw.SyncPreviewWindow(parent=root, account_name="Rosa", theme=theme,
                          log=logs.append, on_confirm=lambda p: None,
                          btn_factory=lambda parent, text, cmd, *a, **k:
                              tk.Button(parent, text=text, command=cmd))
w.show_computing(); root.update(); w.show_plan(plan); root.update()

def texts(widget):
    for child in widget.winfo_children():
        if isinstance(child, (tk.Label, tk.Button)):
            yield child.cget("text")
        yield from texts(child)

print(json.dumps({"labels": list(texts(w.win)), "logs": logs}, ensure_ascii=False))
root.destroy()
"""


def test_sync_pos_preview_window_shows_pause_reason():
    """Panel 顯示原因：預覽視窗列出暫停標的、原本要下的單與原因；交易清單只剩可送的單。
    Tk 在自己的行程裡跑（同行程第二個 Tk root 不可靠，見 test_panel_gui.own_process）。"""
    import json
    import subprocess
    import sys
    from pathlib import Path

    automation = Path(__file__).resolve().parents[1]
    done = subprocess.run(
        [sys.executable, "-c", _PREVIEW_SCRIPT, str(automation), str(automation.parent)],
        capture_output=True, timeout=120, env={**__import__("os").environ,
                                               "PYTHONIOENCODING": "utf-8"})
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[-2000:]
    out = json.loads(done.stdout.decode("utf-8"))
    labels = out["labels"]
    assert "⏸ 手動持倉暫停（這些標的的策略單不會送出）" in labels
    assert any("XYZ" in t and "原本要 BUY 240sh (10→250)" in t for t in labels)
    assert "    手動持倉 10 股已進入 SP500 Universe" in labels
    assert "Required Trades (需要交易: 1 筆)" in labels
    assert "  ⏸ XYZ 策略單暫停：手動持倉 10 股已進入 SP500 Universe" in out["logs"]


def test_sync_pos_notice_not_marked_when_telegram_fails(sync_env):
    import sync_position
    ma.evaluate("Rosa", {"AAPL": 100.0, "XYZ": 10.0}, UNIVERSE)
    plan = sync_position.compute_sync_plan(_ctx(), records=sync_env.records)
    assert not sync_position.notify_new_pauses(plan, send=lambda m: False)
    assert (sync_position.compute_sync_plan(_ctx(), records=sync_env.records).pause_notices
            == plan.pause_notices)


def test_sync_pos_without_records_refuses_before_connecting(sync_env):
    """#183：沒有「預設實盤」— 忘了給連線與紀錄就報錯，不會不知情地繞過送單閘門。"""
    from dataclasses import replace

    import sync_position

    with pytest.raises(TypeError):
        sync_position.compute_sync_plan(_ctx())
    with pytest.raises(TypeError, match="SyncRecords"):
        sync_position.compute_sync_plan(_ctx(), records=None)
    plan = sync_position.compute_sync_plan(_ctx(), records=sync_env.records)
    sync_env.broker = None
    unrecorded = replace(plan, records=None)
    with pytest.raises(TypeError, match="SyncRecords"):
        sync_position.submit_sync_orders(unrecorded)
    with pytest.raises(TypeError, match="SyncRecords"):
        sync_position.submit_sync_orders(replace(unrecorded, orders=[]))
    with pytest.raises(TypeError, match="SyncRecords"):
        sync_position.notify_new_pauses(unrecorded, send=lambda m: True)
    assert sync_env.broker is None              # 一條連線都沒開


def test_deliver_notices_marks_the_ledger_it_was_given(paths, tmp_path):
    """「送通知並記已通知」記在呼叫端指定的那份紀錄（讀出通知的那一份），不碰實盤預設檔。"""
    other = tmp_path / "paper_attribution.sqlite3"
    ma.evaluate("Rosa", {"XYZ": 10.0}, UNIVERSE, ledger_path=other)
    notices = ma.evaluate("Rosa", {"XYZ": 10.0}, JOINED, ledger_path=other).new_notices
    assert notices
    with pytest.raises(TypeError):
        ma.deliver_notices(_ctx(), notices, lambda m: True)
    assert ma.deliver_notices(_ctx(), notices, lambda m: True, path=other)
    assert ma.pending_notices("Rosa", path=other) == []
    assert not paths.ledger.exists()


class _Tue0915(date):
    @classmethod
    def today(cls):
        return date(2026, 9, 15)


@pytest.fixture
def dip_env(paths, fake_config, monkeypatch, tmp_path):
    """sp500_daily_check 熊市 dip-buy 觸發（SPY 回撤 16%）。"""
    import pandas as pd
    import ib_fetcher
    import signals
    import sp500_daily_check
    import sp500_state
    from strategies.sp500_momentum import utils

    env = NS(broker=None)
    monkeypatch.setattr(sp500_state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(sp500_daily_check, "date", _Tue0915)
    monkeypatch.setattr(utils, "load_benchmark", lambda path: pd.DataFrame(
        {"Close": [750.0, 757.3]}, index=pd.to_datetime(["2026-09-11", "2026-09-14"])))
    monkeypatch.setattr(signals, "get_market_regime_state", lambda ref: {
        "SPY_Close": 500.0, "SPY_MA200": 600.0, "SPY_DD": -0.16})
    monkeypatch.setattr(signals, "get_sp500_universe", lambda historical=True: set(UNIVERSE))

    def connect(ctx, **_kw):
        env.broker = _fake_broker({"AAPL": 10.0}, behavior="working")
        return env.broker.connect(ctx)

    monkeypatch.setattr(ib_fetcher, "connect", connect)
    env.state_file = tmp_path / "sp500_state_Taylor.json"
    return env


def _daily(name="Taylor"):
    import sp500_daily_check
    from notify import Digest
    d = Digest("日度風控 T", icon="📤")
    sp500_daily_check.run_daily_check(name, d)
    return d


@pytest.fixture
def dip_ctx(monkeypatch):
    import account_ctx
    monkeypatch.setattr(account_ctx.AccountCtx, "from_name",
                        classmethod(lambda cls, name, role: _ctx(name)))


def test_daily_check_dip_buy_keeps_manual_orders(dip_env, dip_ctx):
    from strategies.sp500_momentum import config as sp_cfg
    _daily()
    assert _cancelled(dip_env.broker) == [8]                      # 手動單 7 沒被取消
    assert [p[0] for p in _sent(dip_env.broker)] == [sp_cfg.DIP_BUY_TICKER]
    assert dip_env.state_file.exists()


def test_daily_check_unreadable_ledger_keeps_dip_trigger_for_tomorrow(
        dip_env, dip_ctx, paths):
    paths.ledger.write_bytes(b"corrupt" * 100)
    with pytest.raises(ma.ManualAttributionError):
        _daily()
    assert _sent(dip_env.broker) == [] and _cancelled(dip_env.broker) == []
    assert not dip_env.state_file.exists()                        # dip 觸發沒被消耗
