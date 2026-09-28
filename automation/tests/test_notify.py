"""
Unit tests for notify.tg_send / tg_send_photo / Digest.

Coverage:
  - Empty token / chat_id → silent no-op (no network call)
  - Network failure → swallowed (only logs warning, doesn't raise)
  - Successful send → calls urlopen with correct URL + payload
  - Keyword-only arguments enforcement
  - Digest（#26）：沒段落不發、標題取最嚴重等級、相同段落合併、依 chat 分組
  - split_message / order_group_lines
"""

from __future__ import annotations

import urllib.error
from unittest.mock import MagicMock, patch

import pytest


def test_tg_send_empty_token_is_noop():
    """Empty token should return immediately without any network call."""
    import notify
    with patch("urllib.request.urlopen") as mock_urlopen:
        assert not notify.tg_send("hello", token="", chat_id="123")
        assert not mock_urlopen.called, "urlopen should not be called with empty token"


def test_tg_send_empty_chat_id_is_noop():
    import notify
    with patch("urllib.request.urlopen") as mock_urlopen:
        assert not notify.tg_send("hello", token="abc", chat_id="")
        assert not mock_urlopen.called


def test_tg_send_network_error_is_swallowed():
    """Network errors must not raise — they should only log warning.
    Telegram failures should never break the trading flow."""
    import notify
    with patch("urllib.request.urlopen",
               side_effect=urllib.error.URLError("network down")):
        # Should NOT raise
        assert not notify.tg_send("hello", token="abc", chat_id="123")


def test_tg_send_timeout_is_swallowed():
    import notify
    with patch("urllib.request.urlopen",
               side_effect=TimeoutError("timed out")):
        assert not notify.tg_send("hello", token="abc", chat_id="123")


def test_tg_send_calls_telegram_api():
    """Verify the correct URL and HTTP method are used."""
    import notify
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        assert notify.tg_send("hello", token="TOKEN", chat_id="CHAT")

        assert mock_urlopen.called
        req = mock_urlopen.call_args[0][0]
        # Verify URL contains the bot token
        assert "TOKEN" in req.full_url
        assert "sendMessage" in req.full_url
        assert req.get_method() == "POST"
        # Verify payload contains chat_id + text
        body = req.data.decode("utf-8")
        assert "chat_id=CHAT" in body
        assert "text=hello" in body


def test_tg_send_token_and_chat_id_are_keyword_only():
    """token / chat_id MUST be keyword-only args to prevent positional bugs."""
    import notify
    with pytest.raises(TypeError):
        # Passing positionally should fail
        notify.tg_send("msg", "TOKEN", "CHAT")  # type: ignore[misc]


def test_tg_send_photo_empty_token_is_noop():
    import notify
    with patch("urllib.request.urlopen") as mock_urlopen:
        notify.tg_send_photo("/nonexistent.png",
                             token="", chat_id="123", caption="test")
        assert not mock_urlopen.called


def test_tg_send_photo_file_not_found_is_swallowed():
    """FileNotFoundError inside open() should be caught."""
    import notify
    # No mock needed: real token but nonexistent file.
    # Should log warning, not raise.
    notify.tg_send_photo("/definitely/does/not/exist.png",
                         token="TOKEN", chat_id="CHAT")


# ── Digest（#26：同一排程多帳戶合併一則）─────────────────────────────

DEST = {"token": "TOKEN", "chat_id": "CHAT"}


def _sent(monkeypatch):
    import notify
    sent = []
    monkeypatch.setattr(notify, "tg_send",
                        lambda msg, *, token, chat_id: sent.append((msg, token, chat_id)))
    return sent


def test_digest_without_sections_sends_nothing(monkeypatch):
    import notify
    sent = _sent(monkeypatch)
    d = notify.Digest("調倉 2026-09-15", icon="📤")
    d.note("SPY 480.12")          # note 不構成內容
    assert d.render() is None
    d.send()
    assert sent == []


def test_digest_merges_accounts_into_one_message(monkeypatch):
    import notify
    sent = _sent(monkeypatch)
    d = notify.Digest("MOO 成交 2026-09-10", icon="✅")
    d.add("Taylor", "8/8", ["🟢 BUY 55 ERY @ 12.41"], **DEST)
    d.add("Rosa", "9/9", ["🟢 BUY 12 XLB @ 88.10"], **DEST)
    d.send()
    assert len(sent) == 1
    msg = sent[0][0]
    assert msg.splitlines()[0] == "<b>✅ MOO 成交 2026-09-10</b>"
    assert "<b>Taylor</b> 8/8\n🟢 BUY 55 ERY @ 12.41" in msg
    assert "<b>Rosa</b> 9/9\n🟢 BUY 12 XLB @ 88.10" in msg


