#!/usr/bin/env python3
"""M0 benchmark and gate evidence on the 10M-event reference workload.

Runs, per the contract's benchmark methodology:

  * N cold runs (default 20) per representative query shape, query cache
    DISABLED, recording total_bytes_processed, total_bytes_billed,
    total_slot_ms, and elapsed wall time from job statistics;
  * the structural union gate: total_bytes_processed of the production
    union (rendered from events_v1.sql.tmpl semantics) must be <= 3x a
    comparable date-pruned raw-table scan selecting the same source
    columns over the same window;
  * date-boundary proof: the half-open UTC predicate includes the end
    date's rows and excludes the day after / day before start, verified
    against per-day raw-table counts;
  * partition-pruning proof: a 1-day window must process a small fraction
    of the 30-day window's bytes.

BI Engine state: this project has no BI Engine reservation; cache is
disabled per run. Both are recorded in the output.

Requires google-cloud-bigquery (run inside one of the fixture venvs).
"""

import argparse
import json
import statistics
import sys

from google.cloud import bigquery

V1_VIEWS = [
    "user_message_received", "llm_request", "llm_response", "llm_error",
    "tool_starting", "tool_completed", "tool_error", "agent_starting",
    "agent_completed", "invocation_starting", "invocation_completed",
    "state_delta", "hitl_credential_request", "hitl_confirmation_request",
    "hitl_input_request",
]

DATE_PRED = ("timestamp >= TIMESTAMP('{start}', 'UTC') AND "
             "timestamp < TIMESTAMP(DATE_ADD(DATE '{end}', INTERVAL 1 DAY),"
             " 'UTC')")

TOK = ("COALESCE(CAST(JSON_VALUE(usage_metadata, '$.total_token_count')"
       " AS INT64), usage_total_tokens)")


def union_sql(p, d, cols="timestamp, event_type, agent, session_id, "
                          "invocation_id, user_id, trace_id, span_id"):
    return "\nUNION ALL\n".join(
        f"SELECT {cols} FROM `{p}.{d}.v_{v}`" for v in V1_VIEWS)


def shapes(p, d, start, end):
    w = DATE_PRED.format(start=start, end=end)
    u = union_sql(p, d)
    return {
        "scorecard_total_events_union":
            f"SELECT COUNT(*) FROM ({u}) WHERE {w}",
        "scorecard_distinct_sessions_union":
            f"SELECT COUNT(DISTINCT session_id) FROM ({u}) WHERE {w}",
        "trend_tokens_by_day":
            f"SELECT DATE(timestamp,'UTC') d, SUM({TOK}) t "
            f"FROM `{p}.{d}.v_llm_response` WHERE {w} GROUP BY d ORDER BY d",
        "bar_top5_users_by_traces_union":
            f"SELECT user_id, COUNT(DISTINCT trace_id) c FROM ({u}) "
            f"WHERE {w} GROUP BY user_id ORDER BY c DESC, user_id LIMIT 5",
        "scorecard_p90_tool_latency":
            f"SELECT DISTINCT PERCENTILE_CONT(total_ms, 0.90) OVER () "
            f"FROM `{p}.{d}.v_tool_completed` WHERE {w}",
        "scorecard_pop_tokens":
            f"WITH cur AS (SELECT SUM({TOK}) v FROM "
            f"`{p}.{d}.v_llm_response` WHERE {w}), "
            f"prev AS (SELECT SUM({TOK}) v FROM `{p}.{d}.v_llm_response` "
            f"WHERE timestamp >= TIMESTAMP_SUB(TIMESTAMP('{start}','UTC'), "
            f"INTERVAL DATE_DIFF(DATE_ADD(DATE '{end}', INTERVAL 1 DAY), "
            f"DATE '{start}', DAY) DAY) AND timestamp < "
            f"TIMESTAMP('{start}','UTC')) "
            f"SELECT cur.v, prev.v, SAFE_DIVIDE(cur.v-prev.v, prev.v) "
            f"FROM cur CROSS JOIN prev",
    }


def run_cold(client, sql, runs):
    stats = []
    for _ in range(runs):
        job = client.query(sql, job_config=bigquery.QueryJobConfig(
            use_query_cache=False))
        job.result()
        stats.append({
            "bytes_processed": job.total_bytes_processed,
            "bytes_billed": job.total_bytes_billed,
            "slot_ms": job.slot_millis,
            "elapsed_ms": int((job.ended - job.started).total_seconds()
                              * 1000),
        })
    return stats


