# Market-history cold-cache follow-up (#96)

The [#93 storage benchmark](data-storage-benchmark.md) established an exact
CSV/Parquet comparison and a no-go recommendation, but its source hashing,
conversion, and round-trip checks warmed the Windows file cache before timing.
This follow-up measures a cold-file-cache proxy separately from an immediately
subsequent warm read. It does not change the live data format or archive.

## Method

Use an isolated worktree and the converted Parquet store from #93. The offline
script checks that the source manifest matches #93, warms the Constituents NPZ
cache, then measures three pairs for each of SR, Skew LS, and SP500 under both
CSV and Parquet. Before each first run, it invokes RAMMap's Empty System Working
Set (`-Es`) and Empty Standby List (`-Et`) actions and records Windows standby
and system-cache resident bytes before and after. The second run follows
without a purge. Both runs create fresh application loaders/selectors. The
format order alternates between repetitions. Every output is checked against
the exact #93 hash; the source manifest is rechecked after all runs.

RAMMap must be obtained separately from [Microsoft Sysinternals](https://learn.microsoft.com/en-us/sysinternals/downloads/rammap)
and run in an elevated Windows session only with operator authorization to
clear the host-wide cache. The tool is not committed to this repository.

```powershell
python data/benchmark_storage_cache.py `
  --data-root C:/QuantCore-master/data `
  --output-dir C:/QuantCore-bench/issue-93-run-20260923 `
  --rammap C:/path/to/RAMMap64.exe `
  --start 2025-01-02 --end 2025-12-31 --repeats 3
```

This is a **cold-cache proxy**, not a guaranteed physical-disk read. RAMMap
can evict Windows cached pages, but hardware caches and other activity on the
shared host remain uncontrolled. The script rejects a purge if more than
512 MiB remains in the normal-priority standby list, rather than labeling
that run cold. Timings exclude the purge itself.
