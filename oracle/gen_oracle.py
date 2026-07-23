#!/usr/bin/env python3
"""Generate the 37 parity-oracle queries from spec/dashboard_spec.yaml.

Independence boundary (docs in oracle/README.md): these queries read the
BQAA-generated fixture views directly and translate the pinned LookML
measure/filter semantics on their own. They must NOT import or render
sql/events_v1.sql.tmpl or reuse the dashboard's calculated fields. The
all-events source below is deliberately a plain 15-view union of common
columns — not the production union's typed stable schema.

call_row_policy = raw_row (frozen; docs/decisions/call-row-policy.md):
SUM/AVG/percentile aggregate every row including streaming partials;
call-count measures use COUNT(DISTINCT trace_id|span_id).

Placeholders {{PROJECT}}, {{DATASET}}, {{VIEW_PREFIX}} plus DATE parameters
@start_date/@end_date (half-open UTC window, end date inclusive at the day
level) are supplied by oracle/runner.py.

Hard-fails on any field, filter, or sort form it cannot translate — an
unmapped construct is a review item, never a silent skip.
"""

import argparse
import sys

import yaml

V1_VIEWS = [
    "user_message_received", "llm_request", "llm_response", "llm_error",
    "tool_starting", "tool_completed", "tool_error", "agent_starting",
    "agent_completed", "invocation_starting", "invocation_completed",
    "state_delta", "hitl_credential_request", "hitl_confirmation_request",
    "hitl_input_request",
]

VIEW = "`{{PROJECT}}.{{DATASET}}.{{VIEW_PREFIX}}_%s`"

DATE_PRED = (
    "timestamp >= TIMESTAMP(@start_date, 'UTC')\n"
    "    AND timestamp < TIMESTAMP(DATE_ADD(@end_date, INTERVAL 1 DAY),"
    " 'UTC')")
PREV_PRED = (
    "timestamp >= TIMESTAMP_SUB(TIMESTAMP(@start_date, 'UTC'), INTERVAL\n"
    "      DATE_DIFF(DATE_ADD(@end_date, INTERVAL 1 DAY), @start_date, DAY)"
    " DAY)\n"
    "    AND timestamp < TIMESTAMP(@start_date, 'UTC')")

# LookML token precedence (usage_metadata first, content-derived fallback).
# Real BQAA data uses both total_token_count and total_tokens; keep the pinned
# key first and accept the observed alternate before the generated fallback.
TOK_TOTAL = ("COALESCE("
             "SAFE_CAST(JSON_VALUE(usage_metadata,"
             " '$.total_token_count') AS INT64), "
             "SAFE_CAST(JSON_VALUE(usage_metadata,"
             " '$.total_tokens') AS INT64), usage_total_tokens)")

PK = "CONCAT(trace_id, '|', span_id)"

# semantic name -> (SQL aggregate over the source, source key)
MEASURES = {
    "agent_events.total_events": ("COUNT(*)", "events"),
    "agent_events.total_invocations":
        ("COUNT(DISTINCT invocation_id)", "events"),
    "agent_events.total_traces": ("COUNT(DISTINCT trace_id)", "events"),
    "agent_events.total_sessions": ("COUNT(DISTINCT session_id)", "events"),
    "agent_events.total_users": ("COUNT(DISTINCT user_id)", "events"),
    "v_llm_response.total_tokens_consumed": (f"SUM({TOK_TOTAL})", "llm"),
    "v_llm_response.total_llm_calls": (f"COUNT(DISTINCT {PK})", "llm"),
    # Aggregates stay FAITHFUL to the pinned LookML (raw AVG/PERCENTILE —
    # no rounding inside the query). Cross-run IEEE summation-order noise
    # is a COMPARATOR concern, handled by the declared canonical float
    # precision policy in oracle/runner.py, never by altering query
    # semantics (PR #1 sixth review).
    "v_llm_response.average_llm_latency": ("AVG(total_ms)", "llm"),
    "v_tool_completed.average_tool_latency": ("AVG(total_ms)", "tool"),
    "v_tool_error.total_tool_errors": (f"COUNT(DISTINCT {PK})", "err"),
}
for p in (50, 75, 90, 99):
    MEASURES[f"v_llm_response.p{p}_llm_latency"] = (
        f"PERCENTILE_CONT(total_ms, 0.{p:02d}) OVER ()", "llm")
    MEASURES[f"v_tool_completed.p{p}_tool_latency"] = (
        f"PERCENTILE_CONT(total_ms, 0.{p:02d}) OVER ()", "tool")

