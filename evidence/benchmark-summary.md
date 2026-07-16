# M0 benchmark evidence — 10M-event reference workload

Dataset `bqaa_bench_10m` (10,000,007 rows, 2026-06-15..2026-07-15, DAY
partitioning on `timestamp`, clustered `event_type, agent, user_id` by the
ADK 1.27.0 plugin). 20 cold runs per shape, query cache disabled, no BI
Engine reservation, on-demand pricing. Raw job statistics:
`evidence/benchmark-10m.json`.

## Representative shapes (30-day window)

| Shape | Bytes processed | p50 | p95 |
|---|---:|---:|---:|
| Total Events scorecard (15-view union) | 247.6 MB | 318 ms | 514 ms |
| Distinct sessions scorecard (union) | 431.0 MB | 481 ms | 593 ms |
| Token trend by day (llm view) | 221.8 MB | 368 ms | 517 ms |
| Top-5 users by traces (union, grouped) | 682.0 MB | 590 ms | 695 ms |
| P90 tool latency scorecard | 198.1 MB | 1,336 ms | 2,498 ms |
| PoP token scorecard (two windows) | 229.7 MB | 305 ms | 422 ms |

Worst-case p95 (2.5 s) is 4x inside the 10 s/chart budget; per-shape bytes
suggest comfortable headroom against the 2 GB/page-refresh budget (formally
measured from Looker-Studio-generated jobs in M4).

## Gates

| Gate | Result | Verdict |
|---|---|---|
| Structural union (≤3x comparable date-pruned base scan, `total_bytes_processed`, cache off) | union 575,783,234 B vs base 405,410,880 B → **1.4202x** | **PASS** |
| Date boundary (half-open UTC predicate) | union in-window rows == raw per-day count (9,625,118); end-date rows included (51); day-after rows 0 | **PASS** |
| Partition pruning | 1-day window 1,426 B vs 30-day 247.6 MB (fraction ≈ 0.00001) | **PASS** |

The 1.42x union ratio confirms the clustering-based branch pruning
(`event_type` is the first clustering key) observed at review time on a
smaller dataset; the extreme pruning fraction reflects partition elimination
plus cluster pruning on the single-day probe.
