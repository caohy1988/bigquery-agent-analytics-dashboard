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
import hashlib
import json
import pathlib
import statistics
import subprocess
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
            "job_id": job.job_id,
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
    ap.add_argument("--allow-fewer-runs", action="store_true",
                    help="Dev only; contract minimum is 20 cold runs")
    ap.add_argument("--scenario-manifest", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    p, d = args.project, args.dataset
    client = bigquery.Client(project=p)

    # Provenance bindings (seventh review): benchmark evidence must bind
    # its scenario/seed, tool, repo commit, and template SQL.
    root = str(pathlib.Path(__file__).resolve().parent.parent)
    def sh(pth):
        return hashlib.sha256(open(pth, "rb").read()).hexdigest()
    commit = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                            capture_output=True, text=True,
                            check=True).stdout.strip()
    if subprocess.run(["git", "-C", root, "status", "--porcelain",
                       "--untracked-files=no"], capture_output=True,
                      text=True, check=True).stdout.strip():
        print("ERROR: modified tracked files — benchmark evidence must "
              "come from committed code", file=sys.stderr)
        return 1
    manifest = json.load(open(args.scenario_manifest))
    if args.runs < 20 and not args.allow_fewer_runs:
        print("ERROR: contract minimum is 20 cold runs per shape",
              file=sys.stderr)
        return 1

    # Load verification: the queried table must BE the manifest's seed.
    n_rows = int(list(client.query(
        f"SELECT COUNT(*) AS n FROM `{p}.{d}.agent_events`").result()
    )[0]["n"])
    if n_rows != manifest["seed"]["total_emitted"]:
        print(f"ERROR: table has {n_rows} rows but the scenario manifest "
              f"binds a seed of {manifest['seed']['total_emitted']} — "
              "wrong or partial load", file=sys.stderr)
        return 1

    # Observed (not asserted) capacity state.
    def observe(sql):
        try:
            return [dict(r) for r in client.query(sql).result()]
        except Exception as e:
            return f"query failed: {str(e)[:120]}"
    bi_obs = observe("SELECT * FROM `region-us`."
                     "INFORMATION_SCHEMA.BI_CAPACITIES")
    res_obs = observe("SELECT * FROM `region-us`."
                      "INFORMATION_SCHEMA.ASSIGNMENTS_BY_PROJECT")

    import datetime as _dt
    result = {"project": p, "dataset": d,
              "window": {"start": args.start, "end": args.end},
              "runs_per_shape": args.runs,
              "query_cache": "disabled",
              "table_rows_verified": n_rows,
              "bi_engine_observed": bi_obs,
              "reservation_assignments_observed": res_obs,
              "bindings": {
                  "scenario": manifest["name"],
                  "scenario_manifest_sha256": sh(args.scenario_manifest),
                  "seed_sha256": manifest["seed"]["ndjson_sha256"],
                  "benchmark_tool_sha256": sh(__file__),
                  "template_sql_sha256": sh(pathlib.Path(root) / "sql"
                                            / "events_v1.template.sql"),
                  "repo_commit": commit,
                  "executed_at_utc": _dt.datetime.now(
                      _dt.timezone.utc).isoformat(),
              },
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
    types = ", ".join(f"'{v.upper()}'" for v in V1_VIEWS)
    base_probe = (f"SELECT COUNT(*), COUNT(DISTINCT trace_id) FROM "
                  f"(SELECT {union_cols} FROM `{p}.{d}.agent_events` "
                  f"WHERE event_type IN ({types})) "
                  f"WHERE {w}")
    ub = run_cold(client, union_probe, 3)
    bb = run_cold(client, base_probe, 3)
    ratio = ub[0]["bytes_processed"] / bb[0]["bytes_processed"]
    result["gates"]["structural_union"] = {
        "union_job_ids": [s["job_id"] for s in ub],
        "base_job_ids": [s["job_id"] for s in bb],
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
    boundary_job = client.query(f"""
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
    """)
    bq_rows = list(boundary_job.result())
    row = dict(bq_rows[0])
    row["boundary_job_id"] = boundary_job.job_id
    # Falsifiability (seventh review): the seed plants rows dated end+1
    # (boundary_after_end_rows), so after-end exclusion can actually fail.
    planted = manifest["seed"]["fixture_counters"][
        "boundary_after_end_rows"]
    before = list(client.query(f"""
        SELECT
          (SELECT COUNT(*) FROM ({union_sql(p, d)})
           WHERE DATE(timestamp,'UTC') <
             DATE '{args.start}') AS before_start_rows,
          (SELECT COUNT(*) FROM ({union_sql(p, d)})
           WHERE DATE(timestamp,'UTC') = DATE '{args.start}')
             AS start_day_rows
    """).result())
    brow = dict(before[0])
    row.update(brow)
    boundary_pass = (row["in_window"] == row["raw_in_window"]
                     and row["end_day_rows"] > 0
                     and row["start_day_rows"] > 0
                     and row["before_start_rows"] > 0
                     and row["after_end_rows"] == planted
                     and planted > 0)
    # The PRODUCTION template, with the actual Looker Studio YYYYMMDD
    # date parameters, must return exactly the in-window count.
    tmpl = open(pathlib.Path(root) / "sql"
                / "events_v1.template.sql").read()
    bindings = __import__("yaml").safe_load(
        open(pathlib.Path(root) / "bindings"
             / "template_bindings.yaml"))["placeholders"]
    tmpl = (tmpl.replace(bindings["PROJECT"], p)
                .replace(bindings["DATASET"], d)
                .replace(bindings["VIEW_PREFIX"], "v"))
    job = client.query(
        f"SELECT COUNT(*) AS n FROM ({tmpl})",
        job_config=bigquery.QueryJobConfig(
            use_query_cache=False,
            query_parameters=[
                bigquery.ScalarQueryParameter(
                    "DS_START_DATE", "STRING",
                    args.start.replace("-", "")),
                bigquery.ScalarQueryParameter(
                    "DS_END_DATE", "STRING", args.end.replace("-", "")),
            ]))
    tmpl_n = int(list(job.result())[0]["n"])
    row["production_template_in_window"] = tmpl_n
    row["production_template_job_id"] = job.job_id
    boundary_pass = boundary_pass and tmpl_n == row["in_window"]
    result["gates"]["date_boundary"] = {**row, "planted_after_end": planted,
                                        "pass": boundary_pass}
    print(f"date boundary: {row} planted={planted} "
          f"({'PASS' if boundary_pass else 'FAIL'})")

    # --- partition-pruning proof ---
    one_day = DATE_PRED.format(start=args.end, end=args.end)
    narrow = run_cold(client, f"SELECT COUNT(*) FROM ({union_sql(p, d)}) "
                              f"WHERE {one_day}", 1)
    wide = result["shapes"]["scorecard_total_events_union"]
    frac = narrow[0]["bytes_processed"] / wide["bytes_processed"]
    result["gates"]["partition_pruning"] = {
        "job_id": narrow[0]["job_id"],
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
    # Hard gates fail the process (seventh review: a failing gate must
    # never exit 0).
    failed = [k for k, v in result["gates"].items() if not v["pass"]]
    if failed:
        print(f"GATES FAILED: {failed}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