def pctl(vals, p):
    vals = sorted(vals)
    k = max(0, min(len(vals) - 1, round(p / 100 * (len(vals) - 1))))
    return vals[k]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--start", default="2026-06-16")
    ap.add_argument("--end", default="2026-07-15")
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    p, d = args.project, args.dataset
    client = bigquery.Client(project=p)

    result = {"project": p, "dataset": d,
              "window": {"start": args.start, "end": args.end},
              "runs_per_shape": args.runs,
              "query_cache": "disabled",
              "bi_engine": "no reservation in project",
              "pricing_model": "on-demand",
              "shapes": {}, "gates": {}}

    for name, sql in shapes(p, d, args.start, args.end).items():
        stats = run_cold(client, sql, args.runs)
        el = [s["elapsed_ms"] for s in stats]
        result["shapes"][name] = {
            "bytes_processed": stats[0]["bytes_processed"],
            "bytes_billed_max": max(s["bytes_billed"] for s in stats),
            "slot_ms_median": statistics.median(
                s["slot_ms"] for s in stats),
            "elapsed_ms": {"p50": pctl(el, 50), "p95": pctl(el, 95),
                            "min": min(el), "max": max(el)},
            "raw": stats,
        }
        print(f"{name}: bytes={stats[0]['bytes_processed']:,} "
              f"p50={pctl(el, 50)}ms p95={pctl(el, 95)}ms")

    # --- structural union gate (bytes processed, cache off) ---
    w = DATE_PRED.format(start=args.start, end=args.end)
    union_cols = ("timestamp, event_type, agent, session_id, invocation_id,"
                  " user_id, trace_id, span_id, parent_span_id, status,"
                  " error_message, is_truncated")
    union_all_cols = union_sql(p, d, union_cols)
    union_probe = (f"SELECT COUNT(*), COUNT(DISTINCT trace_id) FROM "
                   f"({union_all_cols}) WHERE {w}")
    base_probe = (f"SELECT COUNT(*), COUNT(DISTINCT trace_id) FROM "
                  f"(SELECT {union_cols} FROM `{p}.{d}.agent_events`) "
                  f"WHERE {w}")
    ub = run_cold(client, union_probe, 3)
    bb = run_cold(client, base_probe, 3)
    ratio = ub[0]["bytes_processed"] / bb[0]["bytes_processed"]
    result["gates"]["structural_union"] = {
        "union_bytes_processed": ub[0]["bytes_processed"],
        "base_scan_bytes_processed": bb[0]["bytes_processed"],
        "ratio": round(ratio, 4),
        "limit": 3.0,
        "pass": ratio <= 3.0,
    }
    print(f"structural gate: union={ub[0]['bytes_processed']:,} "
          f"base={bb[0]['bytes_processed']:,} ratio={ratio:.4f} "
          f"({'PASS' if ratio <= 3.0 else 'FAIL'})")

    # --- date-boundary proof ---
    bq_rows = list(client.query(f"""
        SELECT
          (SELECT COUNT(*) FROM ({union_sql(p, d)})
           WHERE {w}) AS in_window,
          (SELECT COUNT(*) FROM `{p}.{d}.agent_events`
           WHERE DATE(timestamp,'UTC') BETWEEN '{args.start}' AND
             '{args.end}'
             AND event_type NOT IN ('HITL_CREDENTIAL_COMPLETED',
                                     'HITL_CONFIRMATION_COMPLETED'))
             AS raw_in_window,
          (SELECT COUNT(*) FROM ({union_sql(p, d)})
           WHERE DATE(timestamp,'UTC') = '{args.end}') AS end_day_rows,
          (SELECT COUNT(*) FROM ({union_sql(p, d)})
           WHERE DATE(timestamp,'UTC') =
             DATE_ADD(DATE '{args.end}', INTERVAL 1 DAY)) AS after_end_rows
    """).result())
    row = dict(bq_rows[0])
    boundary_pass = (row["in_window"] == row["raw_in_window"]
                     and row["end_day_rows"] > 0)
    result["gates"]["date_boundary"] = {**row, "pass": boundary_pass}
    print(f"date boundary: {row} ({'PASS' if boundary_pass else 'FAIL'})")

    # --- partition-pruning proof ---
    one_day = DATE_PRED.format(start=args.end, end=args.end)
    narrow = run_cold(client, f"SELECT COUNT(*) FROM ({union_sql(p, d)}) "
                              f"WHERE {one_day}", 1)
    wide = result["shapes"]["scorecard_total_events_union"]
    frac = narrow[0]["bytes_processed"] / wide["bytes_processed"]
    result["gates"]["partition_pruning"] = {
        "one_day_bytes": narrow[0]["bytes_processed"],
        "thirty_day_bytes": wide["bytes_processed"],
        "fraction": round(frac, 4),
        "pass": frac < 0.2,
    }
    print(f"pruning: 1d={narrow[0]['bytes_processed']:,} "
          f"30d={wide['bytes_processed']:,} fraction={frac:.4f} "
          f"({'PASS' if frac < 0.2 else 'FAIL'})")

    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=1, sort_keys=True)
        fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