def test_digest_title_icon_takes_worst_level():
    import notify
    d = notify.Digest("調倉 2026-09-15", icon="📤")
    d.add("Taylor", "送出 1 筆 MOO", ["🟢 BUY 1 XLK"], **DEST)
    assert d.render().startswith("<b>📤 ")
    d.add("Rosa", "沒有送單", ["🔴 需要注意 1 檔:"], level="warn", **DEST)
    assert d.render().startswith("<b>⚠️ ")
    d.add("Yvonne", "❌ 失敗", ["boom"], level="error", **DEST)
    assert d.render().startswith("<b>❌ ")


def test_digest_identical_sections_collapse_into_one():
    import notify
    d = notify.Digest("日度風控 2026-09-15", icon="📤")
    for name in ("Taylor", "Rosa", "Yvonne"):
        d.add(name, "⚠️ 日度風控中止", ["資料錯位"], level="warn", **DEST)
    msg = d.render()
    assert msg.count("資料錯位") == 1
    assert "<b>Taylor、Rosa、Yvonne</b> ⚠️ 日度風控中止" in msg


def test_digest_notes_deduped_and_rendered_under_title():
    import notify
    d = notify.Digest("熊市 dip-buy 2026-09-15", icon="📤")
    d.note("SPY 480.12 · DD 15.3%")
    d.note("SPY 480.12 · DD 15.3%")
    d.add("Taylor", "送出 1 筆 LMT", ["🟢 BUY 12 SPY @ 500.12"], **DEST)
    lines = d.render().splitlines()
    assert lines[1] == "SPY 480.12 · DD 15.3%"
    assert lines.count("SPY 480.12 · DD 15.3%") == 1


def test_digest_escapes_account_names():
    import notify
    d = notify.Digest("t")
    d.add("A<B", "x", **DEST)
    assert "<b>A&lt;B</b> x" in d.render()


def test_digest_groups_by_destination(monkeypatch):
    """帳戶覆寫 telegram 設定時，各自送到自己的 chat，不會送錯群。"""
    import notify
    sent = _sent(monkeypatch)
    d = notify.Digest("t", icon="📤")
    d.add("Taylor", "a", token="T1", chat_id="C1")
    d.add("Rosa", "b", token="T2", chat_id="C2")
    d.add("Yvonne", "c", token="T1", chat_id="C1")
    d.send()
    assert [(t, c) for _, t, c in sent] == [("T1", "C1"), ("T2", "C2")]
    assert "Taylor" in sent[0][0] and "Yvonne" in sent[0][0]
    assert "Rosa" not in sent[0][0]


def test_digest_rejects_unknown_level():
    import notify
    with pytest.raises(ValueError):
        notify.Digest("t").add("Taylor", "x", level="info", **DEST)
    with pytest.raises(ValueError):
        notify.Digest("t").footer("x", level="info", **DEST)


# ── footer / outcomes（#38：21:31 當日排程總結）──────────────────────

def test_footer_alone_is_the_whole_message(monkeypatch):
    """沒單的日子：訊息只有總結那一行，不加標題。"""
    import notify
    sent = _sent(monkeypatch)
    d = notify.Digest("MOO 成交 2026-09-15", icon="✅")
    d.note("不該出現")
    d.footer("✅ 9/15 排程都跑完：21:31 無 MOO 單", **DEST)
    d.send()
    assert sent == [("✅ 9/15 排程都跑完：21:31 無 MOO 單", "TOKEN", "CHAT")]


def test_footer_goes_last_and_raises_title_icon():
    import notify
    d = notify.Digest("MOO 成交 2026-09-15", icon="✅")
    d.add("Taylor", "1/1", ["🟢 BUY 55 ERY @ 12.41"], **DEST)
    d.footer("✅ 9/15 排程都跑完：…", **DEST)
    assert d.render() == ("<b>✅ MOO 成交 2026-09-15</b>\n\n<b>Taylor</b> 1/1\n"
                          "🟢 BUY 55 ERY @ 12.41\n\n✅ 9/15 排程都跑完：…")
    d2 = notify.Digest("MOO 成交 2026-09-15", icon="✅")
    d2.add("Taylor", "1/1", ["🟢 BUY 55 ERY @ 12.41"], **DEST)
    d2.footer("❌ 9/15 排程有狀況：16:00 日度風控 ❌ 沒有執行紀錄", "error", **DEST)
    assert d2.render().startswith("<b>❌ MOO 成交 2026-09-15</b>")


