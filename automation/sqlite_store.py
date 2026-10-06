"""Transactional staging store for portfolio snapshots and trade records.

This module is deliberately separate from the live Excel logger.  It provides
the reversible part of roadmap item 3: import the existing Excel logs into a
single SQLite file, reconcile the rows, and export them again.  Runtime state
files (strategy counters and threshold state) are not part of this store.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Mapping

import pandas as pd

# 欄位名稱與工作表只定義在寫端（#209）
from excel_logger import (
    ACCOUNT, ACTION, AVG_COST, CUSHION_PCT, DATE, DATETIME, DRY_RUN, EST_PRICE,
    EST_VALUE, EXCESS_LIQ, FILL_PRICE, FILL_QTY, FILL_STATUS, FILL_VALUE, LEVERAGE,
    MKT_PX, MKT_VAL, NAV, ORDER_TYPE, QTY, SHARES, SIGNAL_DATE, SNAPSHOT_FIELDS,
    STRATEGY, SYMBOL, TRADE_FIELDS, UNREAL_PNL, UNREAL_PNL_PCT,
)
from excel_logger import PORT_SHEET as PORTFOLIO_SHEET
from excel_logger import TRADE_SHEET


SCHEMA_VERSION = 2

_SNAPSHOT_COLUMNS = {
    "date": "TEXT NOT NULL",
    "account": "TEXT NOT NULL",
    "nav": "REAL",
    "cushion_pct": "REAL",
    "excess_liq": "REAL",
    "symbol": "TEXT NOT NULL",
    "strategy": "TEXT NOT NULL",
    "shares": "REAL",
    "avg_cost": "REAL",
    "mkt_px": "REAL",
    "mkt_val": "REAL",
    "unreal_pnl": "REAL",
    "unreal_pnl_pct": "REAL",
    "occurrence": "INTEGER NOT NULL",
}
_TRADE_COLUMNS = {
    "datetime": "TEXT NOT NULL",
    "date": "TEXT NOT NULL",
    "account": "TEXT NOT NULL",
    "signal_date": "TEXT NOT NULL",
    "strategy": "TEXT NOT NULL",
    "symbol": "TEXT NOT NULL",
    "action": "TEXT NOT NULL",
    "qty": "INTEGER NOT NULL",
    "est_price": "REAL",
    "est_value": "REAL",
    "fill_price": "REAL",
    "fill_qty": "REAL",
    "fill_value": "REAL",
    "fill_status": "TEXT NOT NULL",
    "order_type": "TEXT NOT NULL",
    "leverage": "REAL",
    "dry_run": "INTEGER NOT NULL",
    "occurrence": "INTEGER NOT NULL",
}


class StoreError(RuntimeError):
    """Raised for invalid records or an incompatible SQLite store."""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            idempotency_key TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            account TEXT NOT NULL,
            nav REAL,
            cushion_pct REAL,
            excess_liq REAL,
            symbol TEXT NOT NULL,
            strategy TEXT NOT NULL,
            shares REAL,
            avg_cost REAL,
            mkt_px REAL,
            mkt_val REAL,
            unreal_pnl REAL,
            unreal_pnl_pct REAL,
            occurrence INTEGER NOT NULL,
            UNIQUE(date, account, symbol, occurrence)
        );
        CREATE TABLE IF NOT EXISTS trades (
            idempotency_key TEXT PRIMARY KEY,
            datetime TEXT NOT NULL,
            date TEXT NOT NULL,
            account TEXT NOT NULL,
            signal_date TEXT NOT NULL,
            strategy TEXT NOT NULL,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            qty INTEGER NOT NULL,
            est_price REAL,
            est_value REAL,
            fill_price REAL,
            fill_qty REAL,
            fill_value REAL,
            fill_status TEXT NOT NULL,
            order_type TEXT NOT NULL,
            leverage REAL,
            dry_run INTEGER NOT NULL,
            occurrence INTEGER NOT NULL,
            UNIQUE(datetime, account, symbol, action, qty, occurrence)
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_date_account
            ON portfolio_snapshots(date, account);
        CREATE INDEX IF NOT EXISTS idx_trades_datetime_account
            ON trades(datetime, account);
        """
    )
    row = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    elif int(row[0]) != SCHEMA_VERSION:
        raise StoreError(f"unsupported SQLite schema version: {row[0]}")


