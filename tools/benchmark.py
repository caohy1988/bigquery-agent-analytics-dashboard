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

Capacity contract (ninth review): the reference is on-demand with BI
Engine disabled. Capacity state is verified fail-closed — an unreadable
INFORMATION_SCHEMA, a nonempty BI Engine capacity, a reservation
assignment, or an accelerated/reservation-backed job aborts the run.
Only sanitized counts and job ids are recorded, never raw capacity rows.

Dataset binding (ninth review): --load-receipt (from tools/load_seed.py)
is required; the receipt's load job is re-fetched from BigQuery and must
match the scenario manifest's seed sha and row count, and the live
table's per-event-type counts must equal the manifest's.

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

TOK = ("COALESCE("
       "SAFE_CAST(JSON_VALUE(usage_metadata, '$.total_token_count')"
       " AS INT64), "
       "SAFE_CAST(JSON_VALUE(usage_metadata, '$.total_tokens') AS INT64), "
       "usage_total_tokens)")


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


class CapacityViolation(RuntimeError):
    pass


def job_capacity_state(job):
    """(bi_engine_accelerated, reservation_used) from job statistics."""
    st = job._properties.get("statistics", {})
    bi = st.get("query", {}).get("biEngineStatistics")
    accelerated = bool(bi) and bi.get("biEngineMode") not in (None,
                                                             "DISABLED")
    return accelerated, bool(st.get("reservation_id"))


