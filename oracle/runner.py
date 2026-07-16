#!/usr/bin/env python3
"""Oracle runner: execute the 37 oracle queries against a fixture dataset
and record or compare expected results with full provenance bindings.

Modes:
  run     — execute every query under a scenario manifest, writing one
            result file per chart with bindings: scenario-manifest sha,
            profile + inventory fingerprint, query/generator/runner file
            hashes, repo commit, BigQuery job IDs, execution timestamp.
  compare — re-execute and compare against --expected: verifies the
            expected file's chart_id/scenario/comparison/tolerance metadata
            against the manifest and spec BEFORE comparing rows; ordered,
            type-aware comparison (integers exact via string parse — no
            float coercion; decimals exact for `exact` mode; declared
            relative or absolute tolerance for `percentile_tolerance`).
            Writes a per-profile compare receipt under evidence/.

Scenario manifests (oracle/scenarios/<name>.json) bind the seed artifact
(sha256, args, fixture counters) and optional control filter values
(ARRAY<STRING> parameters, matching the multi-value dashboard controls).

Evidence discipline: refuses to run from a dirty git tree unless
--allow-dirty (results stamped with a commit must be reproducible from it).
"""

import argparse
import datetime
import hashlib
import json
import pathlib
import re
import subprocess
import sys
from decimal import Decimal

import yaml

INT_RE = re.compile(r"^-?\d+$")
FILTER_PARAMS = ("filter_agent", "filter_user_id", "filter_trace_id",
                 "filter_span_id", "filter_tool_name")
CONTROL_TO_PARAM = {"Agent": "filter_agent", "User ID": "filter_user_id",
                    "Trace ID": "filter_trace_id",
                    "Span ID": "filter_span_id",
                    "Tool Name": "filter_tool_name"}


def sha256_file(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def repo_state(allow_dirty: bool) -> str:
    root = str(pathlib.Path(__file__).resolve().parent.parent)
    commit = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                            capture_output=True, text=True,
                            check=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", root, "status", "--porcelain",
                        "--untracked-files=no"],
                           capture_output=True, text=True,
                           check=True).stdout.strip()
    # Untracked files are ignored: evidence outputs written earlier in
    # the same pipeline run are new files, not code changes. Any MODIFIED
    # tracked file invalidates provenance.
    if dirty and not allow_dirty:
        raise SystemExit("refusing to produce evidence with modified "
                         "tracked files (commit first, or pass "
                         "--allow-dirty for dev runs that must not be "
                         "committed)")
    return commit + ("+dirty" if dirty else "")