# pop_<base>_{current,previous,change} → base measure semantics
POP_BASE = {
    "agent_events.pop_total_traces": "agent_events.total_traces",
    "agent_events.pop_total_sessions": "agent_events.total_sessions",
    "agent_events.pop_total_users": "agent_events.total_users",
    "v_llm_response.pop_total_tokens":
        "v_llm_response.total_tokens_consumed",
    "v_llm_response.pop_llm_calls": "v_llm_response.total_llm_calls",
    "v_tool_error.pop_tool_errors": "v_tool_error.total_tool_errors",
}

DIMENSIONS = {
    "agent_events.agent": "agent",
    "agent_events.user_id": "user_id",
    "agent_events.timestamp_date": "DATE(timestamp, 'UTC')",
    "agent_events.timestamp_minute": "TIMESTAMP_TRUNC(timestamp, MINUTE)",
    "v_tool_completed.timestamp_date": "DATE(timestamp, 'UTC')",
    "v_tool_completed.tool_name": "tool_name",
    "v_tool_error.tool_name": "tool_name",
}

# Runtime filter parameters encode the pinned listener matrix directly in
# each oracle query: a chart gains a control's predicate ONLY when its
# manifest `listen` includes that control. Every source control allows
# multiple values, so parameters are ARRAY<STRING>: an empty array means
# "no filter". Filtered scenarios can therefore prove per-chart listener
# scope — including the four User ID exceptions and Tool Name's one-chart
# scope — as executable expected results.
CONTROL_PARAMS = {
    "Agent": ("filter_agent", "agent"),
    "User ID": ("filter_user_id", "user_id"),
    "Trace ID": ("filter_trace_id", "trace_id"),
    "Span ID": ("filter_span_id", "span_id"),
    "Tool Name": ("filter_tool_name", "tool_name"),
}


def listener_predicates(chart: dict) -> list:
    preds = []
    for control in sorted(chart.get("listen") or {}):
        if control == "Date":
            continue  # the parameterized date window
        if control not in CONTROL_PARAMS:
            raise SystemExit(f"{chart['id']}: unmapped control {control!r}")
        param, col = CONTROL_PARAMS[control]
        preds.append(f"(ARRAY_LENGTH(@{param}) = 0 OR {col} IN "
                     f"UNNEST(@{param}))")
    return preds


def source_sql(kind: str, extra_where: list) -> str:
    where = [DATE_PRED] + extra_where
    if kind == "events":
        cols = ("timestamp, event_type, agent, session_id, invocation_id, "
                "user_id, trace_id, span_id")
        union = "\n    UNION ALL\n    ".join(
            f"SELECT {cols} FROM {VIEW % v}" for v in V1_VIEWS)
        return (f"  WITH all_events AS (\n    {union}\n  )\n"
                f"  SELECT * FROM all_events\n  WHERE "
                + "\n    AND ".join(where))
    view = {"llm": "llm_response", "tool": "tool_completed",
            "err": "tool_error"}[kind]
    return (f"  SELECT * FROM {VIEW % view}\n  WHERE "
            + "\n    AND ".join(where))


def alias(field: str) -> str:
    return field.split(".", 1)[1]


def parse_filters(tile_filters: dict, chart_id: str):
    """Translate the LookML filter subset used by the pinned tiles."""
    where, having_not_null = [], []
    for key, raw in tile_filters.items():
        val = str(raw).strip()
        if val == "":
            continue  # empty default filter (listen target), no predicate
        if key in ("agent_events.timestamp_date",
                   "agent_events.pop_date_filter"):
            continue  # the date window itself — parameterized
        if key == "agent_events.event_type":
            lit = val.strip('"')
            where.append(f"event_type = '{lit}'")
        elif val == "NOT NULL" and key in MEASURES:
            having_not_null.append(key)
        elif val == "-NULL" and key in DIMENSIONS:
            where.append(f"{DIMENSIONS[key]} IS NOT NULL")
        else:
            raise SystemExit(
                f"{chart_id}: untranslated filter {key!r} = {raw!r}")
    return where, having_not_null