def run_cold(client, sql, runs):
    stats = []
    for _ in range(runs):
        job = client.query(sql, job_config=bigquery.QueryJobConfig(
            use_query_cache=False))
        job.result()
        accelerated, reserved = job_capacity_state(job)
        if accelerated:
            raise CapacityViolation(
                f"job {job.job_id} was BI Engine accelerated — the "
                "contract requires an on-demand reference with BI Engine "
                "disabled")
        if reserved:
            raise CapacityViolation(
                f"job {job.job_id} ran on a reservation — the contract "
                "requires on-demand pricing")
        stats.append({
            "job_id": job.job_id,
            "bytes_processed": job.total_bytes_processed,
            "bytes_billed": job.total_bytes_billed,
            "slot_ms": job.slot_millis,
            "elapsed_ms": int((job.ended - job.started).total_seconds()
                              * 1000),
            "bi_engine_accelerated": False,
            "reservation_used": False,
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
    ap.add_argument("--load-receipt", required=True,
                    help="Receipt written by tools/load_seed.py binding "
                         "the table to the manifest's seed")
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

    # Load verification: the queried table must BE the manifest's seed,
    # proven by the load receipt (not just a row count — a different
    # 10M-row table would otherwise pass; ninth review).
    receipt = json.load(open(args.load_receipt))
    total = manifest["seed"]["total_emitted"]
    if receipt.get("ndjson_sha256") != manifest["seed"]["ndjson_sha256"]:
        print("ERROR: load receipt seed sha != scenario manifest seed sha",
              file=sys.stderr)
        return 1
    if receipt.get("destination_table") != f"{p}.{d}.agent_events":
        print(f"ERROR: load receipt destination "
              f"{receipt.get('destination_table')} is not "
              f"{p}.{d}.agent_events", file=sys.stderr)
        return 1
    if receipt.get("output_rows") != total:
        print("ERROR: load receipt row count != manifest total_emitted",
              file=sys.stderr)
        return 1
    load_job = client.get_job(receipt["load_job_id"],
                              location=receipt.get("load_job_location"))
    if (load_job.job_type != "load" or load_job.state != "DONE"
            or load_job.error_result
            or load_job.output_rows != receipt["output_rows"]
            or str(load_job.destination).replace(":", ".")
            != receipt["destination_table"]):
        print(f"ERROR: BigQuery load job {receipt['load_job_id']} does "
              "not corroborate the receipt", file=sys.stderr)
        return 1
    counts_job = client.query(
        f"SELECT event_type, COUNT(*) AS n, "
        f"AVG(BYTE_LENGTH(TO_JSON_STRING(content))) AS content_avg, "
        f"AVG(BYTE_LENGTH(TO_JSON_STRING(attributes))) AS attrs_avg "
        f"FROM `{p}.{d}.agent_events` GROUP BY event_type")
    dist = {r["event_type"]: r for r in counts_job.result()}
    et_counts = {k: int(v["n"]) for k, v in dist.items()}
    n_rows = sum(et_counts.values())
    if n_rows != total:
        print(f"ERROR: table has {n_rows} rows but the scenario manifest "
              f"binds a seed of {total} — wrong or partial load",
              file=sys.stderr)
        return 1
    if et_counts != manifest["seed"]["by_event_type"]:
        print("ERROR: live per-event-type counts != scenario manifest "
              f"by_event_type\n  live:     {et_counts}\n  manifest: "
              f"{manifest['seed']['by_event_type']}", file=sys.stderr)
        return 1
    load_verification = {
        "load_receipt": receipt,
        "load_job_verified": True,
        "table_rows_verified": n_rows,
        "row_count_job_id": counts_job.job_id,
        "event_type_counts": et_counts,
        "payload_avg_bytes_by_event_type": {
            k: {"content": round(float(v["content_avg"] or 0), 1),
                "attributes": round(float(v["attrs_avg"] or 0), 1)}
            for k, v in dist.items()},
    }

    # Capacity state, fail-closed and sanitized (ninth review): raw
    # INFORMATION_SCHEMA rows can carry project/reservation/principal
    # identifiers and must never enter publishable evidence.
    def count_rows(sql):
        job = client.query(sql)
        return len(list(job.result())), job.job_id
    try:
        bi_n, bi_job = count_rows(
            "SELECT * FROM `region-us`.INFORMATION_SCHEMA.BI_CAPACITIES")
        res_n, res_job = count_rows(
            "SELECT * FROM `region-us`.INFORMATION_SCHEMA."
            "ASSIGNMENTS_BY_PROJECT")
    except Exception as e:
        print(f"ERROR: could not establish capacity state "
              f"({type(e).__name__}) — the contract requires a verified "
              "on-demand reference; failing closed", file=sys.stderr)
        return 1
    if bi_n or res_n:
        print(f"ERROR: incompatible capacity state — {bi_n} BI Engine "
              f"capacit(ies), {res_n} reservation assignment(s); the "
              "contract requires on-demand with BI Engine disabled",
              file=sys.stderr)
        return 1
    capacity = {"bi_engine_capacities": 0, "reservation_assignments": 0,
                "bi_capacities_job_id": bi_job,
                "assignments_job_id": res_job, "verified": True}

    import datetime as _dt
    result = {"project": p, "dataset": d,
              "window": {"start": args.start, "end": args.end},
              "runs_per_shape": args.runs,
              "query_cache": "disabled",
              "load_verification": load_verification,
              "capacity": capacity,
              "bindings": {
                  "scenario": manifest["name"],
                  "scenario_manifest_sha256": sh(args.scenario_manifest),
                  "seed_sha256": manifest["seed"]["ndjson_sha256"],
                  "load_receipt_sha256": sh(args.load_receipt),
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
    before_job = client.query(f"""
        SELECT
          (SELECT COUNT(*) FROM ({union_sql(p, d)})
           WHERE DATE(timestamp,'UTC') <
             DATE '{args.start}') AS before_start_rows,
          (SELECT COUNT(*) FROM ({union_sql(p, d)})
           WHERE DATE(timestamp,'UTC') = DATE '{args.start}')
             AS start_day_rows
    """)
    brow = dict(list(before_job.result())[0])
    row.update(brow)
    row["before_job_id"] = before_job.job_id
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
    try:
        sys.exit(main())
    except CapacityViolation as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
