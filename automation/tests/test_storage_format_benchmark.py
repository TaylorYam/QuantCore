"""Small offline checks for the research-only storage benchmark helpers."""
from __future__ import annotations

import pandas as pd
import pytest

pytest.importorskip("pyarrow")

from data.benchmark_storage_formats import (
    _append_parquet_atomic,
    _validate_round_trips,
    convert_archive,
    manifest_digest,
    source_manifest,
)


def _write_csv(path, frame):
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.index.name = "Date"
    frame.to_csv(path)


def test_parquet_round_trip_covers_all_local_ticker_frames_without_source_writes(tmp_path):
    data_root = tmp_path / "source"
    dates = pd.to_datetime(["2025-01-02", "2025-01-03"])
    _write_csv(data_root / "stocks" / "AAA.csv",
               pd.DataFrame({"Open": [10.0, 11.0], "Close": [10.5, 11.5],
                             "Volume": [100, 110]}, index=dates))
    _write_csv(data_root / "etfs" / "SPY.csv",
               pd.DataFrame({"Open": [500.0, 501.0], "Close": [502.0, 503.0],
                             "Adj Close": [500.0, 501.0], "Volume": [1_000, 1_100]},
                            index=dates))
    original_manifest = source_manifest(data_root)

    conversion = convert_archive(data_root, tmp_path / "out" / "parquet")
    round_trip = _validate_round_trips(data_root, tmp_path / "out" / "parquet")

    assert conversion["files"] == conversion["parquet_files"] == 2
    assert round_trip["ticker_frames"] == 2
    assert round_trip["all_exact"] is True
    assert source_manifest(data_root) == original_manifest
    assert manifest_digest(original_manifest) == manifest_digest(source_manifest(data_root))


def test_parquet_append_stages_then_atomically_publishes_one_new_session(tmp_path):
    path = tmp_path / "SPY.parquet"
    initial = pd.DataFrame(
        {"Open": [500.0, 501.0], "Close": [502.0, 503.0]},
        index=pd.DatetimeIndex(["2025-01-02", "2025-01-03"], name="Date"),
    )
    initial.to_parquet(path, compression="snappy")
    new = pd.DataFrame(
        {"Open": [504.0], "Close": [505.0]},
        index=pd.DatetimeIndex(["2025-01-06"], name="Date"),
    )

    timing = _append_parquet_atomic(path, new)
    actual = pd.read_parquet(path)

    pd.testing.assert_frame_equal(actual, pd.concat([initial, new]))
    assert timing["total_seconds"] >= timing["publish_seconds"]
    assert not path.with_name(f".{path.name}.bench-tmp").exists()
