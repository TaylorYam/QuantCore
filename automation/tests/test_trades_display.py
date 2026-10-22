"""最近成交（#150）：成交列格式化與策略標籤（Panel 用，不依賴 GUI）。

成交本身經 `ledger` 讀出（#209）；這裡用寫端寫入 → `read_account_trades` 測整條路。
"""
from __future__ import annotations

import pytest

import excel_logger
from ledger import TradeRow
from trades_display import read_account_trades, strategy_label, trade_line


def trade(**kw):
    tr = dict(datetime="2026-09-29 21:30:05", symbol="XLK", action="BUY", qty=10,
              fill_price=230.5, strategy="SR", status="Filled", dry_run=False)
    tr.update(kw)
    return TradeRow(**tr)


def test_row_cells_and_colors():
    row = trade_line(trade())
    assert row.cells() == ("21:30:05", "XLK", "BUY", "10", "$230.50", "SR", "Filled")
    assert row.datetime == "2026-09-29 21:30:05"
    assert (row.action_color, row.status_color) == ("green", "green")
    assert row.colors() == ("text_dim", "text", "green", "text", "text", "text_dim", "green")


def test_sell_is_red_and_dry_run_is_tagged_and_yellow():
    row = trade_line(trade(action="SELL", status="DRY RUN", dry_run=True))
    assert row.action_color == "red"
    assert (row.status, row.status_color) == ("DRY RUN [DRY]", "yellow")
    # 只有 status 本身含 DRY 才變黃；dry_run 旗標只加標籤
    assert trade_line(trade(dry_run=True)).cells()[-1] == "Filled [DRY]"
    assert trade_line(trade(dry_run=True)).status_color == "green"


def test_zero_or_nan_fill_price_is_a_dash():
    for px in (0.0, -1.0, float("nan")):
        assert trade_line(trade(fill_price=px)).price == "—"


def test_short_datetime_is_kept_whole():
    assert trade_line(trade(datetime="21:30")).time == "21:30"


# ── 策略標籤 ─────────────────────────────────────────────────────────

def test_etf_trades_are_labelled_like_the_portfolio():
    """#187：類股／反向 ETF 的成交標籤和 Portfolio 同一套規則（依 Allocation），不看寫入值。"""
    def labels(alloc):
        return {sym: strategy_label(trade(symbol=sym, strategy=raw), alloc)
                for sym, raw in (("XLK", "SR+SkewLS"), ("REW", "SkewLS"), ("AAPL", "SP500"))}

    assert labels((0.0, 1.0)) == {"XLK": "SkewLS", "REW": "SkewLS", "AAPL": "Stocks"}
    assert labels((0.3, 0.2)) == {"XLK": "SR+SkewLS", "REW": "SkewLS", "AAPL": "Stocks"}
    assert labels((0.8, 0.0)) == {"XLK": "SR", "REW": "SkewLS", "AAPL": "Stocks"}
    assert labels(None)["XLK"] == "SR"                  # 不知道 Allocation：舊規則


def _old_stock_label(raw: str) -> str:
    """#209 之前用字串猜的規則（非 ETF 成交）。"""
    return "SR" if "SR" in raw and "SP500" not in raw and "Combo" not in raw else "Stocks"


@pytest.mark.parametrize("written", [
    # trade_log 裡實際出現過的 Strategy 寫法，加上 Sync／手動與空白
    "SP500", "EXIT", "SR", "SR+SkewLS", "SkewLS", "QuantCore Combo",
    "QuantCore SP500 Only", "QuantCore SR Only", "Sync", "Manual", "nan", "",
    "SR+SP500 Combo",
])
def test_stock_trades_keep_the_old_label_for_every_written_strategy(written):
    assert strategy_label(trade(symbol="AAPL", strategy=written)) == _old_stock_label(written)


# ── 寫端寫入 → 畫面列 ─────────────────────────────────────────────────

@pytest.fixture
def trade_log(monkeypatch, tmp_path):
    monkeypatch.setattr(excel_logger, "TRADE_FILE", tmp_path / "trade_log.xlsx")
    monkeypatch.delenv("QUANTCORE_LOG_BACKEND", raising=False)

    def write(account, symbol, strategy, *, fill=True):
        placed = [{"symbol": symbol, "action": "BUY", "qty": 3, "lmt_price": 12.0,
                   "strategy": strategy}]
        fills = [{"symbol": symbol, "filled": 3, "avg_price": 12.5, "status": "Filled"}] if fill else []
        excel_logger.append_trade_log(placed, fills, "2026-10-05", "MOO", 1.3, False,
                                      account_name=account)
    return write


def test_account_trades_are_screen_rows_newest_first_with_that_accounts_allocation(trade_log):
    """alloc 由 Portfolio 頁讀數來源從同一輪的策略參數帶進來（#231），這裡不再讀設定。"""
    trade_log("Taylor", "AAPL", "SP500", fill=False)
    trade_log("Taylor", "XLK", "SR+SkewLS")
    trade_log("Rosa", "XLB", "SR")
    rows = read_account_trades("Taylor", alloc=(0.0, 1.0))
    assert [r.cells()[1:] for r in rows] == [
        ("XLK", "BUY", "3", "$12.50", "SkewLS", "Filled"),
        ("AAPL", "BUY", "3", "—", "Stocks", "DRY_RUN"),
    ]
    assert read_account_trades("Taylor")[0].strategy == "SR"       # 沒有 alloc：類股 ETF 算 SR


def test_missing_or_broken_log_shows_no_trades(tmp_path):
    assert read_account_trades("Taylor", tmp_path / "missing.xlsx") == []
    broken = tmp_path / "broken.xlsx"
    broken.write_bytes(b"not excel")
    assert read_account_trades("Taylor", broken) == []
