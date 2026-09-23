# Market-history cold-cache follow-up (#96)

The [#93 storage benchmark](data-storage-benchmark.md) established an exact
CSV/Parquet comparison and a no-go recommendation, but its source hashing,
conversion, and round-trip checks warmed the Windows file cache before timing.
This follow-up measures a cold-file-cache proxy separately from an immediately
subsequent warm read. It does not change the live data format or archive.

## Method

Use an isolated worktree and the converted Parquet store from #93. The offline
script checks that the source manifest matches #93, warms the Constituents NPZ
cache, and runs one untimed initialization pass per strategy and format. It
then measures three pairs for each of SR, Skew LS, and SP500 under both CSV and
Parquet. Before each first run, it invokes RAMMap's Empty System Working
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
shared host remain uncontrolled. The script retries a purge up to three times
and rejects the run if more than 512 MiB remains in the normal-priority standby
list, rather than labeling that run cold. Timings exclude the purge itself.

## Measured result (2026-09-23)

The reviewable [result snapshot](data-storage-cold-cache-results.json) contains
all 36 measured strategy runs, their output hashes, and before/after memory
counters for each of the 18 purges. The environment was Windows 11, Python
3.13.2, pandas 2.2.3, and PyArrow 23.0.1. The source manifest was the same
680-file SHA-256 as #93,
`af7a2a028b2f79edeaa8e98ba7d7866ff10a0c7ab01d92ad809dfa1d684e192e`,
and remained unchanged. All strategy input/output hashes matched the #93
baseline. Every purge succeeded on its first attempt; post-purge standby cache
was 4.65–18.17 MiB and system-cache resident memory was 2.70–10.45 MiB.

Each value below is the median of three runs in seconds. The SP500 load column
is **stock history reads**, whereas the SR and Skew LS columns cover their ETF
loaders. The end-to-end column includes strategy execution, but not the purge
or untimed initialization.

| Workflow and cache | CSV load | Parquet load | CSV end-to-end | Parquet end-to-end | Parquet improvement |
|---|---:|---:|---:|---:|---:|
| SR cold proxy | 0.133 | 0.074 | 0.413 | 0.367 | 11.1% |
| SR warm | 0.099 | 0.078 | 0.367 | 0.380 | -3.5% |
| Skew LS cold proxy | 0.210 | 0.147 | 0.236 | 0.175 | 25.6% |
| Skew LS warm | 0.198 | 0.127 | 0.225 | 0.155 | 31.0% |
| SP500 cold proxy | 4.517 | 2.482 | 24.398 | 21.948 | 10.0% |
| SP500 warm | 4.258 | 2.096 | 23.812 | 21.448 | 9.9% |

The #93 migration hurdle required at least a 15% median end-to-end gain in two
of three workflows, with no workflow more than 5% slower and exact outputs.
Only Skew LS cleared the 15% bar under either cache condition. SR's warm
Parquet run was 3.5% slower, inside the allowed regression limit. The
recommendation remains **no-go for a production format migration**; CSV stays
the market-history source of truth.

Absolute execution times were higher than the earlier #93 session, including
for the same CSV code path, so cross-session time comparisons are not used to
decide the format. This shared host may have variable CPU and disk contention;
only within-session, paired format comparisons support the conclusion. The
first diagnostic run without strategy-format initialization had an unusually
slow first SR/Parquet read; it was discarded and repeated with the explicit
untimed initialization described above. The RAMMap purge verifies low Windows
file-cache counters, but cannot prove that hardware-level caches are empty.
