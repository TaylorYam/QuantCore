"""交易紀錄讀取端（#209）：經 excel_logger 寫入、經 ledger 讀出，拿到同一筆。"""
from __future__ import annotations

import math
from datetime import date as real_date
from datetime import datetime

import pandas as pd
import pytest

import excel_logger
import ledger


class Contract:
    def __init__(self, symbol):
        self.symbol = symbol


class Position:
    def __init__(self, symbol, position, avg_cost):
        self.contract = Contract(symbol)
        self.position = position
        self.avgCost = avg_cost


@pytest.fixture
def logs(monkeypatch, tmp_path):
    """寫端與讀端都用 tmp 檔；預設 Excel 後端。"""
    monkeypatch.setattr(excel_logger, "PORTFOLIO_FILE", tmp_path / "portfolio_tracker.xlsx")
    monkeypatch.setattr(excel_logger, "TRADE_FILE", tmp_path / "trade_log.xlsx")
    monkeypatch.setattr(excel_logger, "SQLITE_FILE", tmp_path / "quantcore_logs.sqlite3")
    return tmp_path


def _on_day(monkeypatch, day: str):
    """讓寫端以為今天是 day（快照與交易的 Date 欄）。"""
    d = real_date.fromisoformat(day)

    class _Date(real_date):
        @classmethod
        def today(cls):
            return d

    monkeypatch.setattr(excel_logger, "date", _Date)


def _snapshot(nav, account, symbols=("XLK", "AAPL")):
    excel_logger.append_daily_snapshot(
        nav, [Position(s, 5, 100) for s in symbols], {s: 110 for s in symbols},
        account_name=account)


def _trade(account, symbol="XLK", *, strategy="SR", fill=True, dry_run=False, action="BUY"):
    placed = [{"symbol": symbol, "action": action, "qty": 2, "lmt_price": 100.0,
               "strategy": strategy}]
    fills = [{"symbol": symbol, "filled": 2, "avg_price": 101.25, "status": "Filled"}] if fill else []
    excel_logger.append_trade_log(placed, fills, "2026-10-05", "MOO", 1.3, dry_run,
                                  account_name=account)


# ── 每日 NAV ─────────────────────────────────────────────────────────

def test_daily_nav_reads_back_one_value_per_day_for_that_account(logs, monkeypatch):
    _on_day(monkeypatch, "2026-10-02")
    _snapshot(100_000.0, "Taylor")
    _snapshot(55_000.0, "Rosa")
    _on_day(monkeypatch, "2026-10-01")                  # 寫入順序與日期順序不同
    _snapshot(99_500.5, "Taylor")
    _on_day(monkeypatch, "2026-10-02")
    _snapshot(100_123.0, "Taylor")                      # 同一天再存一次：取第一筆

    nav = ledger.daily_nav("Taylor")
    assert list(nav.index) == [pd.Timestamp("2026-10-01"), pd.Timestamp("2026-10-02")]
    assert list(nav) == [99_500.5, 100_000.0]
    assert nav.dtype == float
    assert list(ledger.daily_nav("Rosa")) == [55_000.0]
    assert ledger.daily_nav("Nobody").empty


def test_daily_nav_reads_an_explicit_path(logs, monkeypatch, tmp_path):
    _on_day(monkeypatch, "2026-10-02")
    _snapshot(100_000.0, "Taylor")
    moved = tmp_path / "elsewhere.xlsx"
    excel_logger.PORTFOLIO_FILE.rename(moved)
    assert ledger.daily_nav("Taylor").empty                  # 預設路徑沒有檔
    assert list(ledger.daily_nav("Taylor", path=moved)) == [100_000.0]


