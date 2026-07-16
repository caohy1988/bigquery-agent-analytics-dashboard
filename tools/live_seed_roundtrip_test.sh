#!/usr/bin/env bash
# LIVE test (requires authenticated `bq` CLI + a billing project).
# Loads a small seed into a throwaway dataset with the ADK agent_events
# schema and asserts the JSON payload columns arrive as OBJECTS (the
# double-encoding regression from PR #1 review) and that generated-view
# extraction paths are readable.
#
# Usage: tools/live_seed_roundtrip_test.sh <project> <location>
set -euo pipefail
PROJECT="$1"; LOCATION="${2:-US}"
DS="bqaa_seed_rt_$(date +%s)"
trap 'bq --project_id="$PROJECT" rm -r -f -d "$DS" >/dev/null 2>&1 || true' EXIT

bq --project_id="$PROJECT" --location="$LOCATION" mk -d "$DS" >/dev/null

python3 tools/seed_events.py --events 2000 --days 30 --seed 42 \
  --out /tmp/seed-rt.ndjson >/dev/null

bq --project_id="$PROJECT" --location="$LOCATION" load \
  --source_format=NEWLINE_DELIMITED_JSON \
  "$DS.agent_events" /tmp/seed-rt.ndjson \
  timestamp:TIMESTAMP,event_type:STRING,agent:STRING,session_id:STRING,invocation_id:STRING,user_id:STRING,trace_id:STRING,span_id:STRING,parent_span_id:STRING,content:JSON,attributes:JSON,latency_ms:JSON,status:STRING,error_message:STRING,is_truncated:BOOLEAN

# BigQuery JSON-null semantics: a native JSON `null` is NOT SQL NULL, so
# `field IS NOT NULL AND JSON_TYPE(field) != 'object'` misclassifies it.
# Accept 'object' and 'null'; count everything else as bad. Positive object
# counts guard against the test passing vacuously.
RESULT=$(bq --project_id="$PROJECT" --location="$LOCATION" query \
  --nouse_legacy_sql --format=json "
  SELECT
    COUNTIF(JSON_TYPE(content) NOT IN ('object', 'null')) AS bad_content,
    COUNTIF(JSON_TYPE(attributes) NOT IN ('object', 'null')) AS bad_attributes,
    COUNTIF(JSON_TYPE(latency_ms) NOT IN ('object', 'null')) AS bad_latency,
    COUNTIF(JSON_TYPE(content) = 'object') AS content_objects,
    COUNTIF(JSON_TYPE(attributes) = 'object') AS attribute_objects,
    COUNTIF(JSON_TYPE(latency_ms) = 'object') AS latency_objects,
    COUNTIF(event_type = 'LLM_RESPONSE'
      AND JSON_VALUE(attributes, '\$.usage_metadata.total_token_count') IS NULL
      AND JSON_VALUE(content, '\$.usage.total') IS NULL) AS unreadable_tokens,
    COUNTIF(event_type IN ('TOOL_COMPLETED','TOOL_ERROR')
      AND JSON_VALUE(content, '\$.tool') IS NULL) AS unreadable_tools
  FROM \`$PROJECT.$DS.agent_events\`")

echo "$RESULT"
python3 - "$RESULT" <<'EOF'
import json, sys
row = {k: int(v) for k, v in json.loads(sys.argv[1])[0].items()}
bad = {k: v for k, v in row.items() if k.startswith(("bad_", "unreadable_"))
       and v != 0}
empty = {k: v for k, v in row.items() if k.endswith("_objects") and v == 0}
if bad or empty:
    print(f"FAIL: bad={bad} vacuous={empty}", file=sys.stderr); sys.exit(1)
print(f"live seed round-trip OK: {row['content_objects']} content / "
      f"{row['attribute_objects']} attribute / {row['latency_objects']} "
      "latency objects; token and tool paths readable")
EOF
