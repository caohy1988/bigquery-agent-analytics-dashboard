#!/usr/bin/env python3
"""CI structural tests for the seed generator (no BigQuery required).

Asserts, on a deterministic sample:
  * content/attributes/latency_ms are JSON OBJECTS (dicts) in the NDJSON —
    the double-encoding regression (PR #1 review P1) stays fixed;
  * every one of the 15 static-intersection event types is present;
  * every named contract fixture counter is > 0;
  * determinism: same seed => byte-identical output;
  * --events is honored as a minimum.

The BigQuery half (load round-trip asserting JSON_TYPE='object' and
non-null generated-view fields) lives in tools/live_seed_roundtrip_test.sh.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

STATIC_INTERSECTION = {
    "USER_MESSAGE_RECEIVED", "LLM_REQUEST", "LLM_RESPONSE", "LLM_ERROR",
    "TOOL_STARTING", "TOOL_COMPLETED", "TOOL_ERROR", "AGENT_STARTING",
    "AGENT_COMPLETED", "INVOCATION_STARTING", "INVOCATION_COMPLETED",
    "STATE_DELTA", "HITL_CREDENTIAL_REQUEST", "HITL_CONFIRMATION_REQUEST",
    "HITL_INPUT_REQUEST",
}
JSON_FIELDS = ("content", "attributes", "latency_ms")
MIN_EVENTS = 3000


def run(out: Path) -> str:
    r = subprocess.run(
        [sys.executable, "tools/seed_events.py", "--events",
         str(MIN_EVENTS), "--days", "30", "--seed", "42", "--out", str(out)],
        capture_output=True, text=True, check=True)
    return r.stdout


def main() -> int:
    errors = []
    with tempfile.TemporaryDirectory() as td:
        a, b = Path(td) / "a.ndjson", Path(td) / "b.ndjson"
        summary = json.loads(run(a))
        run(b)

        if a.read_bytes() != b.read_bytes():
            errors.append("same seed did not produce identical output")

        rows = [json.loads(line) for line in a.open()]
        if len(rows) < MIN_EVENTS:
            errors.append(f"--events minimum violated: {len(rows)}")
        if len(rows) != summary["total_emitted"]:
            errors.append("summary total does not match emitted rows")

        seen = {r["event_type"] for r in rows}
        missing = STATIC_INTERSECTION - seen
        if missing:
            errors.append(f"missing event types: {sorted(missing)}")

        for r in rows:
            for f in JSON_FIELDS:
                v = r[f]
                if v is not None and not isinstance(v, dict):
                    errors.append(
                        f"{f} must be a JSON object, got {type(v).__name__} "
                        f"(event_type={r['event_type']})")
                    break
            else:
                continue
            break

        zero = [k for k, v in summary["fixtures"].items() if v == 0]
        if zero:
            errors.append(f"fixture counters at zero: {zero}")

        llm = next(r for r in rows if r["event_type"] == "LLM_RESPONSE"
                   and r["attributes"] and "usage_metadata"
                   in r["attributes"])
        um = llm["attributes"]["usage_metadata"]
        if not isinstance(um, dict) or "total_token_count" not in um:
            errors.append("usage_metadata is not a readable object")

    if errors:
        for e in errors:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(f"seed tests OK: {summary['total_emitted']} rows, "
          f"15/15 event types, all fixture counters non-zero, "
          f"payloads are JSON objects, deterministic")
    return 0


if __name__ == "__main__":
    sys.exit(main())
