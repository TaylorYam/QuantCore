"""Measure cold-proxy and warm market-history reads after an explicit Windows cache purge.

Run only on a machine where an operator has approved a host-wide cache purge.
The source archive is read-only; the existing Parquet conversion and report must
already exist below --output-dir. RAMMap is not bundled with this repository.
"""
from __future__ import annotations

import argparse
import ctypes
import io
import json
import platform
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from statistics import median
from typing import Any
from unittest.mock import patch

import pandas as pd

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from data import benchmark_pipeline
from data import benchmark_storage_formats as storage


def _memory_counters() -> dict[str, int]:
    command = (
        "$s=(Get-Counter '\\Memory\\Standby Cache Normal Priority Bytes',"
        "'\\Memory\\System Cache Resident Bytes').CounterSamples;"
        "[Console]::WriteLine([int64]$s[0].CookedValue);"
        "[Console]::WriteLine([int64]$s[1].CookedValue)"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        check=True, capture_output=True, text=True, timeout=30,
    )
    values = [int(line) for line in result.stdout.splitlines() if line.strip()]
    if len(values) != 2:
        raise RuntimeError(f"unexpected Windows memory counters: {result.stdout!r}")
    return {"standby_bytes": values[0], "system_cache_resident_bytes": values[1]}


def _purge_cache(rammap: Path) -> dict[str, Any]:
    before = _memory_counters()
    # RAMMap accepts one Empty action per invocation. Trim active system cache,
    # then discard standby pages; both operations affect the entire host.
    after_attempts = []
    for _ in range(3):
        for action in ("-Es", "-Et"):
            result = subprocess.run([str(rammap), action], timeout=60, check=False)
            if result.returncode:
                raise RuntimeError(f"RAMMap {action} failed with exit code {result.returncode}")
        after = _memory_counters()
        after_attempts.append(after)
        if after["standby_bytes"] <= 512 * 1024 * 1024:
            return {"before": before, "after": after,
                    "after_attempts": after_attempts,
                    "rammap_actions": ["-Es", "-Et"]}
    raise RuntimeError(f"standby list still exceeds 512 MiB after three purges: {after_attempts}")


def _sp500_once(data_root: Path, parquet_root: Path, cache_dir: Path,
                start: str, end: str, storage_format: str) -> dict[str, Any]:
    from strategies.sp500_momentum import utils

    def reader(path: str | Path) -> pd.DataFrame | None:
        try:
            frame = storage.read_market(Path(path), storage_format,
                                        parquet_root if storage_format == "parquet" else None)
            return frame if not frame.empty and "Close" in frame.columns else None
        except (OSError, ValueError, KeyError, ImportError):
            return None

    with patch.object(utils, "load_stock", reader), redirect_stdout(io.StringIO()):
        return benchmark_pipeline.measure_once(
            data_root, start, end, constituents_cache=True,
            constituents_cache_dir=cache_dir,
        )


def _once(strategy: str, storage_format: str, data_root: Path,
          parquet_root: Path, cache_dir: Path, start: str, end: str) -> dict[str, Any]:
    if strategy == "SP500":
        return _sp500_once(data_root, parquet_root, cache_dir, start, end, storage_format)
    return storage._strategy_once(strategy, storage_format, data_root,
                                  parquet_root if storage_format == "parquet" else None)


def _summarize(runs: list[dict[str, Any]], strategy: str) -> dict[str, float]:
    keys = ("stock_csv_seconds", "total_stage_seconds") if strategy == "SP500" else (
        "load_seconds", "end_to_end_seconds")
    return {key: median(run["measurement"][key] for run in runs) for key in keys}


