"""Offline tests for the reversible Excel/SQLite log backend seam."""

import json
import logging
from pathlib import Path

import pandas as pd
import pytest

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


def test_failed_logger_transaction_preserves_both_projections(monkeypatch, tmp_path):
    import pytest

    portfolio, trade_file, db = _patch_paths(monkeypatch, tmp_path)
    excel_logger.append_daily_snapshot(10000, [Position('XLK', 5, 100)],
                                      {'XLK': 110}, account_name='Taylor',
                                      capture_id='initial')
    before_files = [p.read_bytes() for p in (portfolio, trade_file)]
    before_rows = sqlite_store._query(db, 'portfolio_snapshots')
    insert = sqlite_store._insert

    def fail_after_insert(conn, table, records):
        insert(conn, table, records)
        raise RuntimeError('injected after SQL insert')

    monkeypatch.setattr(sqlite_store, '_insert', fail_after_insert)
    with pytest.raises(RuntimeError, match='injected'):
        excel_logger.append_daily_snapshot(11000, [Position('XLK', 6, 100)],
                                          {'XLK': 120}, account_name='Taylor',
                                          capture_id='next')
    assert sqlite_store._query(db, 'portfolio_snapshots') == before_rows
    assert [p.read_bytes() for p in (portfolio, trade_file)] == before_files


def test_first_use_import_and_export_failure_recovery(monkeypatch, tmp_path):
    portfolio, trade_file, db = _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setenv('QUANTCORE_LOG_BACKEND', 'excel')
    excel_logger.append_daily_snapshot(10000, [Position('XLK', 5, 100)],
                                      {'XLK': 110}, account_name='Taylor')
    assert not db.exists()
    old = portfolio.read_bytes()
    monkeypatch.setenv('QUANTCORE_LOG_BACKEND', 'sqlite')
    export = sqlite_store.export_excel

    def fail_export(*args):
        raise PermissionError('projection locked')

    monkeypatch.setattr(sqlite_store, 'export_excel', fail_export)
    with pytest.raises(PermissionError, match='locked'):
        excel_logger.append_daily_snapshot(11000, [Position('XLK', 6, 100)],
                                          {'XLK': 120}, account_name='Taylor',
                                          capture_id='next')
    assert len(sqlite_store._query(db, 'portfolio_snapshots')) == 2
    assert portfolio.read_bytes() == old
    monkeypatch.setattr(sqlite_store, 'export_excel', export)
    # Recover the projection from the committed DB; never repeat broker orders.
    export(db, portfolio, trade_file)
    assert all(r['match'] for r in sqlite_store.reconcile(db, portfolio, trade_file).values())
    excel_logger.append_daily_snapshot(11000, [Position('XLK', 6, 100)],
                                      {'XLK': 120}, account_name='Taylor',
                                      capture_id='next')
    assert len(sqlite_store._query(db, 'portfolio_snapshots')) == 2


def test_daily_snapshot_attributes_long_sector_etfs_by_allocation(monkeypatch):
    """#181：SR 0% 時，做多的類股 ETF 在快照的 Strategy 欄是 SkewLS。"""
    written = []
    monkeypatch.setattr(excel_logger, "_append_rows",
                        lambda sheet, rows, identities: written.extend(rows))
    positions = [Position("XLC", 60, 113.55), Position("SIJ", 61, 18.93),
                 Position("AAPL", 3, 150.0)]
    prices = {"XLC": 110.23, "SIJ": 18.32, "AAPL": 160.0}
    excel_logger.append_daily_snapshot(15_000.0, positions, prices, account_name="Rosa",
                                       strategy_alloc=(0.0, 1.0))
    assert {r["Symbol"]: r["Strategy"] for r in written} == {
        "XLC": "SkewLS", "SIJ": "SkewLS", "AAPL": ""}
    written.clear()
    excel_logger.append_daily_snapshot(15_000.0, positions[:1], prices, account_name="Rosa")
    assert written[0]["Strategy"] == "SR"                  # 沒給 Allocation：維持舊規則


