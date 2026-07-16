# Decision: call_row_policy = raw_row (frozen, M0)

## Question

Multiple `agent_events` rows can share the count-distinct key
`trace_id|span_id`: ADK logs streaming **partial** `LLM_RESPONSE` events
without popping the span, then the final response on the same span. How must
token sums, latency aggregates, and call counts treat repeated keys?

## Evidence

ADK 1.27.0 `bigquery_agent_analytics_plugin.py` L3074–3125 (`after_model_callback`):

- partial chunks (`llm_response.partial`) are logged as `LLM_RESPONSE`
  **without** popping the span — same `span_id` as the final row;
- each partial row carries `usage_metadata=llm_response.usage_metadata` and
  a computed `latency_ms` duration;
- only the final row pops the span.

The pinned Looker block (`fe6423c`) builds `v_llm_response` as a raw
derived table over all `LLM_RESPONSE` rows. Its measures:

- `total_llm_calls` / `total_tool_usage` / `total_tool_errors`:
  `count_distinct` on `CONCAT(trace_id,'|',span_id)` — repeated keys
  collapse;
- `total_tokens_consumed`, `total_prompt_tokens`, `total_completion_tokens`:
  `SUM` over **all rows**, partials included;
- `average_llm_latency` and the percentile measures: over **all rows**.

## Decision

**`raw_row`**: aggregate exactly as the block does — raw-row SUM/AVG/
percentiles plus distinct-key call counts. This is the only policy that can
satisfy the M4 exact-equality parity gate, because it reproduces block
behavior by construction, including the block's own double-counting when
partial chunks carry token counts.

`terminal_row_per_key` (dedup to the span's final row) is arguably more
correct but diverges from the pinned block; it is tracked as a v1.1
improvement candidate alongside the Tool Name filter-scope fix, to be
offered upstream to the block simultaneously so the surfaces stay in
lockstep.

## Consequences

- The union template and the oracle both aggregate raw rows; oracle call
  counts use distinct keys.
- Seed fixtures include streaming partial+final rows sharing one span
  (`streaming_partial_rows` counter) so M4 proves the policy's behavior,
  not just the happy path.
- The documented limitation stands: parity is defined as matching the
  block, not as deduplicated "true" call metrics.
