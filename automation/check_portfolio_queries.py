"""Opt-in, one-round read-only acceptance check. Never starts the ControlPanel.

Run from a development worktree, with --live-readonly --source-root pointing to
the existing installation. Only connection identity is loaded; trading settings
and notification credentials are not carried into the test AccountCtx.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys
import threading
import time

from account_ctx import AccountCtx
from broker import acceptance_check_client_id, price_feed_client_id
from portfolio_queries import PortfolioQueries


def load_contexts(source_root):
    # 刻意不用 AccountCtx.from_name（#236 評估過）：它讀執行端自己的 accounts.json，
    # 會帶進配置比例；這裡要的是 --source-root 那份安裝的連線身分而已。
    config_dir = source_root / "automation"
    base = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
    accounts = json.loads((config_dir / "accounts.json").read_text(encoding="utf-8"))
    return {a["name"]: AccountCtx(
        name=a["name"], account=a.get("account", base.get("account", "")),
        host=a.get("host", base.get("host", "127.0.0.1")),
        port=int(a.get("port", base.get("port", 4001))),
        client_id=acceptance_check_client_id(i),
        dry_run=True, order_type="MOO", outside_rth=False, wait_fill_sec=0,
        alloc_sr=0, alloc_sp500=0, alloc_skewls=0,
        price_feed_port=int(base.get("price_feed_port", 4002)),
    ) for i, a in enumerate(accounts)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-readonly", action="store_true", required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    source = args.source_root.resolve()
    output = args.output.resolve()
    worktree = Path(__file__).resolve().parent.parent
    if str(worktree) not in sys.path:
        sys.path.insert(0, str(worktree))
    if worktree == source or output.is_relative_to(source):
        parser.error("Run in a separate worktree and save output outside the live installation")
    contexts = load_contexts(source)
    if not contexts:
        parser.error("No Accounts configured")
    if len(contexts) > 3:
        parser.error("This acceptance check is limited to three Accounts")

    # Read-only reference/CSV lookup; no file copy, symlink or live config changes.
    from strategies.sp500_momentum import config as sp_config
    sp_config.REF_DIR = source / "data" / "reference"
    sp_config.ETF_DIR = source / "data" / "etfs"
    sp_config.STOCK_DIR = source / "data" / "stocks"
    from data import market_data      # 參考價的 CSV 後備讀這個根目錄（#229）
    market_data.DATA_ROOT = source / "data"
    from portfolio_snapshot import fetch_snapshot
    logging.disable(logging.CRITICAL)  # IB diagnostic logs can contain Account IDs

    timings = {}
    lock = threading.Lock()
    started = time.monotonic()

    def fetch(name):
        before = time.monotonic() - started
        try:
            return fetch_snapshot(contexts[name], readonly=True)
        finally:
            with lock:
                timings[name] = {"started_s": before, "finished_s": time.monotonic() - started}

    queries = PortfolioQueries(contexts, fetch)
    try:
        queries.refresh()
        deadline = started + 180
        while any(queries.snapshot(n).running for n in contexts) and time.monotonic() < deadline:
            threading.Event().wait(0.05)
        report = {}
        for name, ctx in contexts.items():
            state = queries.snapshot(name)
            data = state.portfolio
            report[name] = {
                "client_id": ctx.client_id,
                "feed_client_id": price_feed_client_id(ctx.client_id, readonly=True),
                "success": state.portfolio is not None and state.error is None,
                "still_running": state.running,
                "nav_present": bool(data and data.nav),
                "holding_count": (len(data.etf_holdings) + len(data.stock_holdings)
                                  + len(data.other_holdings)) if data else 0,
                # Deliberately omit balances, symbols, Account IDs and raw errors.
                "query_failed": state.error is not None,
                **timings.get(name, {}),
            }
        intervals = list(timings.values())
        overlap = (len(intervals) == len(contexts) and
                   max(t["started_s"] for t in intervals) < min(t["finished_s"] for t in intervals))
        result = {"parallel_overlap": overlap, "accounts": report}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2))
        return 0 if overlap and all(r["success"] for r in report.values()) else 1
    finally:
        queries.close()


if __name__ == "__main__":
    raise SystemExit(main())
