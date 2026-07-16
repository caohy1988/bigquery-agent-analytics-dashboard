#!/usr/bin/env python3
"""Oracle runner: execute the 37 oracle queries against a fixture dataset
and record or compare deterministic expected results.

Modes:
  run     — execute every query, write one JSON result file per chart into
            --outdir (these become the committed expected results when run
            against the profile that defines the scenario).
  compare — execute every query and compare against --expected files:
            exact equality for counts/distinct counts/sums/averages;
            declared relative tolerance for percentile charts.

Date windows follow each tile's dashboard default (Usage 14 days,
Performance 7 days) anchored to the scenario's end date, matching the
report's default ranges.

SQL is fed to bq via stdin (query text begins with `--` comment lines that
bq would otherwise parse as flags).
"""

import argparse
import json
import pathlib
import subprocess
import sys

import yaml

SCENARIO_END = "2026-07-15"  # seed generator's fixed end date

PERCENTILE_TOLERANCE = 0.0  # PERCENTILE_CONT is deterministic on BigQuery;
# keep a declared knob for the contract, default exact.

# Floating-point aggregates (AVG, SAFE_DIVIDE change ratios) vary in the
# final ulps across runs/datasets because BigQuery's summation order is
# nondeterministic (observed live: 4538.911743515851 vs 4538.9117435158505
# on identical rows). This epsilon absorbs IEEE ordering noise ONLY — it is
# not a semantic tolerance and is far below any displayed precision (the
# block renders averages at one decimal).
FLOAT_ORDERING_EPSILON = 1e-9


def window_for(chart: dict) -> tuple:
    days = 7 if chart["source_dashboard"] == "performance" else 14
    import datetime as dt
    end = dt.date.fromisoformat(SCENARIO_END)
    start = end - dt.timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def run_query(sql_path: str, project: str, dataset: str, prefix: str,
              location: str, start: str, end: str) -> list:
    sql = open(sql_path).read()
    sql = (sql.replace("{{PROJECT}}", project)
              .replace("{{DATASET}}", dataset)
              .replace("{{VIEW_PREFIX}}", prefix))
    cmd = ["bq", f"--project_id={project}", f"--location={location}",
           "query", "--nouse_legacy_sql", "--format=json",
           "--max_rows=100000",
           f"--parameter=start_date:DATE:{start}",
           f"--parameter=end_date:DATE:{end}",
           "--parameter=filter_agent:STRING:",
           "--parameter=filter_user_id:STRING:",
           "--parameter=filter_trace_id:STRING:",
           "--parameter=filter_span_id:STRING:",
           "--parameter=filter_tool_name:STRING:"]
    p = subprocess.run(cmd, input=sql, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{sql_path}: {p.stderr.strip()[:400]}")
    return json.loads(p.stdout or "[]")


def canon(rows: list) -> list:
    """Canonical row order for queries without a total ORDER BY."""
    return sorted(rows, key=lambda r: json.dumps(r, sort_keys=True))


def values_equal(a, b, tol: float) -> bool:
    if a == b:
        return True
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return False
    if fa == fb:
        return True
    # Integers must match exactly; only float-typed values get the IEEE
    # ordering epsilon (or the declared percentile tolerance if larger).
    if fa == int(fa) and fb == int(fb) and "." not in str(a) + str(b):
        return False
    eff = max(tol, FLOAT_ORDERING_EPSILON)
    if fb != 0:
        return abs(fa - fb) / abs(fb) <= eff
    return abs(fa) <= eff


def rows_equal(got: list, want: list, tol: float) -> bool:
    if len(got) != len(want):
        return False
    for g, w in zip(canon(got), canon(want)):
        if set(g) != set(w):
            return False
        for k in g:
            if not values_equal(g[k], w[k], tol):
                return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["run", "compare"])
    ap.add_argument("--project", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--prefix", default="v")
    ap.add_argument("--location", default="US")
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--spec", default="spec/dashboard_spec.yaml")
    ap.add_argument("--queries", default="oracle/queries")
    ap.add_argument("--outdir", default=None,
                    help="run mode: where result files are written")
    ap.add_argument("--expected", default=None,
                    help="compare mode: directory of expected result files")
    args = ap.parse_args()

    spec = yaml.safe_load(open(args.spec))
    charts = spec["charts"]
    failures = []
    for c in charts:
        cid = c["id"]
        start, end = window_for(c)
        rows = run_query(f"{args.queries}/{cid}.sql", args.project,
                         args.dataset, args.prefix, args.location,
                         start, end)
        record = {
            "chart_id": cid,
            "scenario": args.scenario,
            "window": {"start_date": start, "end_date": end},
            "comparison": ("percentile_tolerance" if c["percentile"]
                           else "exact"),
            "tolerance": (PERCENTILE_TOLERANCE if c["percentile"]
                          else None),
            "rows": rows,
        }
        if args.mode == "run":
            out = pathlib.Path(args.outdir)
            out.mkdir(parents=True, exist_ok=True)
            with open(out / f"{cid}.json", "w") as fh:
                json.dump(record, fh, indent=1, sort_keys=True)
                fh.write("\n")
            print(f"ran {cid}: {len(rows)} rows")
        else:
            exp = json.load(open(f"{args.expected}/{cid}.json"))
            tol = exp.get("tolerance") or 0.0
            if exp["window"] != record["window"]:
                failures.append(f"{cid}: window mismatch")
            elif not rows_equal(rows, exp["rows"], tol):
                failures.append(f"{cid}: result mismatch "
                                f"({len(rows)} vs {len(exp['rows'])} rows)")
            else:
                print(f"match {cid}")

    if args.mode == "compare":
        if failures:
            for f in failures:
                print(f"FAIL: {f}", file=sys.stderr)
            return 1
        print(f"compare OK: all {len(charts)} charts match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