def build_query(c: dict) -> str:
    cid = c["id"]
    dims = c["dimensions"]
    meas = c["measures"]
    where, having_nn = parse_filters(c["tile_filters"], cid)
    where = where + listener_predicates(c)

    pop = [m for m in meas if ".pop_" in m]
    plain = [m for m in meas if m not in pop]

    # Source selection follows the explore's one-to-one join semantics: a
    # field on v_tool_completed/v_tool_error/v_llm_response pins the query
    # to that view (agent_events measures over joined rows are identical to
    # the same measures over the view's own rows); agent_events fields are
    # available on every source.
    VIEW_SOURCE = {"v_tool_completed": "tool", "v_tool_error": "err",
                   "v_llm_response": "llm"}
    sources = set()
    for m in plain:
        if m not in MEASURES:
            raise SystemExit(f"{cid}: unmapped measure {m!r}")
        view = m.split(".", 1)[0]
        if view in VIEW_SOURCE:
            sources.add(VIEW_SOURCE[view])
    for m in pop:
        base = m.rsplit("_", 1)[0]
        if base not in POP_BASE:
            raise SystemExit(f"{cid}: unmapped PoP measure {m!r}")
        view = base.split(".", 1)[0]
        if view in VIEW_SOURCE:
            sources.add(VIEW_SOURCE[view])
    for d in dims:
        if d not in DIMENSIONS:
            raise SystemExit(f"{cid}: unmapped dimension {d!r}")
        view = d.split(".", 1)[0]
        if view in VIEW_SOURCE:
            sources.add(VIEW_SOURCE[view])
    if len(sources) > 1:
        raise SystemExit(f"{cid}: fields span sources {sorted(sources)}")
    src = sources.pop() if sources else "events"

    header = (
        f"-- oracle query: {cid}\n"
        f"-- source tile: {c['source_tile']} (page: {c['page']})\n"
        f"-- pinned block: fe6423cc9775b6dc61f7f7047dd4424603ddb3a1\n"
        f"-- dashboard date default: "
        f"{'7' if c['source_dashboard'] == 'performance' else '14'} days\n"
        f"-- call_row_policy: raw_row\n"
        f"-- GENERATED by oracle/gen_oracle.py — independent LookML\n"
        f"-- translation; must never share the production union SQL.\n")

    if pop:
        base = pop[0].rsplit("_", 1)[0]
        expr, _ = MEASURES[POP_BASE[base]]
        prev_src = source_sql(src, where).replace(DATE_PRED, PREV_PRED)
        wanted = []
        for m in pop:
            kind = m.rsplit("_", 1)[1]
            col = {"current": "cur.value",
                   "previous": "prev.value",
                   "change": "SAFE_DIVIDE(cur.value - prev.value,"
                             " prev.value)"}[kind]
            wanted.append(f"  {col} AS {alias(m)}")
        return (header
                + "WITH cur AS (\n  SELECT " + expr + " AS value FROM (\n"
                + source_sql(src, where) + "\n  )\n),\n"
                + "prev AS (\n  SELECT " + expr + " AS value FROM (\n"
                + prev_src + "\n  )\n)\n"
                + "SELECT\n" + ",\n".join(wanted)
                + "\nFROM cur CROSS JOIN prev\n")

    select, group = [], []
    for d in dims:
        select.append(f"  {DIMENSIONS[d]} AS {alias(d)}")
        group.append(alias(d))
    windowed = False
    for m in plain:
        expr, _ = MEASURES[m]
        if "OVER ()" in expr:
            windowed = True
        select.append(f"  {expr} AS {alias(m)}")

    if windowed and group:
        raise SystemExit(f"{c['id']}: percentile with dimensions unsupported")

    # LookML measure filters compile to HAVING on the grouped explore query.
    # Same-source: plain HAVING. Cross-source (e.g. traces-by-agent filtered
    # on LLM token sums): reproduce via an aux aggregate over the measure's
    # own view, inner-joined on the group keys with a NOT NULL guard —
    # groups whose joined SUM is NULL are excluded exactly as in Looker.
    same_src_having, cross_src_having = [], []
    for m in having_nn:
        m_src = VIEW_SOURCE.get(m.split(".", 1)[0], "events")
        (same_src_having if m_src == src else cross_src_having).append(m)
    if cross_src_having and not group:
        raise SystemExit(f"{cid}: cross-source measure filter without "
                         "group keys is unsupported")

    sql = header + "SELECT\n" + ",\n".join(select) + "\nFROM (\n" \
        + source_sql(src, where) + "\n)\n"
    if group:
        sql += "GROUP BY " + ", ".join(group) + "\n"
    if windowed:
        sql = sql.rstrip() + "\nQUALIFY ROW_NUMBER() OVER () = 1\n"
    if same_src_having:
        sql += "HAVING " + " AND ".join(
            f"{MEASURES[m][0]} IS NOT NULL" for m in same_src_having) + "\n"
    if cross_src_having:
        m = cross_src_having[0]
        if len(cross_src_having) > 1:
            raise SystemExit(f"{cid}: multiple cross-source filters "
                             "unsupported")
        m_expr, m_src = MEASURES[m]
        # The auxiliary aggregation carries the SAME listener predicates as
        # the base (they reference common event columns present on every
        # view). Dropping them reproduced a live defect: with a non-LLM
        # Span ID selected, the pinned join yields 0 rows but an unfiltered
        # aux still admitted the group (PR #1 fifth review).
        aux_where = [p for p in where]
        keys = ", ".join(DIMENSIONS[d] + " AS " + alias(d) for d in dims)
        on = " AND ".join(f"base.{alias(d)} = aux.{alias(d)}" for d in dims)
        sql = (header
               + "WITH base AS (\n"
               + "SELECT\n" + ",\n".join(select) + "\nFROM (\n"
               + source_sql(src, where) + "\n)\n"
               + "GROUP BY " + ", ".join(group) + "\n"
               + "),\naux AS (\n"
               + f"SELECT {keys}, {m_expr} AS filter_value FROM (\n"
               + source_sql(m_src, aux_where) + "\n)\n"
               + "GROUP BY " + ", ".join(group) + "\n)\n"
               + "SELECT base.*\nFROM base\nJOIN aux ON " + on
               + "\nWHERE aux.filter_value IS NOT NULL\n")
    parts = []
    for s in c["sorts"]:
        bits = s.split()
        fld = alias(bits[0])
        desc = " DESC" if "desc" in bits[1:] else ""
        parts.append(fld + desc)
    # Determinism additions — FROZEN INTENTIONAL DIVERGENCES from the
    # pinned block (docs/decisions/determinism-additions.md), which leaves
    # ties and unsorted LIMITs engine-ordered; both are upstream candidates:
    #  * a tile with a LIMIT but no declared sort gets measures DESC —
    #    matching the tile's visual "top N" intent — so LIMIT never
    #    selects arbitrary rows (affects "Top 5 users with most Tokens
    #    consumption", whose pinned LookML has LIMIT 5 and no sorts);
    #  * every dimension not already sorted is appended as a tie-breaker.
    if c["limit"] and not parts:
        parts = [alias(m) + " DESC" for m in plain]
    sorted_fields = {p.split()[0] for p in parts}
    for d in dims:
        if alias(d) not in sorted_fields:
            parts.append(alias(d))
    if parts:
        sql += "ORDER BY " + ", ".join(parts) + "\n"
    if c["limit"]:
        sql += f"LIMIT {c['limit']}\n"
    return sql


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default="spec/dashboard_spec.yaml")
    ap.add_argument("--outdir", default="oracle/queries")
    args = ap.parse_args()

    spec = yaml.safe_load(open(args.spec))
    n = 0
    for c in spec["charts"]:
        sql = build_query(c)
        with open(f"{args.outdir}/{c['id']}.sql", "w") as fh:
            fh.write(sql)
        n += 1
    print(f"wrote {n} oracle queries to {args.outdir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
