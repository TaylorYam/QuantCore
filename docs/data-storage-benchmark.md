# Market-history storage format benchmark (#93)

## Decision

Keep the current CSV market archive as the source of truth. This offline test
found exact CSV/Parquet round-trips and a substantial reduction in disk size,
but the Parquet end-to-end speedup reached the pre-set materiality threshold in
only one of the three workflows. Do not open a production migration task from
this evidence. Re-test if workload size or the storage environment changes.

The threshold was set before this run: require at least 15% median end-to-end
improvement in at least two of SP500, SR, and Skew LS, no more than a 5%
regression in any workflow, and exact per-ticker frames and full strategy
outputs. A 30% or larger storage reduction is useful support but is not enough
by itself to justify adding and operating a second format.

## Method

Run the repeatable offline script in an isolated worktree:

```powershell
python data/benchmark_storage_formats.py `
  --data-root C:/QuantCore-master/data `
  --output-dir C:/QuantCore-bench/issue-93 `
  --start 2025-01-02 --end 2025-12-31 --repeats 3
```

The output directory contains the converted Parquet files, scratch update
simulations, and `storage-format-benchmark.json` with every per-file source
hash, every run, and output hashes. No network, data provider, broker, Telegram,
or production write is used. The benchmark checks the source archive manifest
before and after timing. PyArrow must be installed in the isolated benchmark
environment; it is not added to QuantCore's runtime requirements. The script
records DuckDB availability but does not install or benchmark DuckDB.

The reviewable [benchmark result snapshot](data-storage-benchmark-results.json)
contains the 680-file hash manifest, all three-run measurements, and per-strategy
output hashes. Its local input and output directory paths have been removed.

Environment: Windows 11, Python 3.13.2, pandas 2.2.3, PyArrow 23.0.1. DuckDB
was not installed in the active environment, so it could not be evaluated.
Constituents used the existing warm NPZ cache; the workbook
was not migrated or rewritten. Each measured strategy run starts with a fresh
loader/selector cache. The benchmark does not evict the OS file cache; every
source and converted ticker file was also read during the exact round-trip
check, so these results do not represent cold-disk reads. First-loader-run and
later-run timings are included separately in the JSON.

The hashed input manifest contains 680 files: 639 stock CSVs, 25 ETF CSVs, and
16 reference workbooks. The market archive itself contains 664 CSVs. Its
aggregate input-manifest SHA-256 was
`af7a2a028b2f79edeaa8e98ba7d7866ff10a0c7ab01d92ad809dfa1d684e192e` and was
unchanged after the run. The six already-documented SP500 history gaps
(ANSS, DFS, HES, IPG, JNPR, WBA) occurred in both formats; repeated backtest
history and trade hashes remained identical.

## Results

One conversion pass wrote all 664 market CSVs as per-ticker Snappy Parquet in
5.29 seconds. The files occupied 151,434,839 bytes as CSV and 83,273,759
bytes as Parquet, a 45.0% reduction. All 664 loaded DataFrames were exactly
equal after conversion; their combined frame hash was
`df4d5501ccebeead033a2270f7b4b9fae886f917876cd31ff75f6085ebdb1ee3`.

| Workflow | CSV load | Parquet load | CSV end-to-end | Parquet end-to-end | Parquet improvement |
|---|---:|---:|---:|---:|---:|
| SR, full available history | 0.076 s | 0.047 s | 0.257 s | 0.225 s | 12.3% |
| Skew LS, full shared history | 0.159 s | 0.096 s | 0.183 s | 0.118 s | 35.3% |
| SP500, 2025-01-02–2025-12-31 | 3.028 s | 1.717 s | 17.248 s | 16.401 s | 4.9% |

Each number is the median of three runs. The SP500 stock reads were 43.3%
faster, but the backtest dominates the one-year workflow, so the end-to-end
gain was below the threshold. All three workflows had identical loaded-frame
hashes and identical full output hashes between formats, and no workflow was
more than 5% slower. Only Skew LS reached the 15% hurdle.

An isolated one-row SPY update was run three times on scratch copies. The
existing CSV `_append` path took a median 0.0277 s and rewrites the file
in-place. The candidate Parquet read/merge/stage/replace path took 0.0121 s.
For a full-history single-file replacement, the current CSV serializer plus
atomic `os.replace` took 0.0152 s; the candidate Parquet serialization/stage
plus `os.replace` took 0.0050 s. The atomic replace portion was about 0.0003 s
for either format. These are single-ticker timings, not a claim about the whole
daily provider update.

For stage costs, the current CSV repair stage copied the reference, stock, and
ETF directories and verified their manifests in a median 5.03 s. An isolated
Parquet generation copy with the same reference files and old/new manifest
checks took 3.18 s. This used an empty saved scan, so no provider correction
was invented and zero files were changed. QuantCore has no Parquet-aware
stage/apply path today. Existing publication is atomic per changed file; it
does not switch an entire multi-ticker generation atomically.

## Recommendation and limits

The go/no-go result is **no-go for migration**: correctness passed, there was no
material regression, but only one strategy met the 15% speed threshold.
Maintaining the current CSV source-of-truth avoids building a second format's
daily update, staging, repair, recovery, and publication path for a measured
workload that does not clear the agreed overall benefit bar. Constituents
should remain Excel source plus its existing NPZ derivative cache; this test
does not provide evidence for a SQL migration.

SR and Skew LS were timed over their full shared historical inputs, but SP500
used one requested one-year range. Results were collected on one Windows host
with a warm/uncontrolled OS cache. The update simulation used one copied SPY
bar and no provider download. DuckDB could not be evaluated because it was not
installed. These results compare storage and I/O behavior only; they do not
claim strategy performance improvement. Any future migration requires its own
Issue and the project-mandated 3 Accounts × 5 scenarios OLD/NEW byte comparison,
state backup/restore, and any required backtest A/B.

Validation on this branch: `python -m pytest automation/tests/ -v` — 655 passed,
20 skipped.

The [cold-cache follow-up (#96)](data-storage-cold-cache.md) later measured
explicitly purged and immediately warm reads for all three workflows. Its
exactness checks passed and its no-go recommendation remained unchanged.