def test_daily_nav_skips_non_numeric_nav(tmp_path):
    path = tmp_path / "portfolio_tracker.xlsx"
    pd.DataFrame({"Date": ["2026-10-01", "2026-10-02"], "Account": ["Taylor"] * 2,
                  "NAV": ["", 100.0]}).to_excel(path, sheet_name="Daily", index=False)
    nav = ledger.daily_nav("Taylor", path=path)
    assert list(nav.index) == [pd.Timestamp("2026-10-02")] and list(nav) == [100.0]


def test_daily_nav_without_a_file_is_empty(tmp_path):
    nav = ledger.daily_nav("Taylor", path=tmp_path / "missing.xlsx")
    assert nav.empty and nav.dtype == float


# ── 最近成交 ─────────────────────────────────────────────────────────

def test_a_written_trade_reads_back_as_the_same_trade(logs):
    _trade("Taylor", strategy="SR+SkewLS")
    [t] = ledger.recent_trades("Taylor")
    assert t == ledger.TradeRow(
        datetime=t.datetime, symbol="XLK", action="BUY", qty=2, fill_price=101.25,
        strategy="SR+SkewLS", status="Filled", dry_run=False)
    assert abs(datetime.strptime(t.datetime, "%Y-%m-%d %H:%M:%S") - datetime.now()).total_seconds() < 120


def test_an_unfilled_dry_run_trade_has_no_fill_price(logs):
    _trade("Taylor", symbol="AAPL", strategy="SP500", fill=False, dry_run=True, action="SELL")
    [t] = ledger.recent_trades("Taylor")
    assert (t.symbol, t.action, t.strategy, t.status, t.dry_run) == (
        "AAPL", "SELL", "SP500", "DRY_RUN", True)
    assert math.isnan(t.fill_price)


def test_recent_trades_are_newest_first_within_the_window_for_one_account(logs, monkeypatch):
    today = pd.Timestamp.now().normalize()
    for n, sym in ((30, "OLD"), (8, "EDGE_OUT"), (7, "EDGE_IN"), (1, "XLK")):
        _on_day(monkeypatch, f"{today - pd.Timedelta(days=n):%Y-%m-%d}")
        _trade("Taylor", sym)
    _trade("Rosa", "XLB")

    assert [t.symbol for t in ledger.recent_trades("Taylor")] == ["XLK", "EDGE_IN"]
    assert [t.symbol for t in ledger.recent_trades("Taylor", days=30)] == [
        "XLK", "EDGE_IN", "EDGE_OUT", "OLD"]
    assert [t.symbol for t in ledger.recent_trades("Rosa")] == ["XLB"]
    assert ledger.recent_trades("Nobody") == []


def test_recent_trades_without_a_file_is_empty(tmp_path):
    assert ledger.recent_trades("Taylor", path=tmp_path / "missing.xlsx") == []


def test_unreadable_fill_price_reads_as_zero(tmp_path):
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    path = tmp_path / "trade_log.xlsx"
    pd.DataFrame([{"Date": today, "DateTime": f"{today} 21:30:00", "Account": "Taylor",
                   "Symbol": "MSFT", "Action": "SELL", "Qty": 2, "FillPrice": "bad",
                   "Strategy": "SP500", "FillStatus": "Filled", "DryRun": False}]
                 ).to_excel(path, sheet_name="Trades", index=False)
    [t] = ledger.recent_trades("Taylor", path=path)
    assert t.fill_price == 0.0


# ── SQLite 後端：Excel 投影讀回同樣的值 ──────────────────────────────

def test_sqlite_backend_projection_reads_back_the_same(logs, monkeypatch, use_log_backend):
    use_log_backend("sqlite")
    _on_day(monkeypatch, "2026-10-02")
    _snapshot(100_000.0, "Taylor")
    _trade("Taylor", strategy="SkewLS")
    assert list(ledger.daily_nav("Taylor")) == [100_000.0]
    [t] = ledger.recent_trades("Taylor", days=10_000)
    assert (t.symbol, t.qty, t.fill_price, t.strategy, t.status, t.dry_run) == (
        "XLK", 2, 101.25, "SkewLS", "Filled", False)
