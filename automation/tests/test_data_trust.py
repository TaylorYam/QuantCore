"""
資料可信度（data/data_trust，#235）＋三個 caller.

使用者 2026-10-22 選 B：缺檔／壞檔／沒更新的成分股，排名照跑、不改送單，只在 21:00
Telegram 與 log 列出；04:30 資料更新寫一份結論，連續 3 個交易日沒有新 bar 的檔列進
失敗通知。No IB, no network, no Telegram.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from data import data_trust as dt

HEADER = "Date,Open,High,Low,Close,Adj Close,Volume\n"
SESSION = "2026-10-22"                    # 週四；10-16 是前一週週五


def _write(folder: Path, stem: str, days: list[str]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.csv"
    path.write_text(HEADER + "".join(f"{d},1,1,1,10,10,1\n" for d in days), encoding="utf-8")
    return path


# ── 判斷規則 ─────────────────────────────────────────────────────────

def test_a_file_with_the_session_bar_is_trusted(tmp_path):
    _write(tmp_path, "AAA", ["2026-10-21", SESSION, "2026-10-23"])
    t = dt.trust(tmp_path, "AAA", SESSION)
    assert t.ok and t.last_session == SESSION              # 之後的 bar 不算


def test_a_file_that_stopped_updating_is_stale(tmp_path):
    _write(tmp_path, "PSKY", ["2026-10-15", "2026-10-16"])
    t = dt.trust(tmp_path, "PSKY", SESSION)
    assert (t.status, t.last_session) == (dt.STALE, "2026-10-16")
    assert t.describe() == "PSKY 沒更新（最後 2026-10-16）"
    assert dt.sessions_behind(t, SESSION) == 4             # 10-19、20、21、22


def test_missing_and_broken_files(tmp_path):
    (tmp_path / "BAD.csv").write_text("not,a,price\nfile\n", encoding="utf-8")
    _write(tmp_path, "NEW", ["2026-10-23"])                # 只有 session 之後的 bar

    missing = dt.trust(tmp_path, "GONE", SESSION)
    assert (missing.status, missing.describe()) == (dt.MISSING, "GONE 缺檔")
    assert dt.trust(tmp_path, "BAD", SESSION).status == dt.BROKEN
    late = dt.trust(tmp_path, "NEW", SESSION)
    assert late.describe() == "NEW 壞檔（沒有 2026-10-22 以前的 bar）"
    assert dt.sessions_behind(missing, SESSION) is None


def test_sessions_behind_counts_nyse_sessions_not_calendar_days():
    stale = dt.Trust("X", dt.STALE, last_session="2026-11-26")    # 感恩節
    assert dt.sessions_behind(stale, "2026-11-30") == 2           # 11-27（半天）、11-30
    assert dt.sessions_behind(dt.Trust("X", dt.OK, last_session=SESSION), SESSION) == 0


# ── 04:30 的結論 ─────────────────────────────────────────────────────

def test_conclusion_lists_only_untrusted_files_and_the_notice_needs_three_sessions(tmp_path):
    _write(tmp_path / "stocks", "AAA", [SESSION])
    _write(tmp_path / "stocks", "TWO", ["2026-10-20"])            # 落後 2 天
    _write(tmp_path / "stocks", "THREE", ["2026-10-19"])          # 落後 3 天
    _write(tmp_path / "etfs", "XLK", ["2026-10-16"])

    c = dt.conclude({"stocks": ["THREE", "AAA", "TWO", "GONE", "AAA"], "etfs": ["XLK"]},
                    SESSION, root=tmp_path)
    assert (c["schema"], c["session"], c["checked"]) == (dt.SCHEMA, SESSION, 5)
    assert sorted(c["problems"]) == ["GONE", "THREE", "TWO", "XLK"]
    assert c["problems"]["XLK"] == {"symbol": "XLK", "status": dt.STALE,
                                    "reason": "最後一筆 2026-10-16",
                                    "last_session": "2026-10-16", "folder": "etfs",
                                    "sessions_behind": 4}
    assert json.loads(json.dumps(c)) == c                         # 可以直接寫成 JSON
    assert dt.notice_lines(c) == [                                # 落後 2 天的不告警
        "GONE: 缺檔（沒有檔案）",
        "THREE: 連續 3 個交易日沒有新 bar（最後 2026-10-19）",
        "XLK: 連續 4 個交易日沒有新 bar（最後 2026-10-16）"]


def test_summary_lines_cap_the_list():
    problems = [dt.Trust(f"S{i:02d}", dt.MISSING) for i in range(12)]
    lines = dt.summary_lines(problems)
    assert lines[0] == "  • S00 缺檔" and len(lines) == 11
    assert lines[-1] == "  • …另 2 檔"
    assert dt.summary_lines(problems[:2]) == ["  • S00 缺檔", "  • S01 缺檔"]


def test_cli_reports_and_exits_nonzero_when_something_is_untrusted(tmp_path, capsys):
    _write(tmp_path / "stocks", "AAA", [SESSION])
    _write(tmp_path / "etfs", "SPY", [SESSION])
    assert dt.main(["--session", SESSION, "--data-root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["checked"] == 2
    assert dt.main(["--session", SESSION, "--data-root", str(tmp_path), "--stocks", "GONE"]) == 1
    assert "GONE" in json.loads(capsys.readouterr().out)["problems"]


# ── SP500 選股：這次訊號用到哪些不可信的檔 ──────────────────────────

@pytest.fixture
def engine(tmp_path, monkeypatch):
    from strategies.sp500_momentum import config
    from strategies.sp500_momentum.selection import SelectionEngine

    monkeypatch.setattr(config, "STOCK_DIR", tmp_path)
    e = SelectionEngine.__new__(SelectionEngine)
    e.data_cache, e.metrics_cache, e.scan_cache, e._unusable = {}, {}, {}, {}
    e._watched = None
    return e


def test_selection_reports_what_it_read_without_rereading(engine, tmp_path, monkeypatch):
    from strategies.sp500_momentum import utils

    _write(tmp_path, "AAA", [SESSION])
    _write(tmp_path, "PSKY", ["2026-10-16"])
    (tmp_path / "BAD.csv").write_text("garbage", encoding="utf-8")
    session = pd.Timestamp(SESSION)

    engine.watch()
    for ticker in ("AAA", "PSKY", "BAD", "GONE"):
        engine._note((ticker,))
        engine._get_ticker_data(ticker, session)
    reads = []
    monkeypatch.setattr(utils, "load_stock", lambda p: reads.append(p))
    problems = engine.data_problems(session)

    assert reads == []                                            # 只看已讀進來的
    assert [(t.symbol, t.status) for t in problems] == [
        ("BAD", dt.BROKEN), ("GONE", dt.MISSING), ("PSKY", dt.STALE)]
    assert problems[2].last_session == "2026-10-16"


def test_scan_cache_hit_still_watches_the_constituents(engine, tmp_path):
    """第二個帳戶共用 selector、scan_market 走快取：一樣要列出同樣的檔。"""
    _write(tmp_path, "PSKY", ["2026-10-16"])
    session = pd.Timestamp(SESSION)
    engine.get_constituents = lambda date: ["PSKY"]
    engine._get_ticker_data("PSKY", session)
    engine.scan_cache[(session, 63)] = []

    engine.watch()
    engine.scan_market(session, 63)
    assert [t.symbol for t in engine.data_problems(session)] == ["PSKY"]
    engine.watch()                                                # 下一個帳戶重新記
    assert engine.data_problems(session) == []


def test_live_source_passes_the_problems_with_the_signal(monkeypatch, caplog):
    import signals
    import sp500_state
    from strategies.sp500_momentum import selection
    from strategies.sp500_momentum import signal as sp_signal

    stale = dt.Trust("PSKY", dt.STALE, "最後一筆 2026-10-16", "2026-10-16")

    class Engine:
        constituents_df = pd.DataFrame()

        def watch(self):
            self.watched = True

        def data_problems(self, session):
            assert self.watched and session == pd.Timestamp(SESSION)
            return [stale]

    monkeypatch.setattr(selection, "SelectionEngine", Engine)
    monkeypatch.setattr(sp500_state, "load_state", lambda name: {})
    monkeypatch.setattr(sp_signal, "get_latest_weights_live",
                        lambda h, s, as_of_date=None, selector=None:
                        ({"AAPL": 1.0}, pd.Timestamp(SESSION), {}))
    signal = signals.LiveSource().sp500([], "Rosa")
    assert signal.data_problems == (stale,)
    assert "SP500 data not trusted for 2026-10-22 (1): PSKY 沒更新（最後 2026-10-16）" in caplog.text


# ── 21:00 調倉：排名照跑，Telegram 列出 ──────────────────────────────

def test_data_alarm_sends_a_warning_even_without_orders():
    from rebalance_ib import rebalance_tg_section
    headline, body, level = rebalance_tg_section(
        [], [], order_type="MOO", dry_run=False,
        data_alarms=["📉 SP500 訊號日 2026-10-22 有 1 檔資料不可信", "  • A<B> 缺檔"])
    assert headline == "沒有送單，⚠️ SP500 有資料不可信"
    assert body == ["📉 SP500 訊號日 2026-10-22 有 1 檔資料不可信", "  • A&lt;B&gt; 缺檔"]
    assert level == "warn"


def test_state_alarm_headline_wins_over_the_data_alarm():
    from rebalance_ib import rebalance_tg_section
    headline, body, _ = rebalance_tg_section(
        [], [], order_type="MOO", dry_run=False,
        state_alarms=["⚠️ SP500 state 讀不出來"], data_alarms=["📉 資料"])
    assert headline == "沒有送單，⚠️ 有策略停下（state 讀不出來）"
    assert body == ["⚠️ SP500 state 讀不出來", "📉 資料"]


@pytest.fixture
def rebalance_env(monkeypatch, tmp_path):
    """真的 21:00 流程：in-memory Broker（每檔 $100、NAV $100k）、固定訊號、state 在 tmp。"""
    import account_state
    import manual_attribution as ma
    import rebalance_ib
    import strategy_orders
    from tests.fake_broker import FakeBroker, install_connect

    monkeypatch.setattr(account_state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(ma, "DEFAULT_LEDGER_PATH", tmp_path / "ledger.sqlite3")
    monkeypatch.setattr(ma, "DEFAULT_MANUAL_ORDERS_PATH", tmp_path / "orders.sqlite3")
    monkeypatch.setattr(rebalance_ib, "append_trade_log", lambda *a, **k: None)
    monkeypatch.setattr(strategy_orders.time, "sleep", lambda _s: None)
    env = NS(brokers=[])

    def connect(ctx, **_kw):
        fake = FakeBroker(managed=["U00000001"], positions={"AAPL": 100.0})
        env.brokers.append(fake)
        return fake.connect(ctx)

    install_connect(monkeypatch, connect)
    return env


def _rebalance(problems):
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
                        universe={"AAPL", "MSFT"}, sp500_problems=problems)
    digest = Digest("調倉 T", icon="📤")
    rebalance_ib.main(mode="sp500", ctx=ctx, digest=digest, source=source)
    return digest.render()


def _sent(env):
    return sorted((p.symbol, p.action, p.qty) for b in env.brokers for p in b.placed)


def test_untrusted_data_is_listed_and_orders_are_unchanged(rebalance_env):
    clean = _rebalance(())
    orders = _sent(rebalance_env)
    rebalance_env.brokers.clear()

    problems = (dt.Trust("BAD", dt.BROKEN, "unreadable"),
                dt.Trust("GONE", dt.MISSING, "沒有檔案"),
                dt.Trust("PSKY", dt.STALE, "最後一筆 2026-10-16", "2026-10-16"))
    text = _rebalance(problems)

    assert _sent(rebalance_env) == orders == [("AAPL", "BUY", 400), ("MSFT", "BUY", 500)]
    assert "資料不可信" not in clean
    assert "⚠️ SP500 有資料不可信" in text
    assert "📉 SP500 訊號日 2026-10-22 有 3 檔資料不可信（排名照跑，缺檔／壞檔的不在排名裡）：" in text
    assert ("  • BAD 壞檔（unreadable）\n  • GONE 缺檔\n  • PSKY 沒更新（最後 2026-10-16）"
            in text)


def test_plan_inputs_without_problems_has_no_notice():
    import plan_inputs
    inputs = plan_inputs.PlanInputs.__new__(plan_inputs.PlanInputs)
    inputs.sp500_data_problems, inputs.sp500_date = [], SESSION
    assert inputs.data_notices() == []


# ── 04:30 資料更新 ───────────────────────────────────────────────────

@pytest.fixture
def update_env(tmp_path, monkeypatch):
    import signals
    import update_data
    from tests.fake_signals import FakeSource

    root = tmp_path / "data"
    monkeypatch.setattr(dt.market_data, "DATA_ROOT", root)
    monkeypatch.setattr(dt, "CONCLUSION_DIR", tmp_path / "trust")
    monkeypatch.setattr(signals, "LiveSource",
                        lambda: FakeSource(universe={"AAA", "PSKY", "TWO", "NEW", "SPY", "SSO"}))
    _write(root / "stocks", "AAA", [SESSION])
    _write(root / "stocks", "PSKY", ["2026-10-16"])
    _write(root / "stocks", "TWO", ["2026-10-20"])
    from data.update_data import ALL_ETFS
    for sym in [*ALL_ETFS, "VIX"]:
        _write(root / "etfs", sym, [SESSION])
    return NS(root=root, trust=tmp_path / "trust", module=update_data)


# 2026-10-23 04:30 台北 = 10-22 16:30 美東，10-22 已收盤
AFTER_CLOSE = datetime.fromisoformat("2026-10-22T16:30:00-04:00")


def test_update_writes_the_conclusion_and_lists_three_sessions_behind(update_env):
    lines = update_env.module.conclude_data_trust(AFTER_CLOSE)

    assert lines == ["NEW: 缺檔（沒有檔案）",                      # 新成分股：更新不會自己補檔
                     "PSKY: 連續 4 個交易日沒有新 bar（最後 2026-10-16）"]
    [path] = update_env.trust.iterdir()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["session"] == SESSION
    assert sorted(saved["problems"]) == ["NEW", "PSKY", "TWO"]    # 2 天的照記、不告警
    assert saved["checked"] == 4 + 25                             # 成分股（不含 SPY/SSO）＋ETF＋VIX


def test_before_the_close_the_previous_session_is_checked(update_env):
    """冬令 04:30 台北 = 15:30 美東，當天還沒收盤：看前一個 session。"""
    lines = update_env.module.conclude_data_trust(
        datetime.fromisoformat("2026-10-23T15:30:00-04:00"))
    assert lines[-1] == "PSKY: 連續 4 個交易日沒有新 bar（最後 2026-10-16）"


def test_update_lists_the_untrusted_files_in_the_failure_notice(update_env, monkeypatch):
    import sys
    import types
    fake = types.ModuleType("data.update_data")
    fake.update_etfs = lambda: None
    fake.update_stocks = lambda: None
    fake.ALL_ETFS = []
    monkeypatch.setitem(sys.modules, "data.update_data", fake)
    sent = []
    monkeypatch.setattr(update_env.module, "send_system", lambda msg: sent.append(msg) or True)
    monkeypatch.setattr(update_env.module, "conclude_data_trust",
                        lambda: ["PSKY: 連續 4 個交易日沒有新 bar（最後 2026-10-16）"])

    errors = update_env.module.main()
    assert errors == ["PSKY: 連續 4 個交易日沒有新 bar（最後 2026-10-16）"]
    assert len(sent) == 1 and "PSKY: 連續 4 個交易日沒有新 bar" in sent[0]


def test_a_failed_conclusion_is_reported_not_raised(update_env, monkeypatch):
    import sys
    import types
    fake = types.ModuleType("data.update_data")
    fake.update_etfs = lambda: None
    fake.update_stocks = lambda: None
    monkeypatch.setitem(sys.modules, "data.update_data", fake)
    sent = []
    monkeypatch.setattr(update_env.module, "send_system", lambda msg: sent.append(msg) or True)

    def boom():
        raise OSError("disk full")

    monkeypatch.setattr(update_env.module, "conclude_data_trust", boom)
    assert update_env.module.main() == ["Data trust: disk full"]
    assert len(sent) == 1 and "Data trust: disk full" in sent[0]
