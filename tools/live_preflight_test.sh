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

SQL=$(sed -e "s/{{PROJECT}}/$PROJECT/g" -e "s/{{DATASET}}/$DS/g" \
          -e "s/{{VIEW_PREFIX}}/v/g" sql/preflight.sql.tmpl)
ROWS=$(bq --project_id="$PROJECT" --location="$LOCATION" query \
  --nouse_legacy_sql --format=json "$SQL")

python3 - "$ROWS" <<'EOF'
import json, sys
rows = json.loads(sys.argv[1] or "[]")
wrong = [r for r in rows if r["problem"] == "WRONG_OBJECT_TYPE"]
if not rows:
    print("FAIL: preflight returned [] for a table-shadowed dataset",
          file=sys.stderr); sys.exit(1)
if len(wrong) < 15:
    print(f"FAIL: expected >=15 WRONG_OBJECT_TYPE rows, got {len(wrong)}",
          file=sys.stderr); sys.exit(1)
print(f"live preflight shadow test OK: {len(wrong)} WRONG_OBJECT_TYPE rows")
EOF
