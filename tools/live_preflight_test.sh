#!/usr/bin/env bash
# LIVE regression test for the preflight object-type check (PR #1 review):
# a dataset containing base TABLES that shadow the generated-view names must
# FAIL preflight with WRONG_OBJECT_TYPE — never pass as compatible.
#
# Usage: tools/live_preflight_test.sh <project> <location>
set -euo pipefail
PROJECT="$1"; LOCATION="${2:-US}"
DS="bqaa_preflight_shadow_$(date +%s)"
trap 'bq --project_id="$PROJECT" rm -r -f -d "$DS" >/dev/null 2>&1 || true' EXIT

bq --project_id="$PROJECT" --location="$LOCATION" mk -d "$DS" >/dev/null

for v in user_message_received llm_request llm_response llm_error \
         tool_starting tool_completed tool_error agent_starting \
         agent_completed invocation_starting invocation_completed \
         state_delta hitl_credential_request hitl_confirmation_request \
         hitl_input_request; do
  bq --project_id="$PROJECT" --location="$LOCATION" query --nouse_legacy_sql \
    "CREATE TABLE \`$PROJECT.$DS.v_$v\` (
       timestamp TIMESTAMP, event_type STRING, agent STRING,
       session_id STRING, invocation_id STRING, user_id STRING,
       trace_id STRING, span_id STRING, parent_span_id STRING,
       status STRING, error_message STRING, is_truncated BOOL,
       usage_metadata JSON, usage_prompt_tokens INT64,
       usage_completion_tokens INT64, usage_total_tokens INT64,
       total_ms INT64, ttft_ms INT64, model_version STRING,
       tool_name STRING, tool_origin STRING)" >/dev/null
done

# The rendered SQL begins with `--` comment lines, which `bq` would parse
# as command-line flags if passed positionally — feed it via stdin instead.
# --max_rows must exceed the full problem-row count (each shadowed view
# yields 12-19 rows); the bq default of 100 silently truncates and would
# let a partial detection pass.
ROWS=$(sed -e "s/{{PROJECT}}/$PROJECT/g" -e "s/{{DATASET}}/$DS/g" \
           -e "s/{{VIEW_PREFIX}}/v/g" sql/preflight.sql.tmpl \
  | bq --project_id="$PROJECT" --location="$LOCATION" query \
      --nouse_legacy_sql --format=json --max_rows=10000)

python3 - "$ROWS" <<'EOF'
import json, sys
EXPECTED = {f"v_{s}" for s in [
    "user_message_received", "llm_request", "llm_response", "llm_error",
    "tool_starting", "tool_completed", "tool_error", "agent_starting",
    "agent_completed", "invocation_starting", "invocation_completed",
    "state_delta", "hitl_credential_request", "hitl_confirmation_request",
    "hitl_input_request"]}
rows = json.loads(sys.argv[1] or "[]")
if not rows:
    print("FAIL: preflight returned [] for a table-shadowed dataset",
          file=sys.stderr); sys.exit(1)
wrong = [r for r in rows if r["problem"] == "WRONG_OBJECT_TYPE"]
seen = {r["view_name"] for r in wrong}
if seen != EXPECTED:
    print(f"FAIL: WRONG_OBJECT_TYPE views != expected 15; "
          f"missing={sorted(EXPECTED - seen)} extra={sorted(seen - EXPECTED)}",
          file=sys.stderr); sys.exit(1)
bad_type = {r["view_name"] for r in wrong
            if r.get("observed_object_type") != "BASE TABLE"}
if bad_type:
    print(f"FAIL: observed_object_type not BASE TABLE for {sorted(bad_type)}",
          file=sys.stderr); sys.exit(1)
print(f"live preflight shadow test OK: all 15 views reported "
      f"WRONG_OBJECT_TYPE as BASE TABLE ({len(wrong)} problem rows)")
EOF
