# M0 benchmark evidence — 10M-event reference workload

Raw job statistics: `evidence/benchmark-10m.json`. Dataset `bqaa_bench_10m`
(receipt: `evidence/bqaa_bench_10m.receipt.json`; seed manifest:
`oracle/scenarios/bench-20260715-30d-10m.json`), 20 cold runs per shape,
query cache disabled, no BI Engine reservation, on-demand pricing.

## Gates

| Gate | Result | Verdict |
|---|---|---|
| Structural union (≤3× comparable date-pruned base scan, `total_bytes_processed`, cache off) | union 575,869,599 B vs base 405,475,182 B → **1.4202×** | **PASS** |
| Date boundary (half-open UTC predicate) | union in-window == raw per-day count (9,627,067); end-date rows included (52); day-after rows 0 | **PASS** |
| Partition pruning | 1-day 1,443 B vs 30-day 247,627,785 B (fraction ≈ 1e-5) | **PASS** |

Worst per-shape p95 latency: **1,587 ms** (budget: 10 s/chart). Per-shape
bytes processed (198–682 MB on 10M rows / 30 days) indicate comfortable
headroom against the 2 GB/page-refresh budget, which is formally measured
from Looker-Studio-generated jobs in M4.

The 1.42× union ratio reflects the plugin's default table clustering
(`event_type, agent, user_id`): each union branch prunes on its
`event_type` cluster.
