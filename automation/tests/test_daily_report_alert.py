"""
每日報告保證金警示（#26：daily_report 不再推播持倉報告，只在 cushion 過低時警示）.

No IB, no network.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

pytest.importorskip("ib_insync")


def test_healthy_cushion_is_silent():
    from daily_report import cushion_alert
    assert cushion_alert(nav=15_000.0, cushion_pct=40.6, excess_liq=6_000.0) is None
    assert cushion_alert(nav=15_000.0, cushion_pct=25.0, excess_liq=3_750.0) is None


def test_low_cushion_alerts():
    from daily_report import cushion_alert
    headline, body, level = cushion_alert(nav=15_212.73, cushion_pct=18.46, excess_liq=2_808.0)
    assert headline == "Margin Cushion <b>18.5%</b>（低於 25%）"
    assert body == ["ExcessLiq $2,808 · NAV $15,212.73"]
    assert level == "alert"


def test_missing_account_data_is_not_a_zero_cushion_alarm():
    """get_account_info 缺欄位時 nav / cushion 都是 0 — 不能當成 cushion 0% 警示。"""
    from daily_report import cushion_alert
    assert cushion_alert(nav=0.0, cushion_pct=0.0, excess_liq=0.0) is None


class FakeIB:
    def connect(self, *a, **k):
        pass

    def disconnect(self):
        self.disconnected = True


def _run(monkeypatch, cushion, snapshot=lambda *a, **k: None):
    import daily_report
    from notify import Digest
    monkeypatch.setattr(daily_report, "IB", FakeIB)
    monkeypatch.setattr(daily_report, "get_account_info", lambda ib, ctx: {
        "nav": 15_000.0, "cushion_pct": cushion, "excess_liq": 3_000.0, "maint_margin": 0.0})
    monkeypatch.setattr(daily_report, "get_positions_detail", lambda ib, ctx: [])
    monkeypatch.setattr(daily_report, "append_daily_snapshot", snapshot)
    ctx = NS(name="Taylor", account="U1", host="127.0.0.1", port=4001, client_id=1,
             tg_token="T", tg_chat_id="C", alloc_sr=0.0, alloc_skewls=1.0)
    d = Digest("每日報告 2026-09-15", icon="📊")
    daily_report.main(ctx, digest=d)
    return d


def test_main_writes_snapshot_and_stays_silent_when_healthy(monkeypatch):
    written = []
    d = _run(monkeypatch, 64.6,
             snapshot=lambda *a, **k: written.append((k["account_name"], k["strategy_alloc"])))
    assert written == [("Taylor", (0.0, 1.0))], "Excel 快照照寫，並帶 Allocation 判斷歸屬（#181）"
    assert d.render() is None, "持倉報告不再推播"


def test_main_queues_alert_when_cushion_low(monkeypatch):
    d = _run(monkeypatch, 12.0)
    msg = d.render()
    assert msg.startswith("<b>🔴 每日報告 2026-09-15</b>")
    assert "<b>Taylor</b> Margin Cushion <b>12.0%</b>（低於 25%）" in msg


def test_main_failure_is_reported_and_reraised(monkeypatch):
    def broken(*a, **k):
        raise OSError("xlsx <locked>")

    with pytest.raises(OSError):
        _run(monkeypatch, 64.6, snapshot=broken)


def test_main_failure_section_content(monkeypatch):
    import daily_report
    from notify import Digest

    monkeypatch.setattr(daily_report, "IB", FakeIB)

    def boom(ib, ctx):
        raise RuntimeError("no <account> data")

    monkeypatch.setattr(daily_report, "get_account_info", boom)
    ctx = NS(name="Rosa", account="U2", host="127.0.0.1", port=4002, client_id=1,
             tg_token="T", tg_chat_id="C")
    d = Digest("每日報告", icon="📊")
    with pytest.raises(RuntimeError):
        daily_report.main(ctx, digest=d)
    msg = d.render()
    assert msg.startswith("<b>❌ 每日報告</b>")
    assert "<b>Rosa</b> ❌ 每日報告失敗\nno &lt;account&gt; data" in msg


# ── run_all()：連不上 IB 以前只寫 log（#38）────────────────────────────

class RefusedIB:
    def connect(self, *a, **k):
        raise ConnectionRefusedError("[WinError 1225] refused")

    def disconnect(self):
        pass


def _capture(monkeypatch):
    import notify
    sent = []
    monkeypatch.setattr(notify, "tg_send",
                        lambda msg, *, token, chat_id: sent.append((msg, token, chat_id)))
    return sent


def test_run_all_gateway_down_is_reported(fake_config, monkeypatch):
    """08-03、08-10 04:40 三帳戶都連不上 IB：以前只寫 log，沒有任何通知。"""
    import daily_report
    from notify import Digest
    sent = _capture(monkeypatch)
    monkeypatch.setattr(daily_report, "IB", RefusedIB)
    d = Digest("每日報告 2026-08-10", icon="📊")
    failures = daily_report.run_all(["Taylor", "Rosa", "Yvonne"], d)
    assert [n for n, _ in failures] == ["Taylor", "Rosa", "Yvonne"]
    assert len(sent) == 1
    msg, token, chat = sent[0]
    assert msg.startswith("<b>❌ 每日報告 2026-08-10</b>")
    assert "<b>Taylor、Rosa、Yvonne</b> ❌ 每日報告失敗\n[WinError 1225] refused" in msg
    assert (token, chat) == ("FAKE_TOKEN", "FAKE_CHAT")
    assert d.outcomes(["Taylor", "Rosa", "Yvonne"]) == {
        n: ("error", "❌ 每日報告失敗") for n in ("Taylor", "Rosa", "Yvonne")}


def test_run_all_does_not_duplicate_main_failure_section(fake_config, monkeypatch):
    import daily_report
    from notify import Digest
    sent = _capture(monkeypatch)
    monkeypatch.setattr(daily_report, "IB", FakeIB)

    def boom(ib, ctx):
        raise RuntimeError("no <account> data")

    monkeypatch.setattr(daily_report, "get_account_info", boom)
    daily_report.run_all(["Taylor"], Digest("每日報告", icon="📊"))
    assert sent[0][0].count("❌ 每日報告失敗") == 1


def test_run_all_healthy_accounts_stay_silent(fake_config, monkeypatch):
    import daily_report
    from notify import Digest
    sent = _capture(monkeypatch)
    monkeypatch.setattr(daily_report, "IB", FakeIB)
    monkeypatch.setattr(daily_report, "get_account_info", lambda ib, ctx: {
        "nav": 15_000.0, "cushion_pct": 60.0, "excess_liq": 9_000.0, "maint_margin": 0.0})
    monkeypatch.setattr(daily_report, "get_positions_detail", lambda ib, ctx: [])
    monkeypatch.setattr(daily_report, "append_daily_snapshot", lambda *a, **k: None)
    assert daily_report.run_all(["Taylor", "Rosa"], Digest("每日報告", icon="📊")) == []
    assert sent == []
