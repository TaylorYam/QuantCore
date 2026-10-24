"""Measure authoritative Excel parsing and derived Constituents cache paths.

一次性／研究用：不在排程或 Panel 裡執行，見 docs/data-roadmap.md（#236）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
from statistics import median
import tempfile
from time import perf_counter
import zipfile

import pandas as pd

from strategies.sp500_momentum.constituents import (
    cache_path_for, load_constituents, parse_excel, source_digest,
)


def frame_hash(frame: pd.DataFrame) -> str:
    payload = frame.to_json(date_format="iso", orient="split").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def timed(callable_):
    started = perf_counter()
    result = callable_()
    return perf_counter() - started, result


def semantic_source_update(path: Path, marker: int) -> None:
    """Change XLSX bytes without changing worksheets or cell values."""
    with zipfile.ZipFile(path, mode="a") as workbook:
        workbook.comment = f"issue-51-rebuild-{marker}".encode("ascii")


def benchmark(source: Path, repeats: int = 3) -> dict:
    if repeats < 3:
        raise ValueError("at least three repeats are required")
    source = source.resolve(strict=True)
    original_hash = source_digest(source)
    measurements = {name: [] for name in
                    ("source_hash", "excel_parse", "initial_build", "cache_hit",
                     "source_update_rebuild")}
    output_hashes = {name: [] for name in measurements if name != "source_hash"}
    cache_sizes = []

    for _ in range(repeats):
        elapsed, digest = timed(lambda: source_digest(source))
        assert digest == original_hash
        measurements["source_hash"].append(elapsed)
        elapsed, frame = timed(lambda: parse_excel(source))
        measurements["excel_parse"].append(elapsed)
        output_hashes["excel_parse"].append(frame_hash(frame))

    for run in range(repeats):
        with tempfile.TemporaryDirectory(prefix="quantcore-constituents-build-") as folder:
            root = Path(folder)
            local_source = root / source.name
            shutil.copy2(source, local_source)
            cache_dir = root / "cache"
            elapsed, frame = timed(lambda: load_constituents(local_source, cache_dir=cache_dir))
            measurements["initial_build"].append(elapsed)
            output_hashes["initial_build"].append(frame_hash(frame))
            built_cache = cache_path_for(local_source, source_digest(local_source), cache_dir)
            cache_sizes.append(built_cache.stat().st_size)

            elapsed, frame = timed(lambda: load_constituents(local_source, cache_dir=cache_dir))
            measurements["cache_hit"].append(elapsed)
            output_hashes["cache_hit"].append(frame_hash(frame))

            semantic_source_update(local_source, run)
            elapsed, frame = timed(lambda: load_constituents(local_source, cache_dir=cache_dir))
            measurements["source_update_rebuild"].append(elapsed)
            output_hashes["source_update_rebuild"].append(frame_hash(frame))

    distinct_outputs = sorted({value for values in output_hashes.values() for value in values})
    return {
        "schema_version": 1,
        "source": str(source),
        "source_sha256": original_hash,
        "repeats": repeats,
        "python": platform.python_version(),
        "pandas": pd.__version__,
        "platform": platform.platform(),
        "seconds": measurements,
        "median_seconds": {name: median(values) for name, values in measurements.items()},
        "cache_bytes": cache_sizes,
        "output_hashes": output_hashes,
        "outputs_identical": len(distinct_outputs) == 1,
        "limitations": [
            "OS file cache is not flushed.",
            "Source-update timing changes only the XLSX ZIP comment, outside worksheet semantics.",
            "Initial build and source-update rebuild include Excel parse plus atomic NPZ publication.",
            "Hash timing is included in every production cache lookup.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = benchmark(args.source, args.repeats)
    except (OSError, ValueError, TypeError, zipfile.BadZipFile) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return int(not result["outputs_identical"])


if __name__ == "__main__":
    raise SystemExit(main())