def window_for(chart: dict, scenario_end: str) -> tuple:
    days = 7 if chart["source_dashboard"] == "performance" else 14
    end = datetime.date.fromisoformat(scenario_end)
    start = end - datetime.timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def run_query(sql_path: str, project: str, dataset: str, prefix: str,
              location: str, start: str, end: str,
              filters: dict) -> tuple:
    sql = open(sql_path).read()
    sql = (sql.replace("{{PROJECT}}", project)
              .replace("{{DATASET}}", dataset)
              .replace("{{VIEW_PREFIX}}", prefix))
    job_id_line = []
    cmd = ["bq", f"--project_id={project}", f"--location={location}",
           "query", "--nouse_legacy_sql", "--format=json",
           "--max_rows=100000",
           f"--parameter=start_date:DATE:{start}",
           f"--parameter=end_date:DATE:{end}"]
    for p in FILTER_PARAMS:
        vals = filters.get(p, [])
        cmd.append(f"--parameter=filter_{p[7:]}:ARRAY<STRING>:"
                   + json.dumps(vals))
    proc = subprocess.run(cmd, input=sql, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{sql_path}: {proc.stderr.strip()[:400]}")
    m = re.search(r"bqjob_[a-z0-9_]+", proc.stderr or "")
    job_id_line.append(m.group(0) if m else None)
    return json.loads(proc.stdout or "[]"), job_id_line[0]


def values_equal(a, b, comparison: str, tolerance, tolerance_kind) -> bool:
    """Type-aware comparison. Integers never pass through float (so
    9007199254740992 != 9007199254740993). Exact mode = exact decimal
    equality (float aggregates are ROUNDed inside the oracle SQL, so no
    epsilon is needed or applied)."""
    if a is None or b is None:
        return a is b or a == b
    sa, sb = str(a), str(b)
    if comparison == "exact" and INT_RE.match(sa) and INT_RE.match(sb):
        return int(sa) == int(sb)
    try:
        da, db = Decimal(sa), Decimal(sb)
    except ArithmeticError:
        return sa == sb
    except Exception:
        return sa == sb
    if comparison == "exact":
        return da == db
    tol = Decimal(str(tolerance))
    if tolerance_kind == "absolute":
        return abs(da - db) <= tol
    if db == 0:
        return abs(da) <= tol
    return abs(da - db) / abs(db) <= tol


def rows_equal(got: list, want: list, comparison: str, tolerance,
               tolerance_kind) -> str:
    """Ordered comparison — every oracle query has a total ORDER BY, and
    output order is part of the parity contract. Returns '' or a reason."""
    if len(got) != len(want):
        return f"row count {len(got)} != {len(want)}"
    for i, (g, w) in enumerate(zip(got, want)):
        if set(g) != set(w):
            return f"row {i}: column sets differ"
        for k in g:
            if not values_equal(g[k], w[k], comparison, tolerance,
                                tolerance_kind):
                return f"row {i}: {k}: {g[k]!r} != {w[k]!r}"
    return ""


def scenario_filters(manifest: dict) -> dict:
    raw = manifest.get("filters") or {}
    out = {}
    for control, vals in raw.items():
        if control not in CONTROL_TO_PARAM:
            raise SystemExit(f"scenario filter on unknown control "
                             f"{control!r}")
        if not isinstance(vals, list) or not all(
                isinstance(v, str) for v in vals):
            raise SystemExit(f"scenario filter {control!r} must be a list "
                             "of strings")
        out[CONTROL_TO_PARAM[control]] = vals
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["run", "compare"])
    ap.add_argument("--project", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--prefix", default="v")
    ap.add_argument("--location", default="US")
    ap.add_argument("--profile", required=True,
                    choices=["1.27.0", "1.36.1", "2.4.0"])
    ap.add_argument("--scenario-manifest", required=True)
    ap.add_argument("--spec", default="spec/dashboard_spec.yaml")
    ap.add_argument("--queries", default="oracle/queries")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--expected", default=None)
    ap.add_argument("--receipt-out", default=None,
                    help="compare mode: where the machine-readable compare "
                         "receipt is written")
    ap.add_argument("--allow-dirty", action="store_true")
    args = ap.parse_args()

    if args.mode == "run" and not args.outdir:
        ap.error("run mode requires --outdir")
    if args.mode == "compare" and not (args.expected and args.receipt_out):
        ap.error("compare mode requires --expected and --receipt-out")

    commit = repo_state(args.allow_dirty)
    manifest = json.load(open(args.scenario_manifest))
    manifest_sha = sha256_file(args.scenario_manifest)
    scenario = manifest["name"]
    filters = scenario_filters(manifest)
    inv_path = f"evidence/inventories/adk-{args.profile}.json"
    inventory_fp = json.load(open(inv_path))["fingerprint_sha256"]

    spec = yaml.safe_load(open(args.spec))
    charts = spec["charts"]
    runner_sha = sha256_file(__file__)
    gen_sha = sha256_file(str(pathlib.Path(__file__).parent
                               / "gen_oracle.py"))

    failures, receipt_charts = [], {}
    for c in charts:
        cid = c["id"]
        start, end = window_for(c, manifest["seed"]["end_date"])
        qpath = f"{args.queries}/{cid}.sql"
        rows, job_id = run_query(qpath, args.project, args.dataset,
                                 args.prefix, args.location, start, end,
                                 filters)
        er_decl = next(e for e in c["expected_results"]
                       if e["scenario"] == manifest["base_scenario"]) \
            if c.get("expected_results") else None
        comparison = ("percentile_tolerance" if c["percentile"] else "exact")
        tolerance = (er_decl or {}).get("tolerance") if c["percentile"] \
            else None
        tolerance_kind = (er_decl or {}).get("tolerance_kind") \
            if c["percentile"] else None
        record = {
            "chart_id": cid,
            "scenario": scenario,
            "window": {"start_date": start, "end_date": end},
            "filters": filters,
            "comparison": comparison,
            "tolerance": tolerance,
            "tolerance_kind": tolerance_kind,
            "rows": rows,
            "bindings": {
                "scenario_manifest_sha256": manifest_sha,
                "seed_sha256": manifest["seed"]["ndjson_sha256"],
                "profile": args.profile,
                "inventory_fingerprint": inventory_fp,
                "query_sha256": sha256_file(qpath),
                "generator_sha256": gen_sha,
                "runner_sha256": runner_sha,
                "repo_commit": commit,
                "job_id": job_id,
                "executed_at_utc": datetime.datetime.now(
                    datetime.timezone.utc).isoformat(),
            },
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
            meta_problems = []
            if exp["chart_id"] != cid:
                meta_problems.append("chart_id mismatch")
            if exp["scenario"] != scenario:
                meta_problems.append("scenario mismatch")
            if exp["comparison"] != comparison:
                meta_problems.append("comparison mode mismatch")
            if exp.get("tolerance") != tolerance or \
                    exp.get("tolerance_kind") != tolerance_kind:
                meta_problems.append("tolerance declaration mismatch")
            if exp["window"] != record["window"]:
                meta_problems.append("window mismatch")
            if exp.get("filters", {}) != filters:
                meta_problems.append("filter set mismatch")
            if exp["bindings"]["scenario_manifest_sha256"] != manifest_sha:
                meta_problems.append("scenario manifest sha mismatch")
            if exp["bindings"]["query_sha256"] != record["bindings"][
                    "query_sha256"]:
                meta_problems.append("query file changed since recording")
            reason = ""
            if not meta_problems:
                reason = rows_equal(rows, exp["rows"], comparison,
                                    tolerance, tolerance_kind)
            verdict = "; ".join(meta_problems) or reason
            receipt_charts[cid] = {
                "pass": not verdict, "detail": verdict or "match",
                "job_id": job_id,
            }
            if verdict:
                failures.append(f"{cid}: {verdict}")
            else:
                print(f"match {cid}")

    if args.mode == "compare":
        receipt = {
            "scenario": scenario,
            "scenario_manifest_sha256": manifest_sha,
            "profile": args.profile,
            "dataset": f"{args.project}.{args.dataset}",
            "inventory_fingerprint": inventory_fp,
            "repo_commit": commit,
            "executed_at_utc": datetime.datetime.now(
                datetime.timezone.utc).isoformat(),
            "charts": receipt_charts,
            "pass": not failures,
        }
        pathlib.Path(args.receipt_out).parent.mkdir(parents=True,
                                                    exist_ok=True)
        with open(args.receipt_out, "w") as fh:
            json.dump(receipt, fh, indent=1, sort_keys=True)
            fh.write("\n")
        if failures:
            for f in failures:
                print(f"FAIL: {f}", file=sys.stderr)
            return 1
        print(f"compare OK: all {len(charts)} charts match "
              f"({args.profile}); receipt: {args.receipt_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
