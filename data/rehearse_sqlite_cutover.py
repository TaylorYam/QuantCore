"""Offline SQLite release rehearsal using read-only copies of account logs.

一次性／研究用：不在排程或 Panel 裡執行，見 docs/data-roadmap.md（#236）。

The output directory must not exist. No broker, configuration, notification,
or runtime state module is used. Detailed artifacts stay local (not in Git).
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import patch


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rehearse(portfolio: Path, trades: Path, output: Path) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'automation'))
    import excel_logger as logger
    import sqlite_store as store

    portfolio, trades, output = portfolio.resolve(), trades.resolve(), output.resolve()
    if output.exists():
        raise ValueError('output directory must not exist; use a fresh rehearsal directory')
    before = {str(p): digest(p) for p in (portfolio, trades)}
    output.mkdir(parents=True)
    source = output / 'source'
    source.mkdir()
    p, t = source / portfolio.name, source / trades.name
    shutil.copy2(portfolio, p)
    shutil.copy2(trades, t)
    assert digest(p) == before[str(portfolio)] and digest(t) == before[str(trades)]
    db = output / 'quantcore_logs.sqlite3'
    projection = output / 'projection'
    projection.mkdir()
    pp, pt = projection / 'portfolio_tracker.xlsx', projection / 'trade_log.xlsx'
    shutil.copy2(p, pp)
    shutil.copy2(t, pt)
    report = {'source_sha256': before}

    def reconcile(a, b):
        result = store.reconcile(db, a, b)
        assert all(r['match'] for r in result.values()), result
        return result

    def rows():
        return {table: store._query(db, table)
                for table in ('portfolio_snapshots', 'trades')}

    config = output / 'config.json'               # 後端照 live 的方式寫在 config.json（#230）
    config.write_text('{"log_backend": "sqlite"}', encoding='utf-8')
    with patch.multiple(logger, PORTFOLIO_FILE=pp, TRADE_FILE=pt, SQLITE_FILE=db,
                        CONFIG_FILE=config):
        assert not db.exists()
        logger._ensure_sqlite_store()
        report['first_use_import'] = reconcile(p, t)
        store.export_excel(db, pp, pt)
        report['initial_export'] = reconcile(pp, pt)
        baseline = rows()
        backup = store.backup(db, output / 'before-runtime.sqlite3')
        position = SimpleNamespace(contract=SimpleNamespace(symbol='XLK'),
                                   position=5, avgCost=100)

        def snapshot(identity, price=110):
            logger.append_daily_snapshot(10000, [position], {'XLK': price},
                                         account_name='Rehearsal', capture_id=identity)

        snapshot('capture-a')
        snapshot('capture-a', 111)
        snapshot('capture-b', 111)
        placed = [dict(symbol='XLK', action='BUY', qty=2, lmt_price=100,
                       strategy='SR', order_id='offline-order-1')]
        for price in (101, 102):
            logger.append_trade_log(placed, [dict(symbol='XLK', filled=2,
                                    avg_price=price, status='Filled')],
                                    '2026-09-21', 'MOO', 1.3, False,
                                    account_name='Rehearsal')
        current = rows()
        assert len(current['portfolio_snapshots']) == len(baseline['portfolio_snapshots']) + 2
        assert len(current['trades']) == len(baseline['trades']) + 1
        assert current['trades'][-1]['fill_price'] == 102
        report['runtime_export'] = reconcile(pp, pt)
        file_bytes = [f.read_bytes() for f in (pp, pt)]
        original_insert = store._insert

        def fail(conn, table, records):
            original_insert(conn, table, records)
            raise RuntimeError('injected SQL failure')

        with patch.object(store, '_insert', fail):
            try:
                snapshot('must-rollback')
            except RuntimeError as error:
                assert str(error) == 'injected SQL failure'
            else:
                raise AssertionError('failure injection did not execute')
        assert rows() == current
        assert [f.read_bytes() for f in (pp, pt)] == file_bytes
        report['transaction_rollback_and_excel_bytes_unchanged'] = True
        runtime_backup = store.backup(db, output / 'after-runtime.sqlite3')
        snapshot('after-backup')
        store.restore(runtime_backup, db)
        assert rows() == current  # Includes internal capture/order identities.
        store.export_excel(db, pp, pt)
        report['runtime_backup_restore'] = reconcile(pp, pt)
        store.restore(backup, db)
        assert rows() == baseline
        store.export_excel(db, pp, pt)
        report['baseline_restore'] = reconcile(p, t)

    assert {str(f): digest(f) for f in (portfolio, trades)} == before
    report['source_bytes_unchanged'] = True
    (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--portfolio', type=Path, required=True)
    parser.add_argument('--trades', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = rehearse(args.portfolio, args.trades, args.output)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