def test_excel_backend_writes_columns_in_the_declared_order(monkeypatch, tmp_path):
    """#209：寫端 dict 的欄序和 SNAPSHOT_FIELDS／TRADE_FIELDS（SQLite 匯出用）一致。"""
    portfolio, trade_file, _ = _patch_paths(monkeypatch, tmp_path)
    monkeypatch.delenv("QUANTCORE_LOG_BACKEND")
    excel_logger.append_daily_snapshot(10000, [Position("XLK", 5, 100)], {"XLK": 110},
                                       account_name="Taylor")
    excel_logger.append_trade_log([{"symbol": "XLK", "action": "BUY", "qty": 2,
                                    "lmt_price": 100}], [], "2026-09-21", "MOO", 1.3, True,
                                  account_name="Taylor")
    assert tuple(pd.read_excel(portfolio, sheet_name="Daily").columns) == excel_logger.SNAPSHOT_FIELDS
    assert tuple(pd.read_excel(trade_file, sheet_name="Trades").columns) == excel_logger.TRADE_FIELDS


# ── 後端只在一處決定（#230）────────────────────────────────────────────────

def _backend_world(monkeypatch, tmp_path, config=None, env=None):
    path = tmp_path / "config.json"
    if config is not None:
        path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(excel_logger, "CONFIG_FILE", path)
    monkeypatch.setattr(excel_logger, "_announced", set())
    if env is None:
        monkeypatch.delenv(excel_logger.BACKEND_ENV, raising=False)
    else:
        monkeypatch.setenv(excel_logger.BACKEND_ENV, env)


@pytest.mark.parametrize("config, env, backend, source", [
    (None, None, "excel", "預設"),                                  # 沒有 config.json
    ({"host": "127.0.0.1"}, None, "excel", "預設"),                 # 沒有欄位
    ({"log_backend": "sqlite"}, None, "sqlite", "config.json 的 log_backend"),
    ({"log_backend": " SQLite "}, None, "sqlite", "config.json 的 log_backend"),
    ({"log_backend": "excel"}, None, "excel", "config.json 的 log_backend"),
    ({"log_backend": "excel"}, "sqlite", "sqlite", "環境變數 QUANTCORE_LOG_BACKEND"),
    (None, "sqlite", "sqlite", "環境變數 QUANTCORE_LOG_BACKEND"),   # 過渡期的 wrapper
    ({"log_backend": "sqlite"}, "  ", "sqlite", "config.json 的 log_backend"),
])
def test_backend_comes_from_env_then_config_then_default(monkeypatch, tmp_path, caplog,
                                                         config, env, backend, source):
    _backend_world(monkeypatch, tmp_path, config, env)
    with caplog.at_level(logging.INFO, logger=excel_logger.log.name):
        assert excel_logger._log_backend() == backend
        assert excel_logger._log_backend() == backend
    assert [r.getMessage() for r in caplog.records] == [f"交易紀錄後端：{backend}（{source}）"]


def test_config_backend_routes_the_write_to_sqlite_without_env(monkeypatch, tmp_path):
    """Panel 與排程不帶 wrapper：config.json 一個欄位就讓兩本 xlsx 改由 SQLite 匯出。"""
    portfolio, trade_file, db = _patch_paths(monkeypatch, tmp_path)
    _backend_world(monkeypatch, tmp_path, {"log_backend": "sqlite"})
    excel_logger.append_daily_snapshot(10000, [Position("XLK", 5, 100)], {"XLK": 110},
                                       account_name="Taylor", capture_id="c1")
    assert len(sqlite_store._query(db, "portfolio_snapshots")) == 1
    assert pd.read_excel(portfolio, sheet_name="Daily").shape[0] == 1


def test_unknown_backend_is_refused_before_writing(monkeypatch, tmp_path):
    portfolio, _, db = _patch_paths(monkeypatch, tmp_path)
    _backend_world(monkeypatch, tmp_path, {"log_backend": "csv"})
    with pytest.raises(ValueError, match="csv"):
        excel_logger.append_daily_snapshot(10000, [Position("XLK", 5, 100)], {"XLK": 110},
                                           account_name="Taylor")
    assert not portfolio.exists() and not db.exists()
