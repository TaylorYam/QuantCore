"""
每日報告保證金警示（#26：daily_report 不再推播持倉報告，只在 cushion 過低時警示）.

No IB, no network: IB 這一側是 tests/fake_broker.py 的 in-memory Broker。
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

pytest.importorskip("ib_insync")

from tests.fake_broker import FakeBroker, install


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
    """nav / cushion 都是 0 = 沒資料，不能當成 cushion 0% 警示（Broker 讀不到時本來就
    直接報錯，這是最後一道保險）。"""
    from daily_report import cushion_alert
    assert cushion_alert(nav=0.0, cushion_pct=0.0, excess_liq=0.0) is None


def _broker(monkeypatch, cushion=60.0):
    fake = install(monkeypatch, FakeBroker(managed=["U1"], nav=15_000.0))
    fake.cushion_pct, fake.excess_liq = cushion, 3_000.0
    return fake


def _run(monkeypatch, cushion, snapshot=lambda *a, **k: None):
    import daily_report
    from notify import Digest
    fake = _broker(monkeypatch, cushion)
    monkeypatch.setattr(daily_report, "append_daily_snapshot", snapshot)
    ctx = NS(name="Taylor", account="U1", host="127.0.0.1", port=4001, client_id=1,
             alloc_sr=0.0, alloc_skewls=1.0)
    d = Digest("每日報告 2026-09-15", icon="📊")
    daily_report.main(ctx, digest=d)
    assert fake.connects == [("127.0.0.1", 4001, 1)] and fake.closed == 1
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

    _broker(monkeypatch).fail["account_info"] = RuntimeError("no <account> data")
    ctx = NS(name="Rosa", account="U2", host="127.0.0.1", port=4002, client_id=1)
    d = Digest("每日報告", icon="📊")
    with pytest.raises(RuntimeError):
        daily_report.main(ctx, digest=d)
    msg = d.render()
    assert msg.startswith("<b>❌ 每日報告</b>")
    assert "<b>Rosa</b> ❌ 每日報告失敗\nno &lt;account&gt; data" in msg
