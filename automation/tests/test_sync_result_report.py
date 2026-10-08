"""Sync Pos 送出後的紀錄與 Telegram（#154：從 Tk control_panel._sync_execute 抽出，
內容與原本逐字相同）。"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace as NS

import sync_position as sp


def plan():
    ctx = NS(name="Rosa", leverage=1.0, dry_run=False, tg_token="T", tg_chat_id="C")
    return NS(ctx=ctx, nav=123_456.789, sr_date="2026-09-30", sp500_date="2026-09-29")


PLACED = [{"symbol": "XLK", "action": "BUY", "qty": 10, "strategy": "SR"},
          {"symbol": "AAPL", "action": "SELL", "qty": 3}]
FILLS = [{"symbol": "XLK", "action": "BUY", "qty": 10, "filled": 10, "avg_price": 230.5,
          "status": "Filled"},
         {"symbol": "AAPL", "action": "SELL", "qty": 3, "filled": 0, "avg_price": 0.0,
          "status": "Submitted"}]


def test_trade_log_gets_one_row_per_placed_order(monkeypatch):
    import excel_logger
    calls = []
    monkeypatch.setattr(excel_logger, "append_trade_log",
                        lambda *a, **k: calls.append((a, k)))
    sp.log_sync_trades(plan(), PLACED, FILLS)
    assert calls == [
        (([PLACED[0]], FILLS, "SR:2026-09-30 SP500:2026-09-29", "LMT", 1.0, False),
         {"strategy": "SR", "account_name": "Rosa"}),
        (([PLACED[1]], FILLS, "SR:2026-09-30 SP500:2026-09-29", "LMT", 1.0, False),
         {"strategy": "Sync", "account_name": "Rosa"}),
    ]


def test_telegram_text_lists_fills(monkeypatch):
    text = sp.sync_result_text(plan(), PLACED, FILLS, today=date(2026, 10, 2))
    assert text == "\n".join([
        "<b>[Rosa] Sync Position — LMT</b>",
        "Date: 2026-10-02",
        "NAV: <b>$123,456.79</b>",
        "SR: 2026-09-30  |  SP500: 2026-09-29",
        "\n<b>成交結果 (2 筆):</b>",
        "  ✅ 🟢 BUY  10/10sh XLK  @ $230.50  ~$2,305  [Filled]",
        "  ⚠️ 🔴 SELL 0/3sh AAPL  未成交  [Submitted]",
    ])


def test_telegram_text_lists_orders_when_there_are_no_fill_reports():
    text = sp.sync_result_text(plan(), PLACED, [], today=date(2026, 10, 2))
    assert text.endswith("\n".join([
        "\n<b>委託 (2 筆):</b>",
        "  🟢 BUY  10sh XLK  [SR]",
        "  🔴 SELL 3sh AAPL  []",
    ]))


def test_send_goes_to_the_account_telegram(monkeypatch):
    import notify
    sent = []
    monkeypatch.setattr(notify, "send_account",                # 帳戶頻道（#207）
                        lambda name, msg: sent.append((name, msg)) or True)
    assert sp.send_sync_result(plan(), PLACED, FILLS) is True
    [(name, msg)] = sent
    assert name == "Rosa" and "[Rosa] Sync Position" in msg


def test_send_reports_when_the_message_was_not_sent(monkeypatch):
    import notify
    monkeypatch.setattr(notify, "send_account", lambda name, msg: False)
    assert sp.send_sync_result(plan(), PLACED, FILLS) is False
