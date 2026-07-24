# Repeated-key divergence: direct-view vs pinned-join fan-out (M0 evidence)

Raw measurement: `evidence/divergence-measurement.json`. Scenario
`seed-20260715-30d-100k` (manifest-bound seed, sha `04321db9…`), ADK 1.27.0
fixture, 14-day window (2026-07-02..2026-07-15 inclusive), token-precedence
expression applied identically to both shapes:

| Shape | Rows | Distinct calls | Token sum | Avg latency (ms) |
|---|---:|---:|---:|---:|
| Direct view (`v_llm_response`) — oracle/dashboard | 6,985 | 6,323 | 17,568,787 | 5,922.75 |
| Pinned explore join (BigQuery **simulation** of the explore's self-join on trace/span/event_type — not a live Looker render) | 9,257 | 6,323 | 23,269,595 | 5,569.991 |

The pinned block's explore declares `one_to_one` joins; streaming partial +
final responses share one key and fan out n×n, inflating SUMs and
**reweighting averages toward high-multiplicity keys** (the measured
average decreases here). The v1 dashboard and oracle aggregate the view
directly under the frozen `raw_row` policy — an **intentional, documented
divergence** from block behavior on the repeated-key population;
distinct-count measures are unaffected (6,323 = 6,323). See
docs/decisions/call-row-policy.md.

# Base-table-only undercount (M0 evidence)

Raw measurement: `evidence/undercount-measurement.json`. Full 30-day
scenario, ADK 1.27.0 fixture:

- `agent_events` table rows: **100,001**
- 15-view union rows: **99,738**
- difference: **263** = exactly the scenario manifest's
  `base_table_only_rows` counter, proving the documented Total Events
  undercount category to the row.

# Cross-profile metric equality (M0 evidence)

Machine-readable receipts: `evidence/oracle-compare/*.json` (four:
{base, filtered-f1} × {1.36.1, 2.4.0}). All 37 charts match per receipt —
**148/148 comparisons** — under the enforced-provenance runner: ordered
rows; dimensions lexical; integer measures exact; float measures at the
declared canonical precision (6 dp, comparator policy — oracle SQL stays
LookML-faithful); percentile tolerance 0.0 (relative) as declared. The
filtered scenario exercises the pinned listener matrix (Agent ×2,
User ID ×50, Tool Name ×1) as executable expected results, including the
four User ID listener exceptions and Tool Name's one-chart scope.
