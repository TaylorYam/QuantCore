"""Offline, repeatable CSV / Parquet benchmark for the market-data archive.

All writes go below ``--output-dir``. The supplied data root is fingerprinted
before and after the run and is never modified. This script benchmarks CSV and
per-ticker Parquet; it records DuckDB availability but does not install or
benchmark DuckDB.

Example::

    python data/benchmark_storage_formats.py --data-root C:/QuantCore-master/data \
        --output-dir C:/QuantCore-bench/issue-93 --start 2025-01-02 \
        --end 2025-12-31 --repeats 3
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import platform
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any
from unittest.mock import patch

import pandas as pd
import pyarrow

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))


MATERIALITY_HURDLE = {
    "end_to_end_speedup": "at least 15% median improvement in 2 of 3 strategies",
    "regression_limit": "no more than 5% median end-to-end regression in any strategy",
    "correctness": "all per-ticker frames and full strategy output hashes identical",
    "update_path": "append and full-history atomic replacement both preserve row values and dates",
    "storage_support": "at least 30% archive size reduction is useful but is not sufficient alone",
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_manifest(data_root: Path) -> list[dict[str, Any]]:
    paths = sorted((data_root / "stocks").glob("*.csv"))
    paths += sorted((data_root / "etfs").glob("*.csv"))
    paths += sorted((data_root / "reference").glob("*.xlsx"))
    manifest = []
    for path in paths:
        manifest.append({"file": path.relative_to(data_root).as_posix(),
                         "bytes": path.stat().st_size,
                         "sha256": _sha256_file(path)})
    return manifest


def manifest_digest(manifest: list[dict[str, Any]]) -> str:
    blob = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    frame.index = pd.to_datetime(frame.index).normalize()
    return frame[~frame.index.duplicated(keep="last")].sort_index()


def read_market(path: Path, storage_format: str, parquet_root: Path | None = None) -> pd.DataFrame:
    """Read one ticker as existing loaders do, preserving their date cleanup."""
    if storage_format == "parquet":
        if parquet_root is None:
            raise ValueError("parquet_root is required for Parquet reads")
        source = parquet_root / path.parent.name / f"{path.stem}.parquet"
        frame = pd.read_parquet(source)
    else:
        frame = pd.read_csv(path, index_col="Date", parse_dates=True)
    return _normalize(frame)


def convert_archive(data_root: Path, parquet_root: Path) -> dict[str, Any]:
    """Convert each stock/ETF file to an indexed Snappy Parquet file."""
    started = perf_counter()
    paths = sorted((data_root / "stocks").glob("*.csv"))
    paths += sorted((data_root / "etfs").glob("*.csv"))
    for source in paths:
        relative_parent = source.parent.name
        target_dir = parquet_root / relative_parent
        target_dir.mkdir(parents=True, exist_ok=True)
        frame = pd.read_csv(source, index_col="Date", parse_dates=True)
        frame.to_parquet(target_dir / f"{source.stem}.parquet",
                         engine="pyarrow", compression="snappy", index=True)
    elapsed = perf_counter() - started
    csv_bytes = sum(p.stat().st_size for p in paths)
    parquet_paths = [parquet_root / p.parent.name / f"{p.stem}.parquet" for p in paths]
    parquet_bytes = sum(p.stat().st_size for p in parquet_paths)
    return {"files": len(paths), "parquet_files": len(parquet_paths),
            "conversion_seconds": elapsed, "csv_bytes": csv_bytes,
            "parquet_bytes": parquet_bytes,
            "size_reduction_percent": 100 * (1 - parquet_bytes / csv_bytes)}


def _frame_digest(frame: pd.DataFrame | pd.Series) -> str:
    if isinstance(frame, pd.Series):
        frame = frame.to_frame()
    digest = hashlib.sha256()
    schema = {"columns": [str(c) for c in frame.columns],
              "dtypes": [str(dtype) for dtype in frame.dtypes],
              "index_name": frame.index.name,
              "index_dtype": str(frame.index.dtype),
              "shape": list(frame.shape)}
    digest.update(json.dumps(schema, sort_keys=True).encode())
    digest.update(pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes())
    return digest.hexdigest()


def _value_digests(value: Any) -> dict[str, str]:
    if isinstance(value, pd.DataFrame | pd.Series):
        return {"value": _frame_digest(value)}
    if isinstance(value, dict):
        return {str(key): digest for key, child in sorted(value.items(), key=lambda p: str(p[0]))
                for digest in _value_digests(child).values()}
    if isinstance(value, (list, tuple)):
        return {str(i): digest for i, child in enumerate(value)
                for digest in _value_digests(child).values()}
    return {"value": hashlib.sha256(repr(value).encode()).hexdigest()}


def _validate_round_trips(data_root: Path, parquet_root: Path) -> dict[str, Any]:
    paths = sorted((data_root / "stocks").glob("*.csv"))
    paths += sorted((data_root / "etfs").glob("*.csv"))
    digests = {}
    for path in paths:
        csv_frame = read_market(path, "csv")
        parquet_frame = read_market(path, "parquet", parquet_root)
        pd.testing.assert_frame_equal(csv_frame, parquet_frame, check_exact=True)
        digests[f"{path.parent.name}/{path.stem}"] = _frame_digest(csv_frame)
    aggregate = hashlib.sha256(json.dumps(digests, sort_keys=True).encode()).hexdigest()
    return {"ticker_frames": len(digests), "all_exact": True,
            "aggregate_frame_sha256": aggregate}


def _strategy_once(strategy: str, storage_format: str, data_root: Path,
                   parquet_root: Path | None) -> dict[str, Any]:
    etf_root = data_root / "etfs"
    resolver = lambda name: read_market(etf_root / f"{name}.csv", storage_format, parquet_root)
    if strategy == "SR":
        from strategies.sector_rotation import backtest, config, data_loader
        with patch.object(config, "DATA_DIR", etf_root), patch.object(data_loader, "_load_ticker", resolver):
            load_start = perf_counter()
            loaded = data_loader.load_data()
            load_seconds = perf_counter() - load_start
            run_start = perf_counter()
            output = backtest.run_full_backtest(loaded)
            run_seconds = perf_counter() - run_start
    elif strategy == "Skew LS":
        from strategies.sector_skew_ls import backtest, config, data_loader
        with patch.object(config, "DATA_DIR", etf_root), patch.object(data_loader, "_load_one", resolver):
            load_start = perf_counter()
            loaded = data_loader.load_data()
            load_seconds = perf_counter() - load_start
            run_start = perf_counter()
            output = backtest.run_backtest_proxy(loaded)
            run_seconds = perf_counter() - run_start
    else:
        raise ValueError(f"unknown strategy {strategy}")
    loaded_hashes = _value_digests(loaded)
    output_hashes = _value_digests(output)
    return {"load_seconds": load_seconds, "backtest_seconds": run_seconds,
            "end_to_end_seconds": load_seconds + run_seconds,
            "loaded_hashes": loaded_hashes, "output_hashes": output_hashes}


def _benchmark_sector_strategy(strategy: str, data_root: Path, parquet_root: Path,
                               repeats: int) -> dict[str, Any]:
    formats = {}
    for storage_format in ("csv", "parquet"):
        runs = [_strategy_once(strategy, storage_format, data_root,
                               parquet_root if storage_format == "parquet" else None)
                for _ in range(repeats)]
        stable_loads = len({json.dumps(run["loaded_hashes"], sort_keys=True) for run in runs}) == 1
        stable_outputs = len({json.dumps(run["output_hashes"], sort_keys=True) for run in runs}) == 1
        formats[storage_format] = {
            "runs": runs,
            "median_seconds": {
                key: median(run[key] for run in runs)
                for key in ("load_seconds", "backtest_seconds", "end_to_end_seconds")},
            "outputs_repeatable": stable_outputs,
            "inputs_repeatable": stable_loads,
            "loaded_hashes": runs[0]["loaded_hashes"],
            "output_hashes": runs[0]["output_hashes"],
            "cache_timing": {
                "first_fresh_loader_run_seconds": runs[0]["end_to_end_seconds"],
                "subsequent_runs_median_seconds": median(run["end_to_end_seconds"] for run in runs[1:]),
                "meaning": "data-loader state is fresh on each run; operating-system file cache is not flushed",
            },
        }
    formats["frames_identical"] = formats["csv"]["loaded_hashes"] == formats["parquet"]["loaded_hashes"]
    formats["strategy_outputs_identical"] = formats["csv"]["output_hashes"] == formats["parquet"]["output_hashes"]
    return formats


def _benchmark_sp500(data_root: Path, parquet_root: Path, output_dir: Path,
                     start: str, end: str, repeats: int) -> dict[str, Any]:
    from data import benchmark_pipeline
    from strategies.sp500_momentum import utils

    formats = {}
    cache_dir = output_dir / "constituents-cache"
    for storage_format in ("csv", "parquet"):
        def reader(path: str | Path, fmt: str = storage_format) -> pd.DataFrame | None:
            try:
                frame = read_market(Path(path), fmt,
                                    parquet_root if fmt == "parquet" else None)
                return frame if not frame.empty and "Close" in frame.columns else None
            except (OSError, ValueError, KeyError, ImportError):
                return None

        with patch.object(utils, "load_stock", reader):
            report = benchmark_pipeline.benchmark(
                data_root, start, end, repeats,
                constituents_cache=True,
                warm_constituents_cache=True,
                constituents_cache_dir=cache_dir,
            )
        measured = report["runs"]
        report["cache_timing"] = {
            "first_fresh_loader_run_seconds": measured[0]["total_stage_seconds"],
            "subsequent_runs_median_seconds": median(run["total_stage_seconds"] for run in measured[1:]),
            "first_run_stock_read_seconds": measured[0]["stock_csv_seconds"],
            "subsequent_stock_reads_median_seconds": median(run["stock_csv_seconds"] for run in measured[1:]),
            "meaning": "fresh Constituents selector and stock cache each run; operating-system file cache is not flushed",
        }
        formats[storage_format] = report
    formats["outputs_identical"] = all(
        (a["history_sha256"], a["trades_sha256"]) ==
        (b["history_sha256"], b["trades_sha256"])
        for a, b in zip(formats["csv"]["runs"], formats["parquet"]["runs"]))
    return formats


def _atomic_csv_replace(path: Path, frame: pd.DataFrame) -> dict[str, float]:
    from data.price_continuity import frame_bytes
    started = perf_counter()
    content = frame_bytes(frame)
    serialize = perf_counter() - started
    tmp = path.with_name(f".{path.name}.bench-tmp")
    started = perf_counter()
    tmp.write_bytes(content)
    stage = perf_counter() - started
    started = perf_counter()
    os.replace(tmp, path)
    publish = perf_counter() - started
    return {"prepare_seconds": serialize, "stage_write_seconds": stage,
            "publish_seconds": publish,
            "total_seconds": serialize + stage + publish}


def _atomic_parquet_replace(path: Path, frame: pd.DataFrame) -> dict[str, float]:
    tmp = path.with_name(f".{path.name}.bench-tmp")
    started = perf_counter()
    frame.to_parquet(tmp, engine="pyarrow", compression="snappy", index=True)
    stage = perf_counter() - started
    started = perf_counter()
    os.replace(tmp, path)
    publish = perf_counter() - started
    return {"prepare_seconds": 0.0, "stage_write_seconds": stage,
            "publish_seconds": publish, "total_seconds": stage + publish}


def _append_parquet_atomic(path: Path, new_rows: pd.DataFrame) -> dict[str, float]:
    started = perf_counter()
    old = pd.read_parquet(path)
    combined = pd.concat([old, new_rows])
    combined.index = pd.to_datetime(combined.index).normalize()
    combined.index.name = old.index.name or "Date"
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    read_merge = perf_counter() - started
    write = _atomic_parquet_replace(path, combined)
    write["read_merge_seconds"] = read_merge
    write["total_seconds"] += read_merge
    return write


def _benchmark_updates(data_root: Path, parquet_root: Path, output_dir: Path,
                       repeats: int) -> dict[str, Any]:
    from data import update_data
    from data.price_continuity import PRICE_COLS

    source_csv = data_root / "etfs" / "SPY.csv"
    original = pd.read_csv(source_csv, index_col=0, parse_dates=True)
    original.index.name = "Date"
    append_row = original.iloc[[-1]].copy()
    append_row.index = pd.DatetimeIndex([pd.Timestamp(original.index[-1]) + pd.offsets.BDay(1)],
                                       name="Date")
    append_row = append_row[[column for column in PRICE_COLS if column in append_row.columns]]
    append_values = {"csv_append": [], "parquet_append": [],
                     "csv_atomic_full_replace": [], "parquet_atomic_full_replace": []}
    row_checks = []

    for run in range(repeats):
        scratch = output_dir / "update-simulations" / f"run-{run + 1}"
        scratch.mkdir(parents=True, exist_ok=True)
        csv_append_path = scratch / "append.csv"
        parquet_append_path = scratch / "append.parquet"
        csv_replace_path = scratch / "replace.csv"
        parquet_replace_path = scratch / "replace.parquet"
        shutil.copyfile(source_csv, csv_append_path)
        shutil.copyfile(source_csv, csv_replace_path)
        shutil.copyfile(parquet_root / "etfs" / "SPY.parquet", parquet_append_path)
        shutil.copyfile(parquet_root / "etfs" / "SPY.parquet", parquet_replace_path)

        started = perf_counter()
        added = update_data._append(csv_append_path, append_row)
        append_values["csv_append"].append(perf_counter() - started)
        parquet_append_values = _append_parquet_atomic(parquet_append_path, append_row)
        append_values["parquet_append"].append(parquet_append_values)

        csv_replace_frame = original[[c for c in PRICE_COLS if c in original.columns]]
        append_values["csv_atomic_full_replace"].append(
            _atomic_csv_replace(csv_replace_path, csv_replace_frame))
        append_values["parquet_atomic_full_replace"].append(
            _atomic_parquet_replace(parquet_replace_path, original))

        after_csv = pd.read_csv(csv_append_path, index_col=0, parse_dates=True)
        after_parquet = pd.read_parquet(parquet_append_path)
        if added != 1 or len(after_csv) != len(original) + 1 or len(after_parquet) != len(original) + 1:
            raise AssertionError("synthetic append did not add exactly one session")
        if pd.Timestamp(after_csv.index[-1]) != append_row.index[-1] or pd.Timestamp(after_parquet.index[-1]) != append_row.index[-1]:
            raise AssertionError("synthetic append date mismatch")
        row_checks.append(True)

    def summary(runs: list[Any]) -> Any:
        if runs and isinstance(runs[0], dict):
            keys = runs[0]
            return {key: median(float(run[key]) for run in runs) for key in keys}
        return median(runs)

    return {"ticker": "SPY", "synthetic_rows_per_run": 1,
            "repeats": repeats, "append_rows_and_dates_valid": all(row_checks),
            "csv_append_seconds": summary(append_values["csv_append"]),
            "parquet_append_medians_seconds": summary(append_values["parquet_append"]),
            "csv_atomic_full_replacement_medians_seconds": summary(append_values["csv_atomic_full_replace"]),
            "parquet_atomic_full_replacement_medians_seconds": summary(append_values["parquet_atomic_full_replace"]),
            "notes": ["CSV append uses data.update_data._append and rewrites the complete file in place (not atomic).",
                      "Atomic replacement timings include file serialization/staging and os.replace; scratch targets only.",
                      "This measures a single ticker. The current application has no globally atomic multi-file archive publisher."]}


def _benchmark_archive_staging(data_root: Path, parquet_root: Path,
                               output_dir: Path, repeats: int) -> dict[str, Any]:
    """Measure current CSV stage plus an equivalent isolated Parquet copy."""
    from data import price_continuity

    csv_timings, parquet_timings, changed_counts = [], [], []
    with tempfile.TemporaryDirectory(prefix="storage-stage-", dir=output_dir) as temp_name:
        temp_root = Path(temp_name)
        scan_dir = temp_root / "empty-offline-scan"
        scan_dir.mkdir()
        (scan_dir / "scan.json").write_text(json.dumps({"rescaled": []}), encoding="utf-8")
        (scan_dir / "probe.json").write_text(json.dumps({"provider": {}}), encoding="utf-8")

        csv_old = temp_root / "old-csv-generation"
        csv_old.mkdir()
        for sub in ("reference", "stocks", "etfs"):
            shutil.copytree(data_root / sub, csv_old / sub)

        parquet_old = temp_root / "old-parquet-generation"
        parquet_old.mkdir()
        shutil.copytree(data_root / "reference", parquet_old / "reference")
        shutil.copytree(parquet_root / "stocks", parquet_old / "stocks")
        shutil.copytree(parquet_root / "etfs", parquet_old / "etfs")

        for run in range(repeats):
            csv_new = temp_root / f"csv-generation-{run + 1}"
            started = perf_counter()
            result = price_continuity.stage(csv_old, csv_new, scan_dir)
            csv_timings.append(perf_counter() - started)
            changed_counts.append(len(result["changed"]))

            parquet_new = temp_root / f"parquet-generation-{run + 1}"
            started = perf_counter()
            before = price_continuity.manifest(parquet_old)
            shutil.copytree(parquet_old, parquet_new)
            after = price_continuity.manifest(parquet_new)
            if before["files"] != after["files"]:
                raise AssertionError("Parquet staged generation failed hash verification")
            parquet_timings.append(perf_counter() - started)

    return {
        "repeats": repeats,
        "changed_files_in_synthetic_repair": changed_counts,
        "csv_existing_stage_copy_and_manifest_median_seconds": median(csv_timings),
        "parquet_candidate_stage_copy_and_manifest_median_seconds": median(parquet_timings),
        "csv_runs_seconds": csv_timings,
        "parquet_runs_seconds": parquet_timings,
        "parquet_stage_supported_by_application": False,
        "atomic_publication": "Measured separately by single-ticker full-history replacement; current publisher performs atomic os.replace per changed file, not one atomic multi-file generation switch.",
        "notes": [
            "CSV timings call data.price_continuity.stage with an empty offline scan, measuring a verified no-change stage without inventing provider evidence.",
            "Parquet timings measure the same reference files plus a staged Parquet generation and old/new manifests; this is an isolated filesystem comparison, not a production publisher.",
            "All generation copies are temporary under output-dir and are removed after measurement.",
        ],
    }


def benchmark(data_root: Path, output_dir: Path, start: str, end: str,
              repeats: int = 3) -> dict[str, Any]:
    if repeats < 3:
        raise ValueError("issue acceptance requires at least three repetitions")
    data_root, output_dir = data_root.resolve(), output_dir.resolve()
    if not data_root.is_dir():
        raise ValueError(f"data root does not exist: {data_root}")
    if pd.Timestamp(start) > pd.Timestamp(end):
        raise ValueError("start must not exceed end")
    output_dir.mkdir(parents=True, exist_ok=True)
    parquet_root = output_dir / "parquet-store"
    before = source_manifest(data_root)
    conversion = convert_archive(data_root, parquet_root)
    round_trip = _validate_round_trips(data_root, parquet_root)

    sector_strategies = {
        name: _benchmark_sector_strategy(name, data_root, parquet_root, repeats)
        for name in ("SR", "Skew LS")
    }
    with redirect_stdout(io.StringIO()):
        sp500 = _benchmark_sp500(data_root, parquet_root, output_dir, start, end, repeats)
    update = _benchmark_updates(data_root, parquet_root, output_dir, repeats)
    archive_staging = _benchmark_archive_staging(data_root, parquet_root, output_dir, repeats)

    after = source_manifest(data_root)
    unchanged = before == after
    if not unchanged:
        raise RuntimeError("source data changed while benchmark was running; discard results")

    speedups = {}
    for name, results in [("SR", sector_strategies["SR"]),
                          ("Skew LS", sector_strategies["Skew LS"]),
                          ("SP500", sp500)]:
        csv_seconds = (results["csv"]["median_seconds"]["end_to_end_seconds"]
                       if name != "SP500" else results["csv"]["median_seconds"]["total_stage_seconds"])
        parquet_seconds = (results["parquet"]["median_seconds"]["end_to_end_seconds"]
                           if name != "SP500" else results["parquet"]["median_seconds"]["total_stage_seconds"])
        speedups[name] = 1 - parquet_seconds / csv_seconds

    exact = (round_trip["all_exact"] and sector_strategies["SR"]["frames_identical"] and
             sector_strategies["SR"]["strategy_outputs_identical"] and
             sector_strategies["Skew LS"]["frames_identical"] and
             sector_strategies["Skew LS"]["strategy_outputs_identical"] and
             sp500["outputs_identical"])
    hurdle_count = sum(value >= 0.15 for value in speedups.values())
    regressions_ok = all(value >= -0.05 for value in speedups.values())
    go = exact and hurdle_count >= 2 and regressions_ok
    return {
        "schema_version": 1,
        "data_root": str(data_root), "output_dir": str(output_dir),
        "date_range_sp500": {"start": start, "end": end},
        "environment": {"python": platform.python_version(), "pandas": pd.__version__,
                        "pyarrow": pyarrow.__version__, "duckdb_available": importlib.util.find_spec("duckdb") is not None,
                        "platform": platform.platform(), "repeats": repeats},
        "cache_method": "fresh application-level loader per run; OS file cache was not flushed and is uncontrolled",
        "materiality_hurdle": MATERIALITY_HURDLE,
        "dataset": {"files": len(before), "sha256": manifest_digest(before),
                    "per_file_manifest": before, "source_unchanged": unchanged},
        "conversion": conversion,
        "round_trip": round_trip,
        "strategies": {**sector_strategies, "SP500": sp500},
        "end_to_end_speedup_fraction": speedups,
        "update_and_publication": update,
        "archive_staging": archive_staging,
        "decision": {"exact_outputs": exact, "strategies_meeting_15_percent": hurdle_count,
                     "no_regression_over_5_percent": regressions_ok,
                     "go_to_separate_migration_issue": go,
                     "recommendation": "Proceed to a separately scoped migration issue" if go else
                         "No-go for migration now; keep CSV source-of-truth and retain this benchmark for future retesting"},
        "limitations": [
            "OS cache eviction is not performed; results represent warmed operating-system file cache.",
            "SP500 covers the requested one-year interval, while SR and Skew LS run on their full available shared histories.",
            "Conversion includes every stocks/etfs CSV and excludes the Constituents workbook and NPZ cache.",
            "The synthetic daily update appends one copied SPY bar only in scratch files; no provider or network is used.",
            "DuckDB is optional and only reported as available/unavailable; the storage readers compared here are CSV and per-ticker Parquet.",
            "A single-file os.replace is atomic, but there is no existing atomic publication mechanism for an entire multi-ticker generation.",
            "Storage benchmark is not a strategy A/B or a release authorization. Any migration needs its own issue and the required OLD/NEW characterization and recovery checks.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    try:
        report = benchmark(args.data_root, args.output_dir, args.start, args.end, args.repeats)
    except (OSError, ValueError, TypeError, ImportError, AssertionError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    report_path = args.output_dir / "storage-format-benchmark.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"report": str(report_path), "decision": report["decision"],
                      "speedups": report["end_to_end_speedup_fraction"],
                      "source_unchanged": report["dataset"]["source_unchanged"]},
                     ensure_ascii=False, indent=2))
    return 0 if report["dataset"]["source_unchanged"] and report["decision"]["exact_outputs"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