def benchmark(data_root: Path, output_dir: Path, rammap: Path, start: str, end: str,
              repeats: int = 3) -> dict[str, Any]:
    if sys.platform != "win32" or not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("an elevated Windows session is required to purge the OS cache")
    if repeats < 3:
        raise ValueError("issue acceptance requires at least three repetitions")
    data_root, output_dir, rammap = data_root.resolve(), output_dir.resolve(), rammap.resolve()
    if not rammap.is_file():
        raise ValueError(f"RAMMap executable not found: {rammap}")
    original = json.loads((output_dir / "storage-format-benchmark.json").read_text(encoding="utf-8"))
    before = storage.source_manifest(data_root)
    if storage.manifest_digest(before) != original["dataset"]["sha256"]:
        raise RuntimeError("source manifest differs from the original benchmark")
    parquet_root = output_dir / "parquet-store"
    for item in before:
        path = Path(item["file"])
        if path.suffix == ".csv" and not (parquet_root / path.parent / f"{path.stem}.parquet").is_file():
            raise RuntimeError(f"converted file missing for {path}")

    # Import strategy modules and build the Constituents NPZ before purging.
    from strategies.sector_rotation import backtest as _sr_backtest  # noqa: F401
    from strategies.sector_skew_ls import backtest as _skew_backtest  # noqa: F401
    from strategies.sp500_momentum import config
    from strategies.sp500_momentum.selection import SelectionEngine
    cache_dir = output_dir / "constituents-cache"
    with patch.object(config, "REF_DIR", data_root / "reference"), redirect_stdout(io.StringIO()):
        SelectionEngine(use_constituents_cache=True, constituents_cache_dir=cache_dir)
    # Exclude one-time pandas/PyArrow and strategy initialization from the
    # measured cold run. The subsequent RAMMap purge evicts warmed file pages.
    for strategy in ("SR", "Skew LS", "SP500"):
        for storage_format in ("csv", "parquet"):
            _once(strategy, storage_format, data_root, parquet_root,
                  cache_dir, start, end)

    report: dict[str, Any] = {
        "schema_version": 1,
        "environment": {"platform": platform.platform(),
                        "python": platform.python_version(),
                        "pandas": pd.__version__, "pyarrow": storage.pyarrow.__version__,
                        "repeats": repeats},
        "date_range_sp500": {"start": start, "end": end},
        "source_manifest_sha256": storage.manifest_digest(before),
        "rammap_sha256": storage._sha256_file(rammap),
        "method": "One untimed warmup per strategy and format, then RAMMap -Es and -Et before each cold-proxy run; an immediate second run with no purge is warm. Fresh application loaders/selectors for both. OS standby and system-cache resident counters are recorded before/after purge.",
        "limitations": [
            "An OS standby-list purge is a cold-file-cache proxy; hardware controller caches and concurrent host activity are uncontrolled.",
            "RAMMap cache actions affect the entire Windows host, including other applications.",
        ],
        "strategies": {},
    }
    partial_path = output_dir / "storage-cache-benchmark.partial.json"
    for strategy in ("SR", "Skew LS", "SP500"):
        report["strategies"][strategy] = {"csv": {"cold": [], "warm": []},
                                           "parquet": {"cold": [], "warm": []}}
        for repeat in range(repeats):
            order = ("csv", "parquet") if repeat % 2 == 0 else ("parquet", "csv")
            for storage_format in order:
                cache = _purge_cache(rammap)
                cold = _once(strategy, storage_format, data_root, parquet_root,
                             cache_dir, start, end)
                warm = _once(strategy, storage_format, data_root, parquet_root,
                             cache_dir, start, end)
                group = report["strategies"][strategy][storage_format]
                group["cold"].append({"repeat": repeat + 1, "cache": cache,
                                      "measurement": cold})
                group["warm"].append({"repeat": repeat + 1, "measurement": warm})
                partial_path.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                                   allow_nan=False), encoding="utf-8")
        for storage_format in ("csv", "parquet"):
            group = report["strategies"][strategy][storage_format]
            for mode in ("cold", "warm"):
                group[f"{mode}_median_seconds"] = _summarize(group[mode], strategy)
            expected = original["strategies"][strategy][storage_format]
            for mode in ("cold", "warm"):
                for run in group[mode]:
                    observed = run["measurement"]
                    if strategy == "SP500":
                        reference = expected["runs"][0]
                        keys = ("history_sha256", "trades_sha256")
                    else:
                        reference = expected
                        keys = ("loaded_hashes", "output_hashes")
                    if any(observed[key] != reference[key] for key in keys):
                        raise RuntimeError(f"{strategy} {storage_format} {mode} output changed")

    after = storage.source_manifest(data_root)
    if before != after:
        raise RuntimeError("source archive changed during cold/warm benchmark")
    report["source_unchanged"] = True
    for strategy in report["strategies"]:
        results = report["strategies"][strategy]
        key = "total_stage_seconds" if strategy == "SP500" else "end_to_end_seconds"
        results["end_to_end_speedup_fraction"] = {
            mode: 1 - results["parquet"][f"{mode}_median_seconds"][key] /
            results["csv"][f"{mode}_median_seconds"][key]
            for mode in ("cold", "warm")
        }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--rammap", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    try:
        result = benchmark(args.data_root, args.output_dir, args.rammap,
                           args.start, args.end, args.repeats)
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.TimeoutExpired,
            subprocess.CalledProcessError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    report_path = args.output_dir / "storage-cache-benchmark.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
                           encoding="utf-8")
    report_path.with_name("storage-cache-benchmark.partial.json").unlink(missing_ok=True)
    print(json.dumps({"report": str(report_path), "source_unchanged": True,
                      "speedups": {name: item["end_to_end_speedup_fraction"]
                                   for name, item in result["strategies"].items()}},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
