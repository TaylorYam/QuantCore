"""
preview_rebalance（離線預演）— 安全保證 + 決策鏈 pin tests。

安全保證是本模組的存在理由，逐條考題化：
  1. 零 state 寫入 — STATE_DIR 指到 tmp 後跑完整 sp500 / skewls 流程，
     檔案系統必須一個 json 都沒有（調倉輸入以 persist_state=False 取 SP500
     訊號；本來會寫的 state 由報表揭露）
  2. ADR-0004 hold — 非 sp500 mode 不得呼叫 SP500 訊號、持股 target=current
  3. SkewLS 凍結防護 — 非 skewls mode 沿用 last_target 進 combine
     （SR 預演不得砍 SkewLS 倉）；threshold 未觸發 = hold ≠ 凍結
  4. 決策數字 pin — sizing / 3% 門檻 / no_price / manual 排除
     全部走真 rebalance_plan（combine 內部不動，ADR-0008）

訊號一律 monkeypatch fixtures（不需 data CSV / accounts.json / IB）。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest


def _make_ctx(alloc_sr: float = 0.80, alloc_sp500: float = 0.50,
              alloc_skewls: float = 0.0):
    """Build an AccountCtx directly without going through from_name()."""
    from account_ctx import AccountCtx
    return AccountCtx(
        name="Test",
        account="U00000000",
        host="127.0.0.1",
        port=4001,
        client_id=20,
        dry_run=True,
        order_type="MOO",
        outside_rth=False,
        wait_fill_sec=30,
        alloc_sr=alloc_sr,
        alloc_sp500=alloc_sp500,
        alloc_skewls=alloc_skewls,
        price_feed_port=4002,
        tg_token="",
        tg_chat_id="",
    )


@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    """State 目錄指到 tmp — 檔案系統層驗證『預演零寫入』。"""
    import sp500_state
    import skewls_state
    monkeypatch.setattr(sp500_state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(skewls_state, "STATE_DIR", tmp_path)
    return tmp_path


def _patch_signals(monkeypatch, sr=None, universe=None, skewls=None):
    """signals 模組層 fixtures（preview 透過 signals.* 呼叫 → attr patch 生效）。"""
    import signals
    monkeypatch.setattr(signals, "get_sr_weights",
                        lambda: (dict(sr or {}), "2026-07-03"))
    monkeypatch.setattr(signals, "get_sp500_universe",
                        lambda historical=True: set(universe or set()))
    if skewls is not None:
        monkeypatch.setattr(signals, "get_skewls_weights",
                            lambda as_of_date=None: (dict(skewls), "2026-07-03"))


def _patch_sp500_live(monkeypatch, wgt):
    """
    patch 純策略函式（state 由 caller 傳入/回傳）— 讓真的
    signals.get_sp500_weights bridge（load → 純函式 → save）原樣執行，
    以驗證 save_state 攔截真的攔在 live 路徑上。
    """
    import strategies.sp500_momentum.signal as sp_signal

    def fake_live(current_holdings, state, as_of_date=None):
        new_state = {**state, "bull_weeks_counter": 2}
        return dict(wgt), pd.Timestamp("2026-07-03"), new_state

    monkeypatch.setattr(sp_signal, "get_latest_weights_live", fake_live)


# ── 1. 零 state 寫入保證 ──────────────────────────────────────────────

def test_sp500_save_state_intercepted_filesystem_clean(isolated_state,
                                                       monkeypatch):
    """sp500 mode 走真 get_sp500_weights（每呼叫必 save）→ 攔截記錄 + 零檔案。"""
    import sp500_state
    from preview_rebalance import compute_preview

    orig_save = sp500_state.save_state
    _patch_signals(monkeypatch, sr={}, universe={"MU"})
    _patch_sp500_live(monkeypatch, {"MU": 1.0})

    res = compute_preview(
        "sp500", _make_ctx(), nav=10_000, positions={"MU": 3.0},
        snapshot_prices={"MU": 100.0},
    )

    assert res.blocked_state_writes == ["sp500_state:Test"]
    assert list(isolated_state.glob("*")) == []          # 檔案系統零寫入
    assert sp500_state.save_state is orig_save           # guard 已還原
    assert res.sp500_date == "2026-07-03"
    # target = round(10000×0.5×1.0/100) = 50、current 3 → BUY 47（真 combine）
    assert [(o.symbol, o.action, o.qty) for o in res.orders] == [
        ("MU", "BUY", 47)]
    assert res.orders[0].strategy == "SP500"


def test_skewls_trigger_never_writes_state(isolated_state, monkeypatch):
    """skewls mode 觸發 threshold — live 會走 step 10 寫 state，預演必須不寫。"""
    from preview_rebalance import compute_preview

    _patch_signals(monkeypatch, sr={}, universe=set(),
                   skewls={"REW": 0.8, "XLC": -0.4})

    res = compute_preview(
        "skewls", _make_ctx(alloc_skewls=0.30), nav=100_000, positions={},
        snapshot_prices={"REW": 20.0, "XLC": 60.0},
    )

    assert res.skewls is not None
    assert res.skewls.triggered is True                  # Δ=1.2 > 0.40
    assert list(isolated_state.glob("*")) == []          # 觸發也零寫入
    assert res.blocked_state_writes == []                # 且根本沒有 save 嘗試
    # REW: round(100000×0.3×0.8/20)=+1200 建倉 BUY；XLC: −200 建倉 SHORT→SELL
    assert [(o.symbol, o.action, o.qty) for o in res.orders] == [
        ("REW", "BUY", 1200), ("XLC", "SELL", 200)]
    assert res.orders[1].target == -200                  # 負 target（short）


def test_skewls_threshold_hold_is_not_frozen(isolated_state, monkeypatch):
    """threshold 未觸發 = 沿用 last_target 維持倉位（hold ≠ 凍結）、零單零寫。"""
    import skewls_state
    from preview_rebalance import compute_preview

    _patch_signals(monkeypatch, sr={}, universe=set(),
                   skewls={"REW": 0.8, "XLC": -0.4})
    monkeypatch.setattr(
        skewls_state, "load_state",
        lambda name: {"last_target": {"REW": 0.8, "XLC": -0.4},
                      "last_target_date": "2026-06-20",
                      "last_rebal_date": "2026-06-20"})

    res = compute_preview(
        "skewls", _make_ctx(alloc_skewls=0.30), nav=100_000,
        positions={"REW": 1200.0, "XLC": -200.0},
        snapshot_prices={"REW": 20.0, "XLC": 60.0},
    )

    assert res.skewls.triggered is False                 # Δ=0 ≤ 0.40
    assert res.skewls.wgt == {"REW": 0.8, "XLC": -0.4}   # 沿用 last_target
    assert res.orders == []                              # 已在目標上
    assert list(isolated_state.glob("*")) == []


# ── 2. ADR-0004：非 sp500 mode 的 hold 防護 ──────────────────────────

def test_sr_mode_holds_sp500_and_never_calls_signal(isolated_state,
                                                    monkeypatch):
    """mode=sr：SP500 訊號不得被呼叫（週二舊訊號 whipsaw 防護）、持股 hold。"""
    import strategies.sp500_momentum.signal as sp_signal
    from preview_rebalance import compute_preview

    _patch_signals(monkeypatch, sr={"XLK": 1.0}, universe={"MU"})

    def _must_not_call(*a, **kw):
        raise AssertionError("mode=sr 不得計算 SP500 訊號（ADR-0004）")

    monkeypatch.setattr(sp_signal, "get_latest_weights_live", _must_not_call)

    res = compute_preview(
        "sr", _make_ctx(), nav=100_000, positions={"MU": 10.0},
        snapshot_prices={"XLK": 100.0, "MU": 50.0},
    )

    assert res.blocked_state_writes == []                # 訊號沒跑 → 無 save 嘗試
    assert res.sp500_date == "N/A"
    # MU hold：target=current=10 → aligned，不出單
    mu_rows = [r for r in res.rows if r.symbol == "MU"]
    assert mu_rows[0].category == "aligned"
    assert mu_rows[0].strategy == "SP500 (hold)"
    # SR 建倉正常出單：int(100000×0.8×1.0/100)=800
    assert [(o.symbol, o.action, o.qty) for o in res.orders] == [
        ("XLK", "BUY", 800)]


# ── 3. SkewLS 凍結防護：非 skewls mode 沿用 last_target ───────────────

def test_sr_mode_keeps_skewls_last_target(isolated_state, monkeypatch):
    """SR 預演不得砍 SkewLS 倉：last_target 進 combine、triggered=None。"""
    import skewls_state
    from preview_rebalance import compute_preview

    _patch_signals(monkeypatch, sr={}, universe=set())
    monkeypatch.setattr(
        skewls_state, "load_state",
        lambda name: {"last_target": {"REW": 0.5},
                      "last_target_date": "2026-06-20",
                      "last_rebal_date": "2026-06-20"})

    res = compute_preview(
        "sr", _make_ctx(alloc_skewls=0.30), nav=100_000, positions={},
        snapshot_prices={"REW": 20.0},
    )

    assert res.skewls.triggered is None                  # 本 mode 不檢查
    assert res.skewls.signal_date == "2026-06-20"
    # round(100000×0.3×0.5/20) = 750 → 維持 SkewLS 目標倉
    assert [(o.symbol, o.action, o.qty) for o in res.orders] == [
        ("REW", "BUY", 750)]


# ── 4. 決策數字 pin（真 rebalance_plan；ADR-0008 內部不動）────────────

def test_threshold_skip_and_entry_order(isolated_state, monkeypatch):
    """3% 門檻內漂移 skip；建倉不受門檻限制 — 與 scheduled rebalance 同腦。"""
    from preview_rebalance import compute_preview

    _patch_signals(monkeypatch, sr={"XLK": 0.5, "XLF": 0.5}, universe=set())

    res = compute_preview(
        "sr", _make_ctx(), nav=100_000, positions={"XLK": 390.0},
        snapshot_prices={"XLK": 100.0, "XLF": 50.0},
    )

    # XLK: target int(100000×0.8×0.5/100)=400、drift 1% < 3% → 門檻內不動作
    xlk = [r for r in res.rows if r.symbol == "XLK"][0]
    assert xlk.category == "within_threshold"
    # XLF: 建倉 800 股，不受門檻限制
    assert [(o.symbol, o.action, o.qty) for o in res.orders] == [
        ("XLF", "BUY", 800)]


def test_snapshot_price_priority_and_csv_fallback(isolated_state, monkeypatch):
    """snapshot 報價優先；缺的走 price_lookup；仍缺 → no_price 跳過不下單。"""
    from preview_rebalance import compute_preview

    _patch_signals(monkeypatch, sr={}, universe={"MU", "WDC"})
    _patch_sp500_live(monkeypatch, {"MU": 0.6, "WDC": 0.4})

    res = compute_preview(
        "sp500", _make_ctx(), nav=10_000, positions={"WDC": 5.0},
        snapshot_prices={"MU": 42.0},
        price_lookup=lambda s: None,                     # 模擬本地 CSV 缺檔
    )

    assert res.missing_prices == ["WDC"]
    wdc = [r for r in res.rows if r.symbol == "WDC"][0]
    assert wdc.category == "no_price"
    # MU 用 snapshot 價：round(10000×0.5×0.6/42)=71
    assert [(o.symbol, o.action, o.qty) for o in res.orders] == [
        ("MU", "BUY", 71)]


def test_manual_holding_excluded_even_without_price(isolated_state,
                                                    monkeypatch):
    """手動持倉（BTC）永不下單 — 無報價也只進 manual 對帳列。"""
    from preview_rebalance import compute_preview

    _patch_signals(monkeypatch, sr={}, universe={"MU"})
    _patch_sp500_live(monkeypatch, {"MU": 1.0})

    res = compute_preview(
        "sp500", _make_ctx(), nav=10_000,
        positions={"MU": 3.0, "BTC": 65.1783},
        snapshot_prices={"MU": 100.0},
        price_lookup=lambda s: None,
    )

    assert res.split.other == ["BTC"]
    assert all(o.symbol != "BTC" for o in res.orders)
    btc = [r for r in res.rows if r.symbol == "BTC"][0]
    assert btc.category == "manual"
    assert btc.target is None


def test_invalid_mode_and_nav_rejected():
    from preview_rebalance import compute_preview

    with pytest.raises(ValueError, match="mode"):
        compute_preview("weekly", _make_ctx(), 1000, {})
    with pytest.raises(ValueError, match="NAV"):
        compute_preview("sr", _make_ctx(), -5, {})
    with pytest.raises(ValueError, match="NAV"):
        compute_preview("sr", _make_ctx(), float("nan"), {})


# ── 5. 報價 fallback 單元 ─────────────────────────────────────────────

def test_resolve_prices_priority_chain():
    from preview_rebalance import resolve_prices

    prices, missing = resolve_prices(
        ["AAA", "BBB", "CCC", "DDD"],
        snapshot_prices={"AAA": 10.0, "BBB": 0.0},       # BBB 非正數 → 視同缺
        price_lookup=lambda s: {"BBB": 20.0, "CCC": 30.0}.get(s),
    )
    assert prices == {"AAA": 10.0, "BBB": 20.0, "CCC": 30.0, "DDD": 0.0}
    assert missing == ["DDD"]


# ── 6. CLI 解析 + 端到端（fake accounts.json）─────────────────────────

def test_parse_positions_formats():
    from preview_rebalance import parse_positions

    assert parse_positions("XLU=50, btc=65.1783 xlk=3") == {
        "XLU": 50.0, "BTC": 65.1783, "XLK": 3.0}
    assert parse_positions("") == {}
    with pytest.raises(ValueError, match="SYM=QTY"):
        parse_positions("XLU")


def test_load_snapshot_roundtrip(tmp_path):
    from preview_rebalance import load_snapshot

    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({
        "nav": 16401.49,
        "positions": {"xlu": 50, "BTC": 65.1783},
        "prices": {"xlu": 82.1},
    }), encoding="utf-8")
    nav, positions, prices = load_snapshot(snap)
    assert nav == 16401.49
    assert positions == {"XLU": 50.0, "BTC": 65.1783}
    assert prices == {"XLU": 82.1}

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"positions": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="nav"):
        load_snapshot(bad)


def test_cli_snapshot_end_to_end(isolated_state, monkeypatch, tmp_path,
                                 capsys, fake_config):
    """main()：snapshot 模式 + --json 輸出 + 唯讀 from_name（fake accounts）。"""
    import preview_rebalance as pv

    _patch_signals(monkeypatch, sr={"XLK": 1.0}, universe=set())

    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({
        "nav": 50_000,
        "positions": {"XLK": 300},
        "prices": {"XLK": 100.0},
    }), encoding="utf-8")
    out = tmp_path / "preview.json"

    rc = pv.main(["--account", "Taylor", "--mode", "sr",
                  "--snapshot", str(snap), "--json", str(out)])

    assert rc == 0
    text = capsys.readouterr().out
    assert "預期訂單" in text
    assert "未連 IB / 未寫 state / 未下單" in text

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["account"] == "Taylor"
    assert data["mode"] == "sr"
    # target int(50000×0.8×1.0/100)=400、current 300 → BUY 100（drift 20%）
    assert [(o["symbol"], o["action"], o["qty"]) for o in data["orders"]] == [
        ("XLK", "BUY", 100)]
    assert data["blocked_state_writes"] == []
    # snap.json / preview.json 是本測試自己的輸入輸出 — 只驗證零 state 檔
    assert list(isolated_state.glob("*state_*.json")) == []


def test_cli_snapshot_exclusive_with_manual_inputs(tmp_path):
    """--snapshot 與 --nav/--pos 互斥 → argparse error（exit 2）。"""
    import preview_rebalance as pv

    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"nav": 1, "positions": {}}), encoding="utf-8")
    with pytest.raises(SystemExit):
        pv.main(["--account", "Taylor", "--snapshot", str(snap),
                 "--nav", "1000"])
    with pytest.raises(SystemExit):
        pv.main(["--account", "Taylor", "--nav", "1000"])   # 缺 --pos
