"""Offline timing of existing SP500 code on a caller-supplied dataset.

No strategy changes, report writing, live state, network or broker connections.
Each repetition has fresh application caches; the OS file cache is NOT flushed.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import platform
from statistics import median
import subprocess
from time import perf_counter
from unittest.mock import patch

import pandas as pd


def fingerprint(root: Path) -> list[dict]:
    paths = [root / "reference" / "sp500_constituents_daily.xlsx",
             root / "etfs" / "SPY.csv", root / "etfs" / "SSO.csv"]
    paths.extend(sorted((root / "stocks").glob("*.csv")))
    return [{"file": str(p.relative_to(root)).replace("\\", "/"),
             "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]


def measure_once(root: Path, start: str, end: str) -> dict:
    from strategies.sp500_momentum import config, utils
    from strategies.sp500_momentum.backtester import SP500Backtester
    from strategies.sp500_momentum.selection import SelectionEngine

    # Temporary in-process path bindings only; never edit config.py or live JSON.
    bindings = {"STOCK_DIR": root / "stocks", "ETF_DIR": root / "etfs",
                "REF_DIR": root / "reference"}
    load_stock = utils.load_stock
    csv_seconds, csv_calls, csv_failures = 0.0, 0, []
    metric_seconds, metric_calls = 0.0, 0
    phase = "preload"
    run_csv_calls = 0

    def timed_load(path):
        nonlocal csv_seconds, csv_calls, run_csv_calls
        t = perf_counter()
        try:
            frame = load_stock(path)
            if frame is None:
                csv_failures.append(Path(path).name)
            return frame
        finally:
            csv_seconds += perf_counter() - t
            csv_calls += 1
            if phase == "run":
                run_csv_calls += 1

    with patch.multiple(config, **bindings), patch.object(utils, "load_stock", timed_load):
        t = perf_counter()
        selector = SelectionEngine()
        constituents_seconds = perf_counter() - t
        if selector.constituents_df.empty:
            raise ValueError("Constituents load failed")
        if pd.Timestamp(start) < selector.constituents_df.index.min() or pd.Timestamp(end) > selector.constituents_df.index.max():
            raise ValueError("benchmark range must be inside Constituents snapshot bounds")

        t = perf_counter()
        selector.preload_all_data(pd.Timestamp(start), pd.Timestamp(end))
        preload_seconds = perf_counter() - t
        stock_csv_seconds, stock_csv_calls = csv_seconds, csv_calls
        prepared_bytes = sum(int(df.memory_usage(index=True, deep=True).sum())
                             for df in selector.data_cache.values())
        t = perf_counter()
        spy = utils.load_benchmark(config.ETF_DIR / "SPY.csv")
        sso = utils.load_benchmark(config.ETF_DIR / "SSO.csv")
        benchmark_load_seconds = perf_counter() - t
        if spy is None or sso is None:
            raise ValueError("SPY/SSO load failed")
        t = perf_counter()
        backtest = SP500Backtester(start_date=start, end_date=end, selector=selector,
                                  spy_df=spy, sso_df=sso, write_reports=False)
        setup_seconds = perf_counter() - t
        metrics = selector.calculate_metrics

        def timed_metrics(*args, **kwargs):
            nonlocal metric_seconds, metric_calls
            t = perf_counter()
            try:
                return metrics(*args, **kwargs)
            finally:
                metric_seconds += perf_counter() - t
                metric_calls += 1

        phase = "run"
        t = perf_counter()
        with patch.object(selector, "calculate_metrics", timed_metrics):
            backtest.run()
        run_seconds = perf_counter() - t
    result_bytes = pd.DataFrame(backtest.history).to_json(date_format="iso", double_precision=15).encode()
    trade_bytes = pd.DataFrame(backtest.trades).to_json(date_format="iso", double_precision=15).encode()
    return {"constituents_excel_seconds": constituents_seconds,
            "preload_total_seconds": preload_seconds,
            "stock_csv_seconds": stock_csv_seconds,
            "preload_other_seconds": max(0, preload_seconds - stock_csv_seconds),
            "benchmark_csv_seconds": benchmark_load_seconds, "backtest_setup_seconds": setup_seconds,
            "backtest_run_seconds": run_seconds, "metrics_seconds_within_run": metric_seconds,
            "total_stage_seconds": constituents_seconds + preload_seconds + benchmark_load_seconds + setup_seconds + run_seconds,
            "stock_csv_calls": stock_csv_calls, "csv_calls_during_run": run_csv_calls,
            "csv_failures": sorted(set(csv_failures)), "metric_calls": metric_calls,
            "loaded_tickers": len(selector.data_cache), "prepared_dataframe_bytes": prepared_bytes,
            "backtest_sessions": int(((spy.index >= pd.Timestamp(start)) & (spy.index <= pd.Timestamp(end))).sum()),
            "history_sha256": hashlib.sha256(result_bytes).hexdigest(),
            "trades_sha256": hashlib.sha256(trade_bytes).hexdigest()}


def benchmark(root: Path, start: str, end: str, repeats: int = 3) -> dict:
    if repeats < 1:
        raise ValueError("repeats must be positive")
    if pd.Timestamp(start) > pd.Timestamp(end):
        raise ValueError("start must not exceed end")
    for value in (start, end):
        stamp = pd.Timestamp(value)
        if pd.isna(stamp) or stamp.tz is not None or stamp != stamp.normalize():
            raise ValueError("benchmark requires timezone-free session dates")
    before = fingerprint(root)
    runs = []
    for _ in range(repeats):
        with redirect_stdout(io.StringIO()):
            runs.append(measure_once(root, start, end))
    if fingerprint(root) != before:
        raise ValueError("dataset changed during benchmark; discard timings")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent.parent,
                            capture_output=True, text=True, check=True).stdout.strip()
    repository = Path(__file__).resolve().parent.parent
    code_files = [Path(__file__), *sorted((repository / "strategies" / "sp500_momentum").glob("*.py"))]
    code_sources = [{"file": str(p.relative_to(repository)).replace("\\", "/"),
                     "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in code_files]
    keys = [k for k in runs[0] if k.endswith("seconds") or k == "metrics_seconds_within_run"]
    return {"schema_version": 1, "strategy": "SP500", "start": start, "end": end,
            "git_commit": commit, "python": platform.python_version(), "pandas": pd.__version__,
            "platform": platform.platform(), "repeats": repeats, "runs": runs,
            "median_seconds": {k: median(r[k] for r in runs) for k in keys},
            "outputs_repeatable": len({(r["history_sha256"], r["trades_sha256"]) for r in runs}) == 1,
            "sources": before, "code_sources": code_sources,
            "limitations": ["SP500 only; not an SR/Skew LS or SQL comparison.",
                            "Fresh application caches; OS file cache not flushed, inputs hashed before timing.",
                            "Preload other includes ticker enumeration, EMA preparation and cache insertion.",
                            "Metrics time is nested inside backtest run; do not add it to total.",
                            "Timings include measurement overhead and exclude imports, hashing and result serialization.",
                            "Repeatable outputs are not an OLD/NEW strategy equivalence or performance claim."]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    try:
        result = benchmark(args.data_root.resolve(), args.start, args.end, args.repeats)
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return int(not result["outputs_repeatable"] or any(r["csv_failures"] for r in result["runs"]))


if __name__ == "__main__":
    raise SystemExit(main())
