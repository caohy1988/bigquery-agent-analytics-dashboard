# Repeated-key divergence: direct-view vs pinned-join fan-out (M0 evidence)

Scenario `seed-20260715-30d-100k`, ADK 1.27.0 fixture, 14-day window
(2026-07-02 .. 2026-07-15 inclusive), token-precedence expression applied
identically to both shapes:

| Shape | Rows | Distinct calls | Token sum | Avg latency (ms) |
|---|---:|---:|---:|---:|
| Direct view (`v_llm_response`) — oracle/dashboard | 6,994 | 6,314 | 17,425,015 | 5,876.431 |
| Pinned explore join (self-join on trace/span/event_type) | 9,280 | 6,314 | 23,073,041 | 5,486.023 |

The pinned block's explore declares `one_to_one` joins on
`trace_id, span_id, event_type`; streaming partial + final responses share
one key and fan out n×n in a live Looker render, inflating SUM/AVG measures.
The v1 dashboard and oracle aggregate the view directly under the frozen
`raw_row` policy — an **intentional, documented divergence** from block
behavior on the repeated-key population (distinct-count measures are
unaffected: 6,314 = 6,314). See docs/decisions/call-row-policy.md.

# Base-table-only undercount (M0 evidence)

Full 30-day scenario, ADK 1.27.0 fixture:

- `agent_events` table rows: **100,002**
- 15-view union rows: **99,729**
- difference: **273** = exactly the seed's `base_table_only_rows` counter
  (`HITL_CREDENTIAL_COMPLETED` + `HITL_CONFIRMATION_COMPLETED`), proving the
  documented Total Events undercount category to the row.

# Cross-profile metric equality (M0 evidence)

Expected results recorded from the 1.27.0 fixture; `oracle/runner.py
compare` reports **all 37 charts match** on both `bqaa_fixture_adk_1_36_1`
and `bqaa_fixture_adk_2_4_0` — exact for counts/distinct counts/sums,
IEEE-ordering epsilon (1e-9 relative, documented in the runner) for float
aggregates, declared tolerance 0.0 for percentiles.
