"""Offline acceptance tests for roadmap item 3's SQLite staging store."""

from pathlib import Path

import pandas as pd
import pytest

import sqlite_store as store


def frames():
    snapshots = pd.DataFrame([
        {
            "Date": "2026-09-21", "Account": "Taylor", "NAV": 10000,
            "Cushion%": 25.0, "ExcessLiq": 4000, "Symbol": "XLK",
            "Strategy": "SR", "Shares": 5, "AvgCost": 100,
            "MktPx": 110, "MktVal": 550, "UnrealPnL": 50, "UnrealPnL%": 10,
        },
        {
            "Date": "2026-09-21", "Account": "Yvonne", "NAV": 20000,
            "Cushion%": 30.0, "ExcessLiq": 7000, "Symbol": "REW",
            "Strategy": "SkewLS", "Shares": 8, "AvgCost": 40,
            "MktPx": 42, "MktVal": 336, "UnrealPnL": 16, "UnrealPnL%": 5,
        },
    ])
    trades = pd.DataFrame([
        {
            "DateTime": "2026-09-21 21:00:01", "Date": "2026-09-21",
            "Account": "Taylor", "SignalDate": "2026-09-21", "Strategy": "SR",
            "Symbol": "XLK", "Action": "BUY", "Qty": 5, "EstPrice": 109,
            "EstValue": 545, "FillPrice": 110, "FillQty": 5, "FillValue": 550,
            "FillStatus": "Filled", "OrderType": "MOO", "Leverage": 1.3,
            "DryRun": False,
        },
        {
            "DateTime": "2026-09-21 21:00:02", "Date": "2026-09-21",
            "Account": "Yvonne", "SignalDate": "2026-09-21", "Strategy": "SkewLS",
            "Symbol": "REW", "Action": "SELL", "Qty": 2, "EstPrice": 42,
            "EstValue": 84, "FillPrice": "", "FillQty": "", "FillValue": "",
            "FillStatus": "DRY_RUN", "OrderType": "MOO", "Leverage": 1.5,
            "DryRun": True,
        },
    ])
    return snapshots, trades


def test_import_is_idempotent_and_updates_a_daily_snapshot(tmp_path):
    db = tmp_path / "logs.sqlite3"
    snapshots, trades = frames()

    assert store.import_frames(db, snapshots, trades) == {
        "portfolio_snapshots": 2, "trades": 2,
    }
    assert store.import_frames(db, snapshots, trades) == {
        "portfolio_snapshots": 2, "trades": 2,
    }

    changed = snapshots.copy()
    changed.loc[0, "MktPx"] = 111
    store.import_frames(db, changed, None)
    report = store.reconcile(db)
    assert report["portfolio_snapshots"]["database_rows"] == 2
    assert report["trades"]["database_rows"] == 2
    values = {row["symbol"]: row["mkt_px"] for row in store._query(db, "portfolio_snapshots")}
    assert values["XLK"] == 111.0


def test_failed_transaction_does_not_publish_partial_import(tmp_path, monkeypatch):
    db = tmp_path / "logs.sqlite3"
    snapshots, trades = frames()
    store.import_frames(db, snapshots.iloc[:1], None)

    original = store._insert

    def fail_after_snapshot(conn, table, records):
        if table == "trades":
            raise RuntimeError("simulated writer failure")
        return original(conn, table, records)

    monkeypatch.setattr(store, "_insert", fail_after_snapshot)
    with pytest.raises(RuntimeError, match="simulated"):
        store.import_frames(db, snapshots.iloc[1:], trades)

    assert len(store._query(db, "portfolio_snapshots")) == 1
    assert store._query(db, "portfolio_snapshots")[0]["symbol"] == "XLK"
    assert store._query(db, "trades") == []


def test_backup_restore_round_trip(tmp_path):
    db = tmp_path / "logs.sqlite3"
    backup = tmp_path / "logs.backup.sqlite3"
    snapshots, trades = frames()
    store.import_frames(db, snapshots, trades)
    store.backup(db, backup)

    extra = snapshots.iloc[:1].copy()
    extra.loc[0, "Date"] = "2026-09-22"
    store.import_frames(db, extra, None)
    assert len(store._query(db, "portfolio_snapshots")) == 3

    store.restore(backup, db)
    assert len(store._query(db, "portfolio_snapshots")) == 2
    assert len(store._query(db, "trades")) == 2


def test_export_then_reconcile_is_lossless(tmp_path):
    db = tmp_path / "logs.sqlite3"
    snapshots, trades = frames()
    store.import_frames(db, snapshots, trades)
    portfolio = tmp_path / "portfolio.xlsx"
    trade = tmp_path / "trade.xlsx"
    assert store.export_excel(db, portfolio, trade) == {
        "portfolio_snapshots": 2, "trades": 2,
    }
    report = store.reconcile(db, portfolio, trade)
    assert report["portfolio_snapshots"]["match"] is True
    assert report["trades"]["match"] is True
    assert report["portfolio_snapshots"]["source_hash"] == report["portfolio_snapshots"]["database_hash"]
    assert report["trades"]["source_hash"] == report["trades"]["database_hash"]


def test_same_day_snapshot_rows_are_preserved_by_occurrence(tmp_path):
    db = tmp_path / "logs.sqlite3"
    snapshots, _ = frames()
    duplicated = pd.concat([snapshots, snapshots.iloc[[0]]], ignore_index=True)
    store.import_frames(db, duplicated, None)
    assert len(store._query(db, "portfolio_snapshots")) == 3
    source = tmp_path / "portfolio.xlsx"
    with pd.ExcelWriter(source, engine="openpyxl") as writer:
        duplicated.to_excel(writer, sheet_name="Daily", index=False)
    report = store.reconcile(db, source, None)
    assert report["portfolio_snapshots"]["match"] is True
    assert report["portfolio_snapshots"]["source_rows"] == 3
    assert report["portfolio_snapshots"]["source_unique_rows"] == 3
    assert report["portfolio_snapshots"]["duplicates_dropped"] == 0
    assert report["portfolio_snapshots"]["match"] is True


def test_runtime_state_is_not_a_sqlite_table(tmp_path):
    db = store.initialize(tmp_path / "logs.sqlite3")
    conn = store._connect(db)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    finally:
        conn.close()
    assert "sp500_state" not in tables and "skewls_state" not in tables