def initialize(path: str | Path) -> Path:
    """Create or validate a store and return its resolved path."""
    target = Path(path).expanduser().resolve()
    conn = _connect(target)
    try:
        _schema(conn)
        conn.commit()
    finally:
        conn.close()
    return target


def _missing(frame: pd.DataFrame, fields: Iterable[str]) -> list[str]:
    return [field for field in fields if field not in frame.columns]


def _text(value: object, *, field: str, required: bool = False) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)) or pd.isna(value):
        if required:
            raise StoreError(f"missing required field: {field}")
        return ""
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    text = str(value).strip()
    if required and not text:
        raise StoreError(f"missing required field: {field}")
    return text


def _number(value: object, *, field: str, integer: bool = False) -> float | int | None:
    if value is None or (isinstance(value, str) and not value.strip()) or pd.isna(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise StoreError(f"invalid numeric field {field}: {value!r}") from exc
    if integer:
        if not number.is_integer():
            raise StoreError(f"invalid integer field {field}: {value!r}")
        return int(number)
    return number


def _bool(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        return int(value)
    if value is None or (isinstance(value, float) and pd.isna(value)) or pd.isna(value):
        return 0
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return 1
        if lowered in {"false", "0", "no", "n", "off", ""}:
            return 0
    number = _number(value, field=field, integer=True)
    if number not in (0, 1):
        raise StoreError(f"invalid boolean field {field}: {value!r}")
    return int(number)


def _row(frame: pd.DataFrame, fields: tuple[str, ...]) -> list[dict]:
    missing = _missing(frame, fields)
    if missing:
        raise StoreError(f"missing columns: {', '.join(missing)}")
    return frame.loc[:, list(fields)].to_dict("records")


def _snapshot_item(source: Mapping[str, object]) -> dict:
    """Normalize one legacy snapshot row without assigning its occurrence."""
    return {
        "date": _text(source[DATE], field=DATE, required=True),
        "account": _text(source[ACCOUNT], field=ACCOUNT),
        "nav": _number(source[NAV], field=NAV),
        "cushion_pct": _number(source[CUSHION_PCT], field=CUSHION_PCT),
        "excess_liq": _number(source[EXCESS_LIQ], field=EXCESS_LIQ),
        "symbol": _text(source[SYMBOL], field=SYMBOL, required=True),
        "strategy": _text(source[STRATEGY], field=STRATEGY),
        "shares": _number(source[SHARES], field=SHARES),
        "avg_cost": _number(source[AVG_COST], field=AVG_COST),
        "mkt_px": _number(source[MKT_PX], field=MKT_PX),
        "mkt_val": _number(source[MKT_VAL], field=MKT_VAL),
        "unreal_pnl": _number(source[UNREAL_PNL], field=UNREAL_PNL),
        "unreal_pnl_pct": _number(source[UNREAL_PNL_PCT], field=UNREAL_PNL_PCT),
    }


def _trade_item(source: Mapping[str, object]) -> dict:
    """Normalize one legacy trade row without assigning its occurrence."""
    return {
        "datetime": _text(source[DATETIME], field=DATETIME, required=True),
        "date": _text(source[DATE], field=DATE, required=True),
        "account": _text(source[ACCOUNT], field=ACCOUNT),
        "signal_date": _text(source[SIGNAL_DATE], field=SIGNAL_DATE),
        "strategy": _text(source[STRATEGY], field=STRATEGY),
        "symbol": _text(source[SYMBOL], field=SYMBOL, required=True),
        "action": _text(source[ACTION], field=ACTION, required=True),
        "qty": _number(source[QTY], field=QTY, integer=True),
        "est_price": _number(source[EST_PRICE], field=EST_PRICE),
        "est_value": _number(source[EST_VALUE], field=EST_VALUE),
        "fill_price": _number(source[FILL_PRICE], field=FILL_PRICE),
        "fill_qty": _number(source[FILL_QTY], field=FILL_QTY),
        "fill_value": _number(source[FILL_VALUE], field=FILL_VALUE),
        "fill_status": _text(source[FILL_STATUS], field=FILL_STATUS),
        "order_type": _text(source[ORDER_TYPE], field=ORDER_TYPE),
        "leverage": _number(source[LEVERAGE], field=LEVERAGE),
        "dry_run": _bool(source[DRY_RUN], field=DRY_RUN),
    }


def _snapshot_records(frame: pd.DataFrame) -> dict[str, dict]:
    result: dict[str, dict] = {}
    occurrences: Counter[tuple[str, str, str]] = Counter()
    for source in _row(frame, SNAPSHOT_FIELDS):
        item = _snapshot_item(source)
        # Excel has no timestamp for snapshots.  Preserve every source row;
        # repeated rows can represent separate captures with different NAV or
        # prices.  The occurrence is stable for a given source file order and
        # makes re-importing that file idempotent without losing same-day rows.
        natural = (item["date"], item["account"], item["symbol"])
        occurrence = occurrences[natural]
        occurrences[natural] += 1
        item["occurrence"] = occurrence
        key = f"snapshot:{item['date']}:{item['account']}:{item['symbol']}:{occurrence}"
        item["idempotency_key"] = key
        result[key] = item
    return result


def _trade_records(frame: pd.DataFrame) -> dict[str, dict]:
    result: dict[str, dict] = {}
    occurrences: Counter[str] = Counter()
    for source in _row(frame, TRADE_FIELDS):
        base = _trade_item(source)
        natural = json.dumps(
            {k: base[k] for k in (
                "datetime", "date", "account", "signal_date", "strategy",
                "symbol", "action", "qty", "order_type", "dry_run",
            )}, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        occurrence = occurrences[natural]
        occurrences[natural] += 1
        key = "trade:" + hashlib.sha256(
            f"{natural}|{occurrence}".encode("utf-8")
        ).hexdigest()
        base["occurrence"] = occurrence
        base["idempotency_key"] = key
        result[key] = base
    return result


def _insert(conn: sqlite3.Connection, table: str, records: Mapping[str, dict]) -> int:
    if table == "portfolio_snapshots":
        fields = ["idempotency_key", *_SNAPSHOT_COLUMNS]
        placeholders = ",".join("?" for _ in fields)
        sql = f"INSERT INTO {table} ({','.join(fields)}) VALUES ({placeholders}) " \
              "ON CONFLICT(idempotency_key) DO UPDATE SET " + \
              ",".join(f"{f}=excluded.{f}" for f in _SNAPSHOT_COLUMNS if f != "occurrence")
    elif table == "trades":
        fields = ["idempotency_key", *_TRADE_COLUMNS]
        placeholders = ",".join("?" for _ in fields)
        sql = f"INSERT INTO {table} ({','.join(fields)}) VALUES ({placeholders}) " \
              "ON CONFLICT(idempotency_key) DO UPDATE SET " + \
              ",".join(f"{f}=excluded.{f}" for f in _TRADE_COLUMNS if f != "occurrence")
    else:
        raise StoreError(f"unknown table: {table}")
    for item in records.values():
        conn.execute(sql, tuple(item[field] for field in fields))
    return len(records)


def _runtime_snapshot_records(
    conn: sqlite3.Connection,
    frame: pd.DataFrame,
    identities: Iterable[str],
) -> dict[str, dict]:
    """Normalize runtime rows using caller-owned capture identities.

    Excel snapshots have no timestamp, so a caller must provide an identity
    for each capture.  Existing identities reuse their stored occurrence;
    new identities append after the current rows for the same date/account/
    symbol.  This keeps retries idempotent while preserving repeated captures.
    """
    rows = _row(frame, SNAPSHOT_FIELDS)
    keys = [str(identity).strip() for identity in identities]
    if len(keys) != len(rows) or any(not key for key in keys):
        raise StoreError("runtime snapshot identities must match non-empty row count")
    if len(set(keys)) != len(keys):
        raise StoreError("runtime snapshot identities must be unique")
    existing = {
        row["idempotency_key"]: int(row["occurrence"])
        for row in conn.execute(
            "SELECT idempotency_key, occurrence FROM portfolio_snapshots "
            "WHERE idempotency_key IN (%s)" % ",".join("?" for _ in keys),
            keys,
        ).fetchall()
    } if keys else {}
    next_occurrence: dict[tuple[str, str, str], int] = {}
    result: dict[str, dict] = {}
    for source, key in zip(rows, keys):
        item = _snapshot_item(source)
        natural = (item["date"], item["account"], item["symbol"])
        if key in existing:
            occurrence = existing[key]
        else:
            if natural not in next_occurrence:
                row = conn.execute(
                    "SELECT COALESCE(MAX(occurrence), -1) + 1 "
                    "FROM portfolio_snapshots WHERE date=? AND account=? AND symbol=?",
                    natural,
                ).fetchone()
                next_occurrence[natural] = int(row[0])
            occurrence = next_occurrence[natural]
            next_occurrence[natural] += 1
        item["occurrence"] = occurrence
        item["idempotency_key"] = key
        result[key] = item
    return result


def _runtime_trade_records(
    conn: sqlite3.Connection,
    frame: pd.DataFrame,
    identities: Iterable[str],
) -> dict[str, dict]:
    """Normalize runtime rows using explicit order/fill identities."""
    rows = _row(frame, TRADE_FIELDS)
    keys = [str(identity).strip() for identity in identities]
    if len(keys) != len(rows) or any(not key for key in keys):
        raise StoreError("runtime trade identities must match non-empty row count")
    if len(set(keys)) != len(keys):
        raise StoreError("runtime trade identities must be unique")
    existing = {
        row["idempotency_key"]: int(row["occurrence"])
        for row in conn.execute(
            "SELECT idempotency_key, occurrence FROM trades "
            "WHERE idempotency_key IN (%s)" % ",".join("?" for _ in keys),
            keys,
        ).fetchall()
    } if keys else {}
    next_occurrence: dict[tuple, int] = {}
    result: dict[str, dict] = {}
    for source, key in zip(rows, keys):
        item = _trade_item(source)
        natural = (
            item["datetime"], item["account"], item["symbol"],
            item["action"], item["qty"],
        )
        if key in existing:
            occurrence = existing[key]
        else:
            if natural not in next_occurrence:
                row = conn.execute(
                    "SELECT COALESCE(MAX(occurrence), -1) + 1 FROM trades "
                    "WHERE datetime=? AND account=? AND symbol=? AND action=? AND qty=?",
                    natural,
                ).fetchone()
                next_occurrence[natural] = int(row[0])
            occurrence = next_occurrence[natural]
            next_occurrence[natural] += 1
        item["occurrence"] = occurrence
        item["idempotency_key"] = key
        result[key] = item
    return result


def append_runtime(
    path: str | Path,
    *,
    snapshots: pd.DataFrame | None = None,
    snapshot_identities: Iterable[str] | None = None,
    trades: pd.DataFrame | None = None,
    trade_identities: Iterable[str] | None = None,
) -> dict[str, int]:
    """Append runtime records in one transaction using explicit identities.

    The identity is supplied by the caller (a capture id or broker/order id),
    so retrying the same event updates its row instead of appending a second
    copy.  This API deliberately does not infer identity from mutable prices
    or from the current clock.
    """
    snapshot_ids = list(snapshot_identities or [])
    trade_ids = list(trade_identities or [])
    if snapshots is not None and len(snapshots) != len(snapshot_ids):
        raise StoreError("snapshot_identities are required for every runtime snapshot")
    if trades is not None and len(trades) != len(trade_ids):
        raise StoreError("trade_identities are required for every runtime trade")
    target = initialize(path)
    conn = _connect(target)
    try:
        conn.execute("BEGIN IMMEDIATE")
        snapshot_records = (
            _runtime_snapshot_records(conn, snapshots, snapshot_ids)
            if snapshots is not None else {}
        )
        trade_records = (
            _runtime_trade_records(conn, trades, trade_ids)
            if trades is not None else {}
        )
        inserted = {
            "portfolio_snapshots": _insert(conn, "portfolio_snapshots", snapshot_records),
            "trades": _insert(conn, "trades", trade_records),
        }
        conn.commit()
        return inserted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def import_frames(path: str | Path, snapshots: pd.DataFrame | None = None,
                  trades: pd.DataFrame | None = None) -> dict[str, int]:
    """Import frames in one transaction; an exception rolls back everything."""
    snapshot_records = _snapshot_records(snapshots) if snapshots is not None else {}
    trade_records = _trade_records(trades) if trades is not None else {}
    target = initialize(path)
    conn = _connect(target)
    try:
        conn.execute("BEGIN IMMEDIATE")
        inserted = {
            "portfolio_snapshots": _insert(conn, "portfolio_snapshots", snapshot_records),
            "trades": _insert(conn, "trades", trade_records),
        }
        conn.commit()
        return inserted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def import_excel(path: str | Path, portfolio_file: str | Path | None = None,
                 trade_file: str | Path | None = None) -> dict[str, int]:
    """Import existing Excel logs without changing either source file."""
    portfolio = Path(portfolio_file) if portfolio_file else None
    trade = Path(trade_file) if trade_file else None
    snapshots = pd.read_excel(portfolio, sheet_name=PORTFOLIO_SHEET) if portfolio and portfolio.exists() else None
    trades = pd.read_excel(trade, sheet_name=TRADE_SHEET) if trade and trade.exists() else None
    return import_frames(path, snapshots=snapshots, trades=trades)


def _query(path: str | Path, table: str) -> list[dict]:
    if table not in {"portfolio_snapshots", "trades"}:
        raise StoreError(f"unknown table: {table}")
    target = initialize(path)
    conn = _connect(target)
    try:
        # Preserve append order for the Excel compatibility projection and UI;
        # reconciliation hashes are key-sorted independently of this order.
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _export_frame(path: str | Path, table: str) -> pd.DataFrame:
    rows = _query(path, table)
    if table == "portfolio_snapshots":
        rename = {
            "date": DATE, "account": ACCOUNT, "nav": NAV,
            "cushion_pct": CUSHION_PCT, "excess_liq": EXCESS_LIQ,
            "symbol": SYMBOL, "strategy": STRATEGY, "shares": SHARES,
            "avg_cost": AVG_COST, "mkt_px": MKT_PX, "mkt_val": MKT_VAL,
            "unreal_pnl": UNREAL_PNL, "unreal_pnl_pct": UNREAL_PNL_PCT,
        }
        data = [{rename[k]: row[k] for k in rename} for row in rows]
        return pd.DataFrame(data, columns=list(SNAPSHOT_FIELDS))
    rename = {
        "datetime": DATETIME, "date": DATE, "account": ACCOUNT,
        "signal_date": SIGNAL_DATE, "strategy": STRATEGY, "symbol": SYMBOL,
        "action": ACTION, "qty": QTY, "est_price": EST_PRICE,
        "est_value": EST_VALUE, "fill_price": FILL_PRICE, "fill_qty": FILL_QTY,
        "fill_value": FILL_VALUE, "fill_status": FILL_STATUS, "order_type": ORDER_TYPE,
        "leverage": LEVERAGE, "dry_run": DRY_RUN,
    }
    data = [{rename[k]: row[k] for k in rename} for row in rows]
    return pd.DataFrame(data, columns=list(TRADE_FIELDS))


def export_excel(path: str | Path, portfolio_file: str | Path,
                 trade_file: str | Path) -> dict[str, int]:
    """Export the SQLite store to the legacy two-workbook shape."""
    portfolio = Path(portfolio_file)
    trade = Path(trade_file)
    portfolio.parent.mkdir(parents=True, exist_ok=True)
    trade.parent.mkdir(parents=True, exist_ok=True)
    snapshots = _export_frame(path, "portfolio_snapshots")
    trades = _export_frame(path, "trades")
    with pd.ExcelWriter(portfolio, engine="openpyxl") as writer:
        snapshots.to_excel(writer, sheet_name=PORTFOLIO_SHEET, index=False)
    with pd.ExcelWriter(trade, engine="openpyxl") as writer:
        trades.to_excel(writer, sheet_name=TRADE_SHEET, index=False)
    return {"portfolio_snapshots": len(snapshots), "trades": len(trades)}


def _hash_records(records: Mapping[str, dict]) -> str:
    # Source Excel order and the database query order are intentionally
    # different.  Hash by key so reconciliation reports equality for the
    # same records regardless of insertion or SQL sort order.
    payload = json.dumps(
        [[key, records[key]] for key in sorted(records)],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def reconcile(path: str | Path, portfolio_file: str | Path | None = None,
              trade_file: str | Path | None = None) -> dict:
    """Compare canonical source rows with the SQLite rows.

    Snapshot rows sharing a date/account/symbol are retained with a source
    occurrence number because Excel does not carry a capture timestamp.
    Re-importing the same source is idempotent while preserving those rows.
    """
    source_snapshots = pd.read_excel(portfolio_file, sheet_name=PORTFOLIO_SHEET) if portfolio_file and Path(portfolio_file).exists() else None
    source_trades = pd.read_excel(trade_file, sheet_name=TRADE_SHEET) if trade_file and Path(trade_file).exists() else None
    expected_snapshots = _snapshot_records(source_snapshots) if source_snapshots is not None else {}
    expected_trades = _trade_records(source_trades) if source_trades is not None else {}
    # Excel has no runtime identity columns. Compare the public projection
    # using the same occurrence-aware canonicalization on both sides. Runtime
    # keys remain in SQLite and are verified separately by backup/retry checks.
    actual_snapshots = _snapshot_records(_export_frame(path, "portfolio_snapshots"))
    actual_trades = _trade_records(_export_frame(path, "trades"))

    def result(expected: dict, actual: dict, table: str, source_count: int) -> dict:
        return {
            "source_rows": source_count,
            "source_unique_rows": len(expected),
            "database_rows": len(actual),
            "duplicates_dropped": source_count - len(expected),
            "source_hash": _hash_records(expected),
            "database_hash": _hash_records(actual),
            "missing_keys": sorted(set(expected) - set(actual)),
            "unexpected_keys": sorted(set(actual) - set(expected)),
            "match": expected == actual,
            "table": table,
        }

    return {
        "portfolio_snapshots": result(
            expected_snapshots, actual_snapshots, "portfolio_snapshots",
            len(source_snapshots) if source_snapshots is not None else 0,
        ),
        "trades": result(
            expected_trades, actual_trades, "trades",
            len(source_trades) if source_trades is not None else 0,
        ),
    }


def backup(path: str | Path, destination: str | Path) -> Path:
    """Make an atomic SQLite backup using SQLite's online backup API."""
    source = initialize(path)
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    src = _connect(source)
    dst = _connect(tmp)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        # sqlite3.Connection's context manager commits but does not close the
        # handle.  Close both handles before os.replace; Windows refuses to
        # rename a still-open temporary database.
        src.close()
        dst.close()
    try:
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def restore(backup_file: str | Path, destination: str | Path) -> Path:
    """Restore a SQLite backup atomically, validating it before publication."""
    source = initialize(backup_file)
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    src = _connect(source)
    dst = _connect(tmp)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        src.close()
        dst.close()
    try:
        initialize(tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import", help="import Excel logs into SQLite")
    imp.add_argument("--db", required=True, type=Path)
    imp.add_argument("--portfolio", type=Path)
    imp.add_argument("--trades", type=Path)
    rec = sub.add_parser("reconcile", help="compare Excel logs with SQLite")
    rec.add_argument("--db", required=True, type=Path)
    rec.add_argument("--portfolio", type=Path)
    rec.add_argument("--trades", type=Path)
    exp = sub.add_parser("export", help="export SQLite to Excel logs")
    exp.add_argument("--db", required=True, type=Path)
    exp.add_argument("--portfolio", required=True, type=Path)
    exp.add_argument("--trades", required=True, type=Path)
    for name in ("backup", "restore"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--source", required=True, type=Path)
        cmd.add_argument("--destination", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "import":
        result = import_excel(args.db, args.portfolio, args.trades)
    elif args.command == "reconcile":
        result = reconcile(args.db, args.portfolio, args.trades)
    elif args.command == "export":
        result = export_excel(args.db, args.portfolio, args.trades)
    elif args.command == "backup":
        result = {"backup": str(backup(args.source, args.destination))}
    else:
        result = {"restore": str(restore(args.source, args.destination))}
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
