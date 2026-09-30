"""最近成交（#150）：讀 trade_log 與成交列格式化，Tk TradesView 與新 Panel 共用。"""
from __future__ import annotations

import math

import pandas as pd

from trades_display import read_latest_trades, trade_row


def trade(**kw):
    tr = {"datetime": "2026-09-29 21:30:05", "symbol": "XLK", "action": "BUY", "qty": 10,
          "fill_price": 230.5, "strategy": "SR", "status": "Filled", "dry_run": False}
    tr.update(kw)
    return tr


def test_row_cells_and_colors():
    row = trade_row(trade())
    assert row.cells() == ("21:30:05", "XLK", "BUY", "10", "$230.50", "SR", "Filled")
    assert row.datetime == "2026-09-29 21:30:05"
    assert (row.action_color, row.status_color) == ("green", "green")


def test_sell_is_red_and_dry_run_is_tagged_and_yellow():
    row = trade_row(trade(action="SELL", status="DRY RUN", dry_run=True))
    assert row.action_color == "red"
    assert (row.status, row.status_color) == ("DRY RUN [DRY]", "yellow")
    # 只有 status 本身含 DRY 才變黃；dry_run 旗標只加標籤
    assert trade_row(trade(dry_run=True)).cells()[-1] == "Filled [DRY]"
    assert trade_row(trade(dry_run=True)).status_color == "green"


def test_missing_zero_or_nan_fill_price_is_a_dash():
    for px in (0, -1.0, float("nan")):
        assert trade_row(trade(fill_price=px)).price == "—"
    tr = trade()
    del tr["fill_price"]
    assert trade_row(tr).price == "—"


def test_short_datetime_is_kept_whole():
    assert trade_row(trade(datetime="21:30")).time == "21:30"


def _write_log(path, rows):
    pd.DataFrame(rows).to_excel(path, sheet_name="Trades", index=False)


def test_read_latest_trades_keeps_seven_days_newest_first_for_one_account(tmp_path):
    today = pd.Timestamp.now().normalize()
    day = lambda n: (today - pd.Timedelta(days=n)).strftime("%Y-%m-%d")
    log = tmp_path / "trade_log.xlsx"
    _write_log(log, [
        {"Date": day(30), "DateTime": f"{day(30)} 21:30:00", "Account": "Taylor",
         "Symbol": "OLD", "Action": "BUY", "Qty": 1, "FillPrice": 1, "Strategy": "SR",
         "FillStatus": "Filled", "DryRun": False},
        {"Date": day(2), "DateTime": f"{day(2)} 21:30:00", "Account": "Taylor",
         "Symbol": "XLK", "Action": "BUY", "Qty": 3, "FillPrice": "12.5", "Strategy": "SP500",
         "FillStatus": "Filled", "DryRun": False},
        {"Date": day(1), "DateTime": f"{day(1)} 21:30:00", "Account": "Taylor",
         "Symbol": "MSFT", "Action": "SELL", "Qty": 2, "FillPrice": "bad",
         "Strategy": "SR+SP500 Combo", "FillStatus": "DRY RUN", "DryRun": True},
        {"Date": day(1), "DateTime": f"{day(1)} 21:31:00", "Account": "Rosa",
         "Symbol": "XLB", "Action": "BUY", "Qty": 5, "FillPrice": 3, "Strategy": "SR",
         "FillStatus": "Filled", "DryRun": False},
    ])
    trades = read_latest_trades("Taylor", log)
    assert [t["symbol"] for t in trades] == ["MSFT", "XLK"]           # 新到舊、超過 7 天不列
    assert trades[0]["strategy"] == "Stocks" and trades[0]["fill_price"] == 0.0
    assert trades[1]["strategy"] == "SR" and trades[1]["fill_price"] == 12.5  # SR_ETFS 一律 SR
    assert [t["symbol"] for t in read_latest_trades("Rosa", log)] == ["XLB"]
    assert read_latest_trades("Nobody", log) == []


def test_read_latest_trades_missing_or_broken_log_is_empty(tmp_path):
    assert read_latest_trades("Taylor", tmp_path / "missing.xlsx") == []
    broken = tmp_path / "broken.xlsx"
    broken.write_bytes(b"not excel")
    assert read_latest_trades("Taylor", broken) == []


def test_nan_fill_price_from_the_log_shows_a_dash(tmp_path):
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    log = tmp_path / "trade_log.xlsx"
    _write_log(log, [{"Date": today, "DateTime": f"{today} 21:30:00", "Account": "Taylor",
                      "Symbol": "AAPL", "Action": "BUY", "Qty": 1, "FillPrice": None,
                      "Strategy": "SP500", "FillStatus": "Filled", "DryRun": False}])
    [tr] = read_latest_trades("Taylor", log)
    assert math.isnan(tr["fill_price"])
    assert trade_row(tr).price == "—"
