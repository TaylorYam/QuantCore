"""Derived Constituents cache: exact output, invalidation and failure handling."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from strategies.sp500_momentum import constituents


def write_workbook(path: Path, rows=None, *, numeric=False):
    rows = rows or [("2025-01-02", "AAPL", "MSFT"),
                    ("2025-01-03", "NVDA", None)]
    if numeric:
        rows = [(rows[0][0], rows[0][1], 123), *rows[1:]]
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows, columns=["Date", "T1", "T2"], dtype=object)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame({"ignored": [1]}).to_excel(writer, sheet_name="Summary", index=False)
        frame.to_excel(writer, sheet_name="2025", index=False)


def assert_same(left, right):
    pd.testing.assert_frame_equal(left, right, check_dtype=True, check_exact=True)
    assert [list(left.loc[d].dropna()) for d in left.index] == [
        list(right.loc[d].dropna()) for d in right.index]


def test_build_and_hit_are_exact(tmp_path, monkeypatch):
    source = tmp_path / "reference" / "constituents.xlsx"
    cache_dir = tmp_path / "cache"
    write_workbook(source)
    expected = constituents.parse_excel(source)
    built = constituents.load_constituents(source, cache_dir=cache_dir)
    assert_same(built, expected)
    cache_files = list(cache_dir.glob("*.npz"))
    assert len(cache_files) == 1
    monkeypatch.setattr(constituents, "parse_excel",
                        lambda path: (_ for _ in ()).throw(AssertionError("cache miss")))
    assert_same(constituents.load_constituents(source, cache_dir=cache_dir), expected)


@pytest.mark.parametrize("index_unit", ["us", "ns"])
@pytest.mark.parametrize("column_dtype", ["object", "string"])
def test_cache_preserves_index_precision_and_column_dtype(
        tmp_path, index_unit, column_dtype):
    source = tmp_path / "constituents.xlsx"
    source.write_bytes(b"source identity only")
    index = pd.DatetimeIndex(
        np.asarray(["2025-01-02", "2025-01-03"], dtype=f"datetime64[{index_unit}]"),
        name="Date",
    )
    frame = pd.DataFrame(
        {"T1": pd.Series(["AAPL", None], dtype=column_dtype).array},
        index=index,
    )
    digest = constituents.source_digest(source)
    arrays = constituents._cache_arrays(frame, source, digest)
    assert arrays is not None
    cache_path = constituents.cache_path_for(source, digest, tmp_path / "cache")
    constituents._write_cache(cache_path, arrays)
    assert_same(constituents._read_cache(cache_path, source, digest), frame)


def test_source_atomic_replace_invalidates_cache(tmp_path):
    source = tmp_path / "reference" / "constituents.xlsx"
    cache_dir = tmp_path / "cache"
    write_workbook(source)
    first = constituents.load_constituents(source, cache_dir=cache_dir)
    replacement = source.with_suffix(".replacement.xlsx")
    write_workbook(replacement, [("2025-01-02", "AAPL", "META")])
    os.replace(replacement, source)
    second = constituents.load_constituents(source, cache_dir=cache_dir)
    assert first.loc[pd.Timestamp("2025-01-02"), "T2"] == "MSFT"
    assert second.loc[pd.Timestamp("2025-01-02"), "T2"] == "META"
    assert len(list(cache_dir.glob("*.npz"))) == 1


def test_atomic_replace_during_cache_hit_retries(tmp_path, monkeypatch):
    source = tmp_path / "reference" / "constituents.xlsx"
    cache_dir = tmp_path / "cache"
    write_workbook(source, [("2025-01-02", "AAPL", None)])
    constituents.load_constituents(source, cache_dir=cache_dir)
    replacement = source.with_suffix(".replacement.xlsx")
    write_workbook(replacement, [("2025-01-02", "META", None)])
    real_read = constituents._read_cache
    changed = False

    def replace_after_read(*args):
        nonlocal changed
        frame = real_read(*args)
        if not changed:
            os.replace(replacement, source)
            changed = True
        return frame

    monkeypatch.setattr(constituents, "_read_cache", replace_after_read)
    result = constituents.load_constituents(source, cache_dir=cache_dir)
    assert result.iloc[0, 0] == "META"


def test_schema_change_invalidates_and_preserves_new_columns(tmp_path):
    source = tmp_path / "reference" / "constituents.xlsx"
    cache_dir = tmp_path / "cache"
    write_workbook(source)
    constituents.load_constituents(source, cache_dir=cache_dir)
    replacement = source.with_suffix(".replacement.xlsx")
    with pd.ExcelWriter(replacement, engine="openpyxl") as writer:
        pd.DataFrame({"Date": ["2025-01-02"], "T1": ["AAPL"],
                      "T2": ["MSFT"], "T3": ["META"]}).to_excel(
                          writer, sheet_name="2025", index=False)
    os.replace(replacement, source)
    result = constituents.load_constituents(source, cache_dir=cache_dir)
    assert list(result.columns) == ["T1", "T2", "T3"]
    assert list(result.iloc[0]) == ["AAPL", "MSFT", "META"]


@pytest.mark.parametrize("damage", ["truncated", "version"])
def test_corrupt_or_incompatible_cache_rebuilds(tmp_path, damage):
    source = tmp_path / "reference" / "constituents.xlsx"
    cache_dir = tmp_path / "cache"
    write_workbook(source)
    expected = constituents.load_constituents(source, cache_dir=cache_dir)
    cache_path = next(cache_dir.glob("*.npz"))
    if damage == "truncated":
        cache_path.write_bytes(b"broken")
    else:
        with np.load(cache_path, allow_pickle=False) as loaded:
            arrays = {name: loaded[name] for name in loaded.files}
        arrays["format_version"] = np.asarray([999], dtype=np.int64)
        with cache_path.open("wb") as target:
            np.savez_compressed(target, **arrays)
    assert_same(constituents.load_constituents(source, cache_dir=cache_dir), expected)
    with np.load(cache_path, allow_pickle=False) as repaired:
        assert repaired["format_version"].tolist() == [constituents.CACHE_FORMAT_VERSION]


def test_different_data_roots_do_not_share_cache(tmp_path):
    cache_dir = tmp_path / "shared-cache"
    one = tmp_path / "one" / "constituents.xlsx"
    two = tmp_path / "two" / "constituents.xlsx"
    write_workbook(one, [("2025-01-02", "AAPL", None)])
    write_workbook(two, [("2025-01-02", "META", None)])
    assert constituents.load_constituents(one, cache_dir=cache_dir).iloc[0, 0] == "AAPL"
    assert constituents.load_constituents(two, cache_dir=cache_dir).iloc[0, 0] == "META"
    assert len(list(cache_dir.glob("*.npz"))) == 2


def test_different_parser_versions_do_not_share_cache(tmp_path, monkeypatch):
    source = tmp_path / "constituents.xlsx"
    source.write_bytes(b"same workbook bytes")
    digest = constituents.source_digest(source)
    current = constituents.cache_path_for(source, digest, tmp_path / "cache")
    monkeypatch.setattr(constituents.pd, "__version__", "future-parser-version")
    future = constituents.cache_path_for(source, digest, tmp_path / "cache")
    assert current != future


def test_missing_source_never_uses_stale_cache(tmp_path):
    source = tmp_path / "reference" / "constituents.xlsx"
    cache_dir = tmp_path / "cache"
    write_workbook(source)
    constituents.load_constituents(source, cache_dir=cache_dir)
    source.unlink()
    with pytest.raises(FileNotFoundError):
        constituents.load_constituents(source, cache_dir=cache_dir)


def test_non_string_schema_is_exact_but_not_cached(tmp_path):
    source = tmp_path / "reference" / "constituents.xlsx"
    cache_dir = tmp_path / "cache"
    write_workbook(source, numeric=True)
    expected = constituents.parse_excel(source)
    actual = constituents.load_constituents(source, cache_dir=cache_dir)
    assert_same(actual, expected)
    assert list(cache_dir.glob("*.npz")) == []


def test_unwritable_cache_location_falls_back_to_excel(tmp_path):
    source = tmp_path / "reference" / "constituents.xlsx"
    impossible = tmp_path / "not-a-directory"
    write_workbook(source)
    impossible.write_text("occupied", encoding="utf-8")
    expected = constituents.parse_excel(source)
    assert_same(constituents.load_constituents(source, cache_dir=impossible), expected)


def test_environment_can_disable_cache(tmp_path, monkeypatch):
    source = tmp_path / "reference" / "constituents.xlsx"
    cache_dir = tmp_path / "cache"
    write_workbook(source)
    monkeypatch.setenv(constituents.DISABLE_CACHE_ENV, "1")
    expected = constituents.parse_excel(source)
    assert_same(constituents.load_constituents(source, cache_dir=cache_dir), expected)
    assert not cache_dir.exists()


def test_concurrent_builders_publish_only_complete_cache(tmp_path):
    source = tmp_path / "reference" / "constituents.xlsx"
    cache_dir = tmp_path / "cache"
    write_workbook(source)
    expected = constituents.parse_excel(source)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(
            lambda _: constituents.load_constituents(source, cache_dir=cache_dir), range(6)))
    for result in results:
        assert_same(result, expected)
    assert len(list(cache_dir.glob("*.npz"))) == 1
    assert list(cache_dir.glob("*.tmp")) == []


def test_partial_temp_file_is_never_a_cache_hit(tmp_path):
    source = tmp_path / "reference" / "constituents.xlsx"
    cache_dir = tmp_path / "cache"
    write_workbook(source)
    cache_dir.mkdir()
    (cache_dir / ".constituents.partial.tmp").write_bytes(b"partial")
    expected = constituents.parse_excel(source)
    assert_same(constituents.load_constituents(source, cache_dir=cache_dir), expected)
    assert len(list(cache_dir.glob("*.npz"))) == 1


def test_daily_fixture_output_is_identical(tmp_path):
    source = tmp_path / "reference" / "constituents.xlsx"
    rows = [("2024-12-31", "A", "B"), ("2025-01-02", "B", "C"),
            ("2025-01-03", "C", None)]
    write_workbook(source, rows)
    old = constituents.parse_excel(source)
    new = constituents.load_constituents(source, cache_dir=tmp_path / "cache")
    assert_same(new, old)
    for day in old.index:
        assert list(new.loc[day].dropna()) == list(old.loc[day].dropna())


def test_benchmark_covers_build_hit_and_rebuild(tmp_path):
    from data.benchmark_constituents_cache import benchmark
    source = tmp_path / "reference" / "constituents.xlsx"
    write_workbook(source)
    result = benchmark(source, repeats=3)
    assert result["outputs_identical"]
    assert all(len(result["seconds"][name]) == 3 for name in result["seconds"])
    assert all(size > 0 for size in result["cache_bytes"])
