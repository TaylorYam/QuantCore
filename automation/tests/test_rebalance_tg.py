"""
21:00 調倉 Telegram 段落（#26：rebalance_ib.rebalance_tg_section）.

原則：只列送出的單；沒單且無警報、無錯誤 → 不發。對帳 🔴 即使沒送單也要發
（凍結類 bug 的警鈴：決策層算出該動、執行層一張單都沒送）；🟠 只附帶。

No IB, no network.
"""

from __future__ import annotations

import pytest

pytest.importorskip("ib_insync")

PLACED = [
    {"symbol": "ERY", "action": "BUY", "qty": 55, "lmt_price": 12.3, "strategy": "SkewLS"},
    {"symbol": "SEF", "action": "SELL", "qty": 21, "lmt_price": 15.0, "strategy": "SkewLS"},
    {"symbol": "XLB", "action": "BUY", "qty": 12, "lmt_price": 88.1, "strategy": "SkewLS"},
]


def section(placed=(), fills=(), **kw):
    from rebalance_ib import rebalance_tg_section
    kw.setdefault("order_type", "MOO")
    kw.setdefault("dry_run", False)
    return rebalance_tg_section(list(placed), list(fills), **kw)


def test_no_orders_no_alarm_is_silent():
    assert section(drift_advisories=["🟠 SkewLS 合計偏離 6.7% NAV"]) is None


def test_moo_orders_grouped_by_side():
    headline, body, level = section(PLACED)
    assert headline == "送出 3 筆 MOO"
    assert body == ["🟢 BUY 55 ERY · 12 XLB", "🔴 SELL 21 SEF"]
    assert level == "ok"


def test_moo_fill_results_are_ignored_at_2100():
    """MOO 送出時都是 PreSubmitted，成交由 21:31 回報 — 21:00 不列未成交。"""
    fills = [{"symbol": "ERY", "action": "BUY", "qty": 55, "filled": 0,
              "avg_price": 0.0, "status": "PreSubmitted"}]
    _, body, level = section(PLACED, fills)
    assert not any("成交" in ln for ln in body)
    assert level == "ok"


def test_red_alarm_sends_even_without_orders():
    alarms = ["🔴 需要注意 1 檔:", "  REW [SkewLS]: 應 263 實 85 — 未送出任何對應單"]
    headline, body, level = section(drift_alarms=alarms)
    assert headline == "沒有送單"
    assert body == alarms
    assert level == "warn"


def test_advisory_attached_when_section_is_sent():
    _, body, _ = section(PLACED, drift_advisories=["🟠 SkewLS 合計偏離 6.7% NAV（4 檔累積）"])
    assert body[-1] == "🟠 SkewLS 合計偏離 6.7% NAV（4 檔累積）"


def test_error_without_orders():
    headline, body, level = section(error_msg="connect <refused>")
    assert headline == "❌ 失敗"
    assert body == ["connect &lt;refused&gt;"]
    assert level == "error"


def test_error_after_orders_placed():
    headline, body, level = section(PLACED, error_msg="boom")
    assert headline == "送出 3 筆 MOO，❌ 之後失敗"
    assert body == ["🟢 BUY 55 ERY · 12 XLB", "🔴 SELL 21 SEF", "boom"]
    assert level == "error"


def test_dry_run_is_tagged():
    headline, _, _ = section(PLACED, dry_run=True)
    assert headline == "送出 3 筆 MOO [DRY RUN]"


def test_non_moo_lists_unfilled_orders():
    fills = [
        {"symbol": "ERY", "action": "BUY", "qty": 55, "filled": 55, "avg_price": 12.3, "status": "Filled"},
        {"symbol": "XLB", "action": "BUY", "qty": 12, "filled": 5, "avg_price": 88.1, "status": "Submitted"},
    ]
    headline, body, level = section(PLACED, fills, order_type="LMT")
    assert headline == "送出 3 筆 LMT"
    assert "⚠️ XLB 成交 5/12 [Submitted]" in body
    assert not any("ERY 成交" in ln for ln in body)
    assert level == "warn"


def test_drift_lines_are_html_escaped():
    _, body, _ = section(drift_alarms=["🔴 drift=1.2% < 3%"])
    assert body == ["🔴 drift=1.2% &lt; 3%"]


def test_add_section_never_raises(monkeypatch):
    """在 finally 內呼叫：組段落出錯只能 log，不能蓋掉原本的例外。"""
    import rebalance_ib
    from types import SimpleNamespace as NS
    from notify import Digest

    def broken(*a, **k):
        raise RuntimeError("format bug")

    monkeypatch.setattr(rebalance_ib, "rebalance_tg_section", broken)
    ctx = NS(name="Taylor", order_type="MOO", dry_run=False, tg_token="T", tg_chat_id="C")
    d = Digest("調倉")
    rebalance_ib._add_rebalance_section(d, ctx, placed=PLACED, fill_results=[])
    assert d.render() is None


