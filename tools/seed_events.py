#!/usr/bin/env python3
"""Deterministic synthetic agent_events seed generator (NDJSON for `bq load`).

Publication safety: every identifier is synthetic; no production project IDs,
credentials, user identifiers, prompts, tool arguments/results, or error
payloads. Deterministic by --seed so expected oracle results are stable.

Embedded contract fixtures (all counted in the summary the tool prints):

  * token precedence — LLM_RESPONSE rows in three variants:
      metadata_only  (usage_metadata set, content-derived columns NULL)
      content_only   (content usage set, usage_metadata absent)
      conflict       (both set, DIFFERENT values — precedence must pick
                      usage_metadata)
  * repeated call keys — streaming partial LLM_RESPONSE rows sharing one
    trace/span with the final response (call_row_policy fixtures);
  * duplicate tool terminal events on one span;
  * base-table-only event types (HITL_CREDENTIAL_COMPLETED,
    HITL_CONFIRMATION_COMPLETED) that have NO generated view — these prove
    the documented Total Events undercount;
  * TOOL_ERROR rows with synthetic error strings;
  * an empty-ish day gap so date-boundary tests have edges.

Usage:
  python3 tools/seed_events.py --events 10000000 --days 30 \
      --seed 20260715 --out seed.ndjson
"""

import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone

AGENTS = ["order-agent", "billing-agent", "support-agent", "search-agent",
          "escalation-agent"]
TOOLS = ["lookup_order", "fetch_invoice", "search_kb", "create_ticket",
         "check_inventory"]
MODELS = ["gemini-2.5-flash", "gemini-2.5-pro"]
ORIGINS = ["LOCAL", "MCP", "SUB_AGENT"]
BASE_TABLE_ONLY = ["HITL_CREDENTIAL_COMPLETED", "HITL_CONFIRMATION_COMPLETED"]


def iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S.%f UTC")


def hexid(rng: random.Random, n: int) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


def row(ts, event_type, agent, session, invocation, user, trace, span,
        parent=None, content=None, attributes=None, latency=None,
        status="OK", error=None):
    return {
        "timestamp": iso(ts),
        "event_type": event_type,
        "agent": agent,
        "session_id": session,
        "invocation_id": invocation,
        "user_id": user,
        "trace_id": trace,
        "span_id": span,
        "parent_span_id": parent,
        "content": json.dumps(content) if content is not None else None,
        "attributes": json.dumps(attributes) if attributes is not None
        else None,
        "latency_ms": json.dumps(latency) if latency is not None else None,
        "status": status,
        "error_message": error,
        "is_truncated": False,
    }


def llm_response(rng, ts, agent, session, invocation, user, trace, span,
                 variant, partial=False):
    prompt = rng.randint(200, 4000)
    completion = rng.randint(20, 800)
    total = prompt + completion
    content, attributes = {"response": {"text": "synthetic"}}, {}
    if variant in ("content_only", "conflict"):
        content["usage"] = {"prompt": prompt, "completion": completion,
                            "total": total}
    if variant in ("metadata_only", "conflict"):
        bump = 7 if variant == "conflict" else 0
        attributes["usage_metadata"] = {
            "prompt_token_count": prompt + bump,
            "candidates_token_count": completion + bump,
            "total_token_count": total + 2 * bump,
        }
    attributes["model_version"] = rng.choice(MODELS)
    latency = None if partial else {
        "total_ms": rng.randint(300, 12000),
        "time_to_first_token_ms": rng.randint(80, 1500),
    }
    return row(ts, "LLM_RESPONSE", agent, session, invocation, user, trace,
               span, content=content, attributes=attributes, latency=latency)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=100000)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--seed", type=int, default=20260715)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    end = datetime(2026, 7, 15, tzinfo=timezone.utc)
    start = end - timedelta(days=args.days)
    counts: dict = {}

    def bump(k):
        counts[k] = counts.get(k, 0) + 1

    with open(args.out, "w") as fh:
        def emit(r):
            fh.write(json.dumps(r) + "\n")
            bump(r["event_type"])

        n = 0
        while n < args.events:
            # One invocation "turn": invocation start, llm req/resp,
            # 0-2 tool calls, invocation end.
            day_offset = rng.random() * args.days
            # leave day 3 nearly empty for boundary tests
            if 2.9 < day_offset < 3.9 and rng.random() < 0.95:
                continue
            ts = start + timedelta(days=day_offset)
            agent = rng.choice(AGENTS)
            user = f"user-{rng.randint(1, 500):04d}"
            session = f"sess-{hexid(rng, 12)}"
            invocation = f"inv-{hexid(rng, 12)}"
            trace = hexid(rng, 32)

            emit(row(ts, "INVOCATION_STARTING", agent, session, invocation,
                     user, trace, hexid(rng, 16)))
            n += 1

            span = hexid(rng, 16)
            variant = rng.choices(
                ["metadata_only", "content_only", "conflict"],
                weights=[70, 20, 10])[0]
            # call_row_policy fixture: ~5% of responses stream partials
            # under the SAME trace/span before the final row.
            if rng.random() < 0.05:
                for _ in range(rng.randint(1, 3)):
                    emit(llm_response(rng, ts, agent, session, invocation,
                                      user, trace, span, variant,
                                      partial=True))
                    n += 1
            emit(llm_response(rng, ts + timedelta(seconds=2), agent, session,
                              invocation, user, trace, span, variant))
            n += 1

            for _ in range(rng.randint(0, 2)):
                tspan = hexid(rng, 16)
                tool = rng.choice(TOOLS)
                tts = ts + timedelta(seconds=rng.randint(3, 20))
                if rng.random() < 0.07:
                    emit(row(tts, "TOOL_ERROR", agent, session, invocation,
                             user, trace, tspan,
                             content={"tool": tool,
                                      "args": {"synthetic": True},
                                      "tool_origin": rng.choice(ORIGINS)},
                             latency={"total_ms": rng.randint(40, 9000)},
                             status="ERROR",
                             error="synthetic: upstream timeout"))
                else:
                    emit(row(tts, "TOOL_COMPLETED", agent, session,
                             invocation, user, trace, tspan,
                             content={"tool": tool, "result": {"ok": True},
                                      "tool_origin": rng.choice(ORIGINS)},
                             latency={"total_ms": rng.randint(40, 9000)}))
                n += 1
                # duplicate tool terminal on same span (~1%)
                if rng.random() < 0.01:
                    emit(row(tts + timedelta(milliseconds=50),
                             "TOOL_COMPLETED", agent, session, invocation,
                             user, trace, tspan,
                             content={"tool": tool, "result": {"ok": True},
                                      "tool_origin": "LOCAL"},
                             latency={"total_ms": rng.randint(40, 9000)}))
                    n += 1

            # base-table-only events (~2% of turns) — undercount fixture
            if rng.random() < 0.02:
                emit(row(ts + timedelta(seconds=30),
                         rng.choice(BASE_TABLE_ONLY), agent, session,
                         invocation, user, trace, hexid(rng, 16),
                         content={"tool": rng.choice(TOOLS)}))
                n += 1

            emit(row(ts + timedelta(seconds=rng.randint(30, 90)),
                     "INVOCATION_COMPLETED", agent, session, invocation,
                     user, trace, hexid(rng, 16)))
            n += 1

    print(json.dumps({"total": sum(counts.values()),
                      "by_event_type": dict(sorted(counts.items()))},
                     indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