def test_footer_only_goes_to_its_destination(monkeypatch):
    """帳戶覆寫 telegram：該帳戶的段落照送自己的 chat，總結送共用 chat。"""
    import notify
    sent = _sent(monkeypatch)
    d = notify.Digest("MOO 成交", icon="✅")
    d.add("Rosa", "1/1", ["🟢 BUY 1 XLB @ 50.00"], token="T2", chat_id="C2")
    d.footer("✅ 總結", token="T1", chat_id="C1")
    d.send()
    assert sent == [
        ("<b>✅ MOO 成交</b>\n\n<b>Rosa</b> 1/1\n🟢 BUY 1 XLB @ 50.00", "T2", "C2"),
        ("✅ 總結", "T1", "C1"),
    ]


def test_outcomes_per_account():
    import notify
    d = notify.Digest("日度風控", icon="📤")
    d.add("Taylor", "熊市 dip-buy 送出 1 筆 LMT", ["🟢 BUY 12 SPY @ 500.12"], **DEST)
    d.add("Taylor", "❌ 日度風控失敗", ["boom"], level="error", **DEST)
    d.add("Rosa", "⚠️ 日度風控中止", ["資料錯位"], level="warn", **DEST)
    d.add("Rosa", "晚到的 ok 段落", **DEST)
    assert d.outcomes(["Taylor", "Rosa", "Yvonne"]) == {
        "Taylor": ("error", "❌ 日度風控失敗"),   # 取最嚴重的一段
        "Rosa": ("warn", "⚠️ 日度風控中止"),
        "Yvonne": ("ok", None),                  # 沒段落 = 跑完、沒事
    }
    assert notify.Digest("t").outcomes() == {}


def test_digest_send_splits_long_messages(monkeypatch):
    import notify
    sent = _sent(monkeypatch)
    d = notify.Digest("t")
    d.add("Taylor", "x", [f"line {i:04d} " + "x" * 90 for i in range(100)], **DEST)
    d.send()
    assert len(sent) >= 2
    assert all(len(m) <= notify.TG_MAX_CHARS for m, _, _ in sent)
    joined = "\n".join(m for m, _, _ in sent)
    assert "line 0000" in joined and "line 0099" in joined


def test_split_message_keeps_short_message_whole():
    import notify
    assert notify.split_message("a\nb") == ["a\nb"]


def test_split_message_breaks_on_lines_and_hard_splits_long_line():
    import notify
    chunks = notify.split_message("aaaa\nbbbb\n" + "c" * 12, limit=10)
    assert chunks == ["aaaa\nbbbb", "cccccccccc", "cc"]
    assert all(len(c) <= 10 for c in chunks)


def test_order_group_lines_groups_buy_and_sell():
    import notify
    orders = [
        {"symbol": "ERY", "action": "BUY", "qty": 55, "lmt_price": 12.3},
        {"symbol": "SEF", "action": "SELL", "qty": 21, "lmt_price": 15.0},
        {"symbol": "XLB", "action": "BUY", "qty": 12.0, "lmt_price": 88.1},
    ]
    assert notify.order_group_lines(orders) == [
        "🟢 BUY 55 ERY · 12 XLB",
        "🔴 SELL 21 SEF",
    ]
    assert notify.order_group_lines(orders[:1], show_price=True) == [
        "🟢 BUY 55 ERY @ 12.30",
    ]
    assert notify.order_group_lines([]) == []


def test_digest_on_delivered_only_after_every_chunk_is_sent(monkeypatch):
    """#106：「只通知一次」的 caller 靠 on_delivered 記已通知 — 沒送到不能算。"""
    import notify
    results = iter([True, False])
    monkeypatch.setattr(notify, "tg_send", lambda msg, *, token, chat_id: next(results))
    monkeypatch.setattr(notify, "split_message", lambda msg: [msg[:5], msg[5:]])
    calls = []
    d = notify.Digest("調倉 2026-09-28", icon="📤")
    d.add("Rosa", "沒有送單", ["⏸ XYZ"], "warn", **DEST,
          on_delivered=lambda: calls.append("Rosa"))
    d.send()
    assert calls == []

    monkeypatch.setattr(notify, "tg_send", lambda msg, *, token, chat_id: True)
    d.add("Taylor", "送出 1 筆 MOO", ["🟢 BUY 1 XLK"], **{"token": "T2", "chat_id": "C2"},
          on_delivered=lambda: calls.append("Taylor"))
    d.send()
    assert calls == ["Rosa", "Taylor"]

