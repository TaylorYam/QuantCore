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


def test_run_accounts_uses_callers_digest(fake_config, monkeypatch):
    """#38：__main__ 傳入 digest，跑完照樣發送，之後讀各帳戶結果寫執行紀錄。"""
    import rebalance_ib
    from notify import Digest
    sent = _capture_sends(monkeypatch)

    def fake_main(mode, ctx, digest):
        placed = PLACED[:1] if ctx.name == "Rosa" else []
        rebalance_ib._add_rebalance_section(digest, ctx, placed=placed, fill_results=[])

    monkeypatch.setattr(rebalance_ib, "main", fake_main)
    d = Digest("調倉 2026-09-16", icon="📤")
    assert rebalance_ib.run_accounts(["Taylor", "Rosa", "Yvonne"], mode="all", digest=d) == []
    assert len(sent) == 1 and sent[0].startswith("<b>📤 調倉 2026-09-16</b>")
    assert d.outcomes(["Taylor", "Rosa", "Yvonne"]) == {
        "Taylor": ("ok", None),
        "Rosa": ("ok", "送出 1 筆 MOO [DRY RUN]"),
        "Yvonne": ("ok", None),
    }


def test_run_accounts_quiet_day_sends_nothing(fake_config, monkeypatch):
    import rebalance_ib
    sent = _capture_sends(monkeypatch)
    monkeypatch.setattr(rebalance_ib, "main",
                        lambda mode, ctx, digest: rebalance_ib._add_rebalance_section(
                            digest, ctx, placed=[], fill_results=[]))
    rebalance_ib.run_accounts(["Taylor", "Rosa", "Yvonne"], mode="skewls")
    assert sent == []


def test_run_accounts_failure_continues_to_next_account(fake_config, monkeypatch):
    """#29：Taylor 的 Gateway 掛掉，Rosa / Yvonne 照常調倉（以前整輪中止）。"""
    import rebalance_ib
    sent = _capture_sends(monkeypatch)
    ran = []

    def fake_main(mode, ctx, digest):
        ran.append(ctx.name)
        if ctx.name == "Taylor":
            rebalance_ib._add_rebalance_section(digest, ctx, placed=[], fill_results=[],
                                                error_msg="[WinError 1225] refused")
            raise ConnectionRefusedError("[WinError 1225] refused")
        rebalance_ib._add_rebalance_section(digest, ctx, placed=PLACED[:1], fill_results=[])

    monkeypatch.setattr(rebalance_ib, "main", fake_main)
    failures = rebalance_ib.run_accounts(["Taylor", "Rosa", "Yvonne"], mode="all")
    assert ran == ["Taylor", "Rosa", "Yvonne"]
    assert failures == [("Taylor", "[WinError 1225] refused")]
    assert len(sent) == 1
    msg = sent[0]
    assert msg.startswith("<b>❌ 調倉 ")
    # fake_config 的 dry_run=True；main 已加的段落不重複
    assert msg.count("Taylor") == 1
    assert "<b>Taylor</b> ❌ 失敗 [DRY RUN]\n[WinError 1225] refused" in msg
    assert "<b>Rosa、Yvonne</b> 送出 1 筆 MOO [DRY RUN]" in msg
    assert "未執行" not in msg


def test_run_accounts_all_fail_returns_every_failure(fake_config, monkeypatch):
    """08-24 / 08-26 情境：三個 Gateway 全掛 → 三帳戶都試過、都回報。"""
    import rebalance_ib
    sent = _capture_sends(monkeypatch)

    def fake_main(mode, ctx, digest):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(rebalance_ib, "main", fake_main)
    failures = rebalance_ib.run_accounts(["Taylor", "Rosa", "Yvonne"], mode="all")
    assert [n for n, _ in failures] == ["Taylor", "Rosa", "Yvonne"]
    assert "<b>Taylor、Rosa、Yvonne</b> ❌ 失敗\nrefused" in sent[0]


