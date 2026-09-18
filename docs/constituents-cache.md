# Constituents Excel cache

`reference/sp500_constituents_daily.xlsx` remains the authoritative source for
the SP500 universe. `SelectionEngine` can use a disposable, content-addressed
NPZ derivative to avoid reparsing the workbook on every process start.

## Runtime behavior

- The workbook is hashed before every cache lookup. A missing or unreadable
  source raises the source error; an older cache is never used as a fallback.
- Cache filenames include the resolved source identity and SHA-256 digest, so a
  changed workbook cannot reuse an older result.
- A cache hit rechecks the source digest after reading. If the workbook was
  atomically replaced during the read, loading retries and publishes only data
  parsed from one source version.
- Cache writes use a temporary file, `fsync`, and `os.replace`. Readers see
  either the previous complete cache or the new complete cache, never a partial
  file. Concurrent builders may race safely; stale versions are removed after a
  successful publication.
- The default cache directory is `.constituents-cache` beside the workbook.
  Callers such as benchmarks can pass an isolated directory.
- Set `QUANTCORE_DISABLE_CONSTITUENTS_CACHE=1` (also `true`, `yes`, or `on`)
  to force the historical Excel path. A cache write failure logs a warning and
  still returns the exact Excel result.

The serialized frame preserves the historical sheet selection, row order,
sorting, column names, index name, and missing values. Workbooks with a
non-string ticker cell or column name are returned exactly as parsed and are
not cached.

## Validation

The characterization harness captures three accounts across five signal
scenarios and restores the pre-existing state bytes after every run:

```powershell
python data/characterize_constituents.py --mode old --output output/issue51/old-characterization.json
python data/characterize_constituents.py --mode new --output output/issue51/new-characterization.json
```

The issue #51 run produced byte-identical characterization output, including
15 account/scenario entries and 2,949 constituent days. The 2025 backtest
history and trade SHA-256 values also matched between the Excel and cache
paths.

The standalone cache benchmark measures source hashing, Excel parsing, initial
build, cache hit, and source-update rebuild:

```powershell
python -m data.benchmark_constituents_cache `
  --source data/reference/sp500_constituents_daily.xlsx `
  --repeats 3 `
  --output output/issue51/cache-microbenchmark.json
```

On the issue #51 fixture (Windows 11, Python 3.13.2, pandas 2.2.3), the
median Excel parse was 15.867 s, initial cache build 16.812 s, cache hit
0.139 s, and source-update rebuild 16.728 s. All 12 output hashes were
identical. The hit still hashes the authoritative workbook, so the measured
0.139 s includes that integrity check.

The full pipeline benchmark exposes `--constituents-cache on|off` and
`--warm-constituents-cache` for controlled comparisons. The cache is enabled
by default for production `SelectionEngine` calls; the benchmark CLI defaults
to `off` so existing benchmark invocations remain read-only unless caching is
requested explicitly.
