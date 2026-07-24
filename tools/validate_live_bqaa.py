#!/usr/bin/env python3
"""Compile and execute every dashboard oracle query on a live BQAA dataset.

This is a read-only smoke test for a real installation, not parity evidence:
fixture/profile certification remains the responsibility of oracle/runner.py.
The output is deliberately sanitized. It records query hashes, row counts,
and BigQuery job IDs, but never query results, source identifiers, prompts,
user IDs, traces, or error text.
"""

import argparse
import datetime
import hashlib
import json
import pathlib
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from oracle.runner import run_query, window_for  # noqa: E402

REQUIRED_VIEWS = {
    "user_message_received", "llm_request", "llm_response", "llm_error",
    "tool_starting", "tool_completed", "tool_error", "agent_starting",
    "agent_completed", "invocation_starting", "invocation_completed",
    "state_delta", "hitl_credential_request", "hitl_confirmation_request",
    "hitl_input_request",
}


def sha256_file(path: str) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--prefix", default="v")
    ap.add_argument("--location", default="US")
    ap.add_argument("--end-date", required=True,
                    help="Inclusive YYYY-MM-DD report end date")
    ap.add_argument("--spec", default="spec/dashboard_spec.yaml")
    ap.add_argument("--output",
                    help="Optional sanitized JSON execution receipt")
    args = ap.parse_args()

    # Parse up front so an invalid date fails before the first query.
    datetime.date.fromisoformat(args.end_date)
    spec = yaml.safe_load(open(args.spec))
    charts = spec["charts"]
    if len(charts) != 37:
        raise SystemExit(f"expected 37 charts, found {len(charts)}")

    # Execute the same source preflight used by the report. Results contain
    # only object/column diagnostics; do not include them in the receipt.
    preflight = (ROOT / "sql/preflight.sql.tmpl").read_text()
    preflight = (preflight.replace("{{PROJECT}}", args.project)
                  .replace("{{DATASET}}", args.dataset)
                  .replace("{{VIEW_PREFIX}}", args.prefix))
    pr = subprocess.run(
        ["bq", f"--project_id={args.project}",
         f"--location={args.location}", "query", "--nouse_legacy_sql",
         "--format=json", "--max_rows=10000"],
        input=preflight, capture_output=True, text=True)
    if pr.returncode != 0:
        raise SystemExit(f"preflight execution failed: {pr.stderr[:400]}")
    problems = json.loads(pr.stdout or "[]")
    if problems:
        raise SystemExit(
            f"preflight found {len(problems)} incompatible source objects")

    executed = []
    failures = []
    for chart in charts:
        start, end = window_for(chart, args.end_date)
        qpath = chart["oracle_query"]
        try:
            rows, job_id = run_query(
                qpath, args.project, args.dataset, args.prefix,
                args.location, start, end, {})
        except Exception as exc:
            failures.append(f"{chart['id']}: {exc}")
            print(f"FAIL {chart['id']}", file=sys.stderr)
            continue
        executed.append({
            "chart_id": chart["id"],
            "query_sha256": sha256_file(qpath),
            "row_count": len(rows),
            "job_id": job_id,
            "window_days": 7 if chart["source_dashboard"] == "performance"
            else 14,
        })
        print(f"OK {chart['id']}: {len(rows)} rows [{job_id}]", flush=True)

    receipt = {
        "kind": "sanitized-live-bqaa-smoke-test",
        "semantic_scope": (
            "read-only execution/compatibility; not fixture parity "
            "certification"),
        "executed_at_utc": datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
        "report_end_date": args.end_date,
        "view_prefix": args.prefix,
        "required_view_count": len(REQUIRED_VIEWS),
        "spec_sha256": sha256_file(args.spec),
        "charts_expected": 37,
        "charts_succeeded": len(executed),
        "charts_failed": len(failures),
        "charts": executed,
        "pass": not failures and len(executed) == 37,
    }
    if args.output:
        out = pathlib.Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n")
        print(f"sanitized receipt: {out}")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("live BQAA validation OK: 37/37 oracle queries executed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