def _capture_sends(monkeypatch):
    import notify
    sent = []
    monkeypatch.setattr(notify, "tg_send",
                        lambda msg, *, token, chat_id: sent.append(msg))
    return sent


def test_run_accounts_sends_one_message_for_all_accounts(fake_config, monkeypatch):
    import rebalance_ib
    sent = _capture_sends(monkeypatch)
    seen = []

    def fake_main(mode, ctx, digest):
        seen.append((mode, ctx.name, ctx.order_type))
        rebalance_ib._add_rebalance_section(digest, ctx, placed=PLACED[:1], fill_results=[])

    monkeypatch.setattr(rebalance_ib, "main", fake_main)
    rebalance_ib.run_accounts(["Taylor", "Rosa", "Yvonne"], mode="skewls", order_type="MKT")
    assert seen == [("skewls", "Taylor", "MKT"), ("skewls", "Rosa", "MKT"), ("skewls", "Yvonne", "MKT")]
    assert len(sent) == 1
    assert sent[0].count("送出 1 筆 MKT") == 1        # 三帳戶內容相同 → 合成一段
    assert "<b>Taylor、Rosa、Yvonne</b> 送出 1 筆 MKT" in sent[0]


def test_run_accounts_quiet_day_sends_nothing(fake_config, monkeypatch):
    import rebalance_ib
    sent = _capture_sends(monkeypatch)
    monkeypatch.setattr(rebalance_ib, "main",
                        lambda mode, ctx, digest: rebalance_ib._add_rebalance_section(
                            digest, ctx, placed=[], fill_results=[]))
    rebalance_ib.run_accounts(["Taylor", "Rosa", "Yvonne"], mode="skewls")
    assert sent == []


def test_run_accounts_failure_marks_skipped_accounts(fake_config, monkeypatch):
    """08-26 情境：Taylor 連線失敗 → 例外照拋、Rosa / Yvonne 沒跑，訊息要標出來。"""
    import rebalance_ib
    sent = _capture_sends(monkeypatch)

    def fake_main(mode, ctx, digest):
        rebalance_ib._add_rebalance_section(digest, ctx, placed=[], fill_results=[],
                                            error_msg="[WinError 1225] refused")
        raise ConnectionRefusedError("[WinError 1225] refused")

    monkeypatch.setattr(rebalance_ib, "main", fake_main)
    with pytest.raises(ConnectionRefusedError):
        rebalance_ib.run_accounts(["Taylor", "Rosa", "Yvonne"], mode="all")
    assert len(sent) == 1
    msg = sent[0]
    assert msg.startswith("<b>❌ 調倉 ")
    # fake_config 的 dry_run=True
    assert "<b>Taylor</b> ❌ 失敗 [DRY RUN]\n[WinError 1225] refused" in msg
    assert "<b>Rosa、Yvonne</b> ⚠️ 未執行\n前一帳戶失敗，本輪中止" in msg


def test_run_accounts_failure_before_main_adds_error(fake_config, monkeypatch):
    """ctx 建立就失敗（main 沒機會加段落）→ 仍要有失敗段落。"""
    import rebalance_ib
    sent = _capture_sends(monkeypatch)
    calls = []
    monkeypatch.setattr(rebalance_ib, "main", lambda mode, ctx, digest: calls.append(ctx.name))
    with pytest.raises(ValueError):
        rebalance_ib.run_accounts(["Taylor", "Nobody", "Yvonne"], mode="all")
    assert calls == ["Taylor"]
    assert "<b>Nobody</b> ❌ 失敗" in sent[0]
    assert "<b>Yvonne</b> ⚠️ 未執行" in sent[0]


def test_digest_renders_three_accounts_in_one_message():
    import rebalance_ib
    from types import SimpleNamespace as NS
    from notify import Digest

    d = Digest("調倉 2026-09-10", icon="📤")
    for name, placed in (("Taylor", PLACED), ("Rosa", PLACED[:1]), ("Yvonne", [])):
        ctx = NS(name=name, order_type="MOO", dry_run=False, tg_token="T", tg_chat_id="C")
        rebalance_ib._add_rebalance_section(d, ctx, placed=placed, fill_results=[])
    assert d.render() == (
        "<b>📤 調倉 2026-09-10</b>\n"
        "\n"
        "<b>Taylor</b> 送出 3 筆 MOO\n"
        "🟢 BUY 55 ERY · 12 XLB\n"
        "🔴 SELL 21 SEF\n"
        "\n"
        "<b>Rosa</b> 送出 1 筆 MOO\n"
        "🟢 BUY 55 ERY"
    )
