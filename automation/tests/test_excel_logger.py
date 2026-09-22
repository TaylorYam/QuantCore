"""Offline tests for the reversible Excel/SQLite log backend seam."""

from pathlib import Path

import pandas as pd

import excel_logger
import sqlite_store


class Contract:
    def __init__(self, symbol):
        self.symbol = symbol


class Position:
    def __init__(self, symbol, position, avg_cost):
        self.contract = Contract(symbol)
        self.position = position
        self.avgCost = avg_cost


def _patch_paths(monkeypatch, tmp_path: Path):
    portfolio = tmp_path / "portfolio_tracker.xlsx"
    trades = tmp_path / "trade_log.xlsx"
    db = tmp_path / "quantcore_logs.sqlite3"
    monkeypatch.setattr(excel_logger, "PORTFOLIO_FILE", portfolio)
    monkeypatch.setattr(excel_logger, "TRADE_FILE", trades)
    monkeypatch.setattr(excel_logger, "SQLITE_FILE", db)
    monkeypatch.setenv("QUANTCORE_LOG_BACKEND", "sqlite")
    return portfolio, trades, db


def test_sqlite_backend_exports_and_retries_by_identity(monkeypatch, tmp_path):
    portfolio, trade_file, db = _patch_paths(monkeypatch, tmp_path)
    positions = [Position("XLK", 5, 100)]

    excel_logger.append_daily_snapshot(
        10000, positions, {"XLK": 110}, account_name="Taylor",
        capture_id="capture-1",
    )
    # Retrying the same capture updates the row instead of duplicating it.
    excel_logger.append_daily_snapshot(
        10000, positions, {"XLK": 111}, account_name="Taylor",
        capture_id="capture-1",
    )
    assert len(sqlite_store._query(db, "portfolio_snapshots")) == 1
    assert sqlite_store._query(db, "portfolio_snapshots")[0]["mkt_px"] == 111.0
    assert pd.read_excel(portfolio, sheet_name="Daily").shape[0] == 1

    placed = [{
        "symbol": "XLK", "action": "BUY", "qty": 2,
        "lmt_price": 100, "strategy": "SR", "order_id": "42",
    }]
    fills = [{
        "symbol": "XLK", "filled": 2, "avg_price": 101,
        "status": "Filled",
    }]
    excel_logger.append_trade_log(
        placed, fills, "2026-09-21", "MOO", 1.3, False,
        account_name="Taylor",
    )
    excel_logger.append_trade_log(
        placed, [{**fills[0], "avg_price": 102}], "2026-09-21", "MOO", 1.3, False,
        account_name="Taylor",
    )
    trade_rows = sqlite_store._query(db, "trades")
    assert len(trade_rows) == 1
    assert trade_rows[0]["fill_price"] == 102.0
    assert pd.read_excel(trade_file, sheet_name="Trades").shape[0] == 1