def test_run_accounts_failure_before_main_adds_error(fake_config, monkeypatch):
    """ctx 建立就失敗（main 沒機會加段落）→ 補失敗段落，後面帳戶照跑。"""
    import rebalance_ib
    sent = _capture_sends(monkeypatch)
    calls = []
    monkeypatch.setattr(rebalance_ib, "main", lambda mode, ctx, digest: calls.append(ctx.name))
    failures = rebalance_ib.run_accounts(["Taylor", "Nobody", "Yvonne"], mode="all")
    assert calls == ["Taylor", "Yvonne"]
    assert [n for n, _ in failures] == ["Nobody"]
    assert "<b>Nobody</b> ❌ 失敗" in sent[0]


def test_run_accounts_keyboard_interrupt_aborts(fake_config, monkeypatch):
    """Ctrl+C 仍立即中止：剩下的帳戶標未執行，例外照拋。"""
    import rebalance_ib
    sent = _capture_sends(monkeypatch)
    ran = []

    def fake_main(mode, ctx, digest):
        ran.append(ctx.name)
        raise KeyboardInterrupt

    monkeypatch.setattr(rebalance_ib, "main", fake_main)
    with pytest.raises(KeyboardInterrupt):
        rebalance_ib.run_accounts(["Taylor", "Rosa", "Yvonne"], mode="all")
    assert ran == ["Taylor"]
    assert "<b>Rosa、Yvonne</b> ⚠️ 未執行\n手動中止，本輪未執行" in sent[0]


# ── main() 收尾：Excel 交易紀錄 + Telegram 段落（#29）──────────────────

def _ctx(name="Taylor"):
    from types import SimpleNamespace as NS
    return NS(name=name, order_type="MOO", dry_run=False, leverage=1.3,
              tg_token="T", tg_chat_id="C")


def test_log_trades_and_report_writes_each_order(monkeypatch):
    import rebalance_ib
    from notify import Digest
    calls = []
    monkeypatch.setattr(rebalance_ib, "append_trade_log",
                        lambda placed, fills, sig, ot, lev, dry, strategy, account_name:
                        calls.append((placed[0]["symbol"], sig, ot, lev, dry, strategy, account_name)))
    d = Digest("調倉")
    err = rebalance_ib._log_trades_and_report(
        d, _ctx(), placed=PLACED, fill_results=[], signal_str="SR:N/A SP500:N/A",
        drift_alarms=[], drift_advisories=[], error_msg=None)
    assert err is None
    assert calls == [(o["symbol"], "SR:N/A SP500:N/A", "MOO", 1.3, False, "SkewLS", "Taylor")
                     for o in PLACED]
    assert "<b>Taylor</b> 送出 3 筆 MOO" in d.render()


def test_log_trades_and_report_excel_failure_keeps_orders_in_message(monkeypatch):
    """trade_log.xlsx 被鎖住：單已送出，訊息仍要列出送出的單並標 ❌。"""
    import rebalance_ib
    from notify import Digest

    def locked(*a, **k):
        raise PermissionError("trade_log.xlsx is locked")

    monkeypatch.setattr(rebalance_ib, "append_trade_log", locked)
    d = Digest("調倉", icon="📤")
    err = rebalance_ib._log_trades_and_report(
        d, _ctx(), placed=PLACED, fill_results=[], signal_str="",
        drift_alarms=[], drift_advisories=[], error_msg=None)
    assert isinstance(err, PermissionError)
    msg = d.render()
    assert msg.startswith("<b>❌ 調倉</b>")
    assert "<b>Taylor</b> 送出 3 筆 MOO，❌ 之後失敗" in msg
    assert "🟢 BUY 55 ERY · 12 XLB" in msg
    assert "交易紀錄寫入失敗: trade_log.xlsx is locked" in msg


def test_log_trades_and_report_no_orders_skips_excel(monkeypatch):
    import rebalance_ib
    from notify import Digest
    monkeypatch.setattr(rebalance_ib, "append_trade_log",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不該寫 Excel")))
    d = Digest("調倉")
    assert rebalance_ib._log_trades_and_report(
        d, _ctx(), placed=[], fill_results=[], signal_str="",
        drift_alarms=[], drift_advisories=[], error_msg=None) is None
    assert d.render() is None


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
