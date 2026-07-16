#!/usr/bin/env python3
"""Deterministic synthetic agent_events seed generator (NDJSON for `bq load`).

Publication safety: every identifier is synthetic; no production project IDs,
credentials, user identifiers, prompts, tool arguments/results, or error
payloads. Deterministic by --seed so expected oracle results are stable.

JSON payload encoding: `content`, `attributes`, and `latency_ms` are emitted
as JSON OBJECTS inside each NDJSON row (single-encoded), matching how the
BQAA plugin stores them in the JSON-typed columns. Loading with `bq load
--source_format=NEWLINE_DELIMITED_JSON` against the agent_events schema must
yield `JSON_TYPE(content) = 'object'`; tools/test_seed_events.py asserts the
structural half locally and tools/live_seed_roundtrip_test.sh asserts the
BigQuery half.

Coverage guarantee: every run deterministically emits at least one row for
each of the 15 static-intersection event types (one warm-up turn exercises
them all), so no union branch is ever empty.

`--events N` is a MINIMUM: generation completes the final conversational
turn, so the actual total is N or slightly higher (reported in the summary).

Embedded contract fixtures — each tracked by an explicit counter in the
printed summary:

  * token precedence — LLM_RESPONSE variants: metadata_only, content_only,
    conflict (both set, different values; precedence must pick metadata);
  * streaming partial LLM_RESPONSE rows sharing one trace/span with the
    final response (call_row_policy fixtures);
  * duplicate tool terminal events on one span;
  * base-table-only event types (HITL_CREDENTIAL_COMPLETED,
    HITL_CONFIRMATION_COMPLETED) proving the documented undercount;
  * TOOL_ERROR rows with synthetic error strings;
  * a near-empty day (day offset 3) so date-boundary tests have edges.

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

FIXTURE_COUNTERS = [
    "token_metadata_only", "token_content_only", "token_conflict",
    "streaming_partial_rows", "duplicate_tool_terminal_rows",
    "base_table_only_rows",
]


def iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S.%f UTC")


def hexid(rng: random.Random, n: int) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


def row(ts, event_type, agent, session, invocation, user, trace, span,
        parent=None, content=None, attributes=None, latency=None,
        status="OK", error=None):
    # content/attributes/latency_ms stay as dicts — the single outer NDJSON
    # serialization encodes them as JSON objects (never double-encode).
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
        "content": content,
        "attributes": attributes,
        "latency_ms": latency,
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


class Emitter:
    def __init__(self, fh):
        self.fh = fh
        self.counts: dict = {}
        self.fixtures = {k: 0 for k in FIXTURE_COUNTERS}
        self.total = 0

    def emit(self, r, fixture=None):
        self.fh.write(json.dumps(r) + "\n")
        self.counts[r["event_type"]] = self.counts.get(r["event_type"], 0) + 1
        if fixture:
            self.fixtures[fixture] += 1
        self.total += 1


def warmup_turn(em: Emitter, rng: random.Random, start: datetime):
    """Deterministically exercise all 15 static-intersection event types."""
    ts = start + timedelta(hours=1)
    agent, user = AGENTS[0], "user-0001"
    session, invocation = "sess-warmup", "inv-warmup"
    trace = hexid(rng, 32)

    def r(offset_s, event_type, **kw):
        em.emit(row(ts + timedelta(seconds=offset_s), event_type, agent,
                    session, invocation, user, trace, hexid(rng, 16), **kw))

    r(0, "USER_MESSAGE_RECEIVED")
    r(1, "INVOCATION_STARTING")
    r(2, "AGENT_STARTING",
      content={"text_summary": "synthetic instruction"})
    r(3, "LLM_REQUEST",
      content={"request": {"text": "synthetic"}},
      attributes={"model": MODELS[0], "llm_config": {"temperature": 0},
                  "tools": [{"name": TOOLS[0]}]})
    em.emit(llm_response(rng, ts + timedelta(seconds=4), agent, session,
                         invocation, user, trace, hexid(rng, 16),
                         "metadata_only"), fixture="token_metadata_only")
    r(5, "LLM_ERROR", status="ERROR", error="synthetic: model unavailable",
      latency={"total_ms": 1200})
    r(6, "TOOL_STARTING",
      content={"tool": TOOLS[0], "args": {"synthetic": True},
               "tool_origin": "LOCAL"})
    r(7, "TOOL_COMPLETED",
      content={"tool": TOOLS[0], "result": {"ok": True},
               "tool_origin": "LOCAL"},
      latency={"total_ms": 150})
    r(8, "TOOL_ERROR", status="ERROR", error="synthetic: upstream timeout",
      content={"tool": TOOLS[1], "args": {"synthetic": True},
               "tool_origin": "MCP"},
      latency={"total_ms": 90})
    r(9, "STATE_DELTA", attributes={"state_delta": {"synthetic": 1}})
    r(10, "HITL_CREDENTIAL_REQUEST",
      content={"tool": TOOLS[2], "args": {"synthetic": True}})
    r(11, "HITL_CONFIRMATION_REQUEST",
      content={"tool": TOOLS[2], "args": {"synthetic": True}})
    r(12, "HITL_INPUT_REQUEST",
      content={"tool": TOOLS[2], "args": {"synthetic": True}})
    r(13, "AGENT_COMPLETED", latency={"total_ms": 9000})
    r(14, "INVOCATION_COMPLETED")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=100000,
                    help="MINIMUM number of events; the final turn completes")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--seed", type=int, default=20260715)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    end = datetime(2026, 7, 15, tzinfo=timezone.utc)
    start = end - timedelta(days=args.days)

    with open(args.out, "w") as fh:
        em = Emitter(fh)
        warmup_turn(em, rng, start)

        while em.total < args.events:
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

            em.emit(row(ts, "USER_MESSAGE_RECEIVED", agent, session,
                        invocation, user, trace, hexid(rng, 16)))
            em.emit(row(ts + timedelta(milliseconds=100),
                        "INVOCATION_STARTING", agent, session, invocation,
                        user, trace, hexid(rng, 16)))
            em.emit(row(ts + timedelta(milliseconds=200), "LLM_REQUEST",
                        agent, session, invocation, user, trace,
                        hexid(rng, 16),
                        content={"request": {"text": "synthetic"}},
                        attributes={"model": rng.choice(MODELS)}))

            span = hexid(rng, 16)
            variant = rng.choices(
                ["metadata_only", "content_only", "conflict"],
                weights=[70, 20, 10])[0]
            if rng.random() < 0.05:
                for _ in range(rng.randint(1, 3)):
                    em.emit(llm_response(rng, ts, agent, session, invocation,
                                         user, trace, span, variant,
                                         partial=True),
                            fixture="streaming_partial_rows")
            em.emit(llm_response(rng, ts + timedelta(seconds=2), agent,
                                 session, invocation, user, trace, span,
                                 variant), fixture=f"token_{variant}")

            for _ in range(rng.randint(0, 2)):
                tspan = hexid(rng, 16)
                tool = rng.choice(TOOLS)
                tts = ts + timedelta(seconds=rng.randint(3, 20))
                em.emit(row(tts - timedelta(milliseconds=500),
                            "TOOL_STARTING", agent, session, invocation,
                            user, trace, tspan,
                            content={"tool": tool,
                                     "args": {"synthetic": True},
                                     "tool_origin": rng.choice(ORIGINS)}))
                if rng.random() < 0.07:
                    em.emit(row(tts, "TOOL_ERROR", agent, session,
                                invocation, user, trace, tspan,
                                content={"tool": tool,
                                         "args": {"synthetic": True},
                                         "tool_origin": rng.choice(ORIGINS)},
                                latency={"total_ms": rng.randint(40, 9000)},
                                status="ERROR",
                                error="synthetic: upstream timeout"))
                else:
                    em.emit(row(tts, "TOOL_COMPLETED", agent, session,
                                invocation, user, trace, tspan,
                                content={"tool": tool, "result": {"ok": True},
                                         "tool_origin": rng.choice(ORIGINS)},
                                latency={"total_ms": rng.randint(40, 9000)}))
                    if rng.random() < 0.01:
                        em.emit(row(tts + timedelta(milliseconds=50),
                                    "TOOL_COMPLETED", agent, session,
                                    invocation, user, trace, tspan,
                                    content={"tool": tool,
                                             "result": {"ok": True},
                                             "tool_origin": "LOCAL"},
                                    latency={"total_ms":
                                             rng.randint(40, 9000)}),
                                fixture="duplicate_tool_terminal_rows")

            if rng.random() < 0.02:
                em.emit(row(ts + timedelta(seconds=30),
                            rng.choice(BASE_TABLE_ONLY), agent, session,
                            invocation, user, trace, hexid(rng, 16),
                            content={"tool": rng.choice(TOOLS)}),
                        fixture="base_table_only_rows")

            em.emit(row(ts + timedelta(seconds=rng.randint(30, 90)),
                        "INVOCATION_COMPLETED", agent, session, invocation,
                        user, trace, hexid(rng, 16)))

    summary = {
        "requested_minimum": args.events,
        "total_emitted": em.total,
        "by_event_type": dict(sorted(em.counts.items())),
        "fixtures": em.fixtures,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
