#!/usr/bin/env python3
"""Oracle runner: execute the 37 oracle queries against a fixture dataset
and record or compare expected results with enforced provenance.

Comparison policy (declared here, applied by the comparator — never by
altering oracle query semantics):
  * DIMENSIONS compare lexically — "001" != "1", "1e1" != "10";
  * integer MEASURES compare exactly (string-parsed, no float coercion);
  * float MEASURES compare at CANONICAL_FLOAT_DECIMALS (quantized
    ROUND_HALF_EVEN) — the declared canonical precision absorbing
    BigQuery's cross-run IEEE summation-order noise; the oracle SQL stays
    faithful to the pinned LookML (raw AVG/PERCENTILE);
  * percentile charts additionally apply their DECLARED tolerance
    (relative or absolute) after quantization; tolerance 0.0 means
    equality at canonical precision.

Provenance enforcement in compare mode: the expected file must carry the
CURRENT runner/generator/spec hashes, a clean repo commit, the declared
scenario-manifest sha, the recording profile's committed inventory
fingerprint, and a BigQuery job id; the chart's declared expected-result
matrix (scenario, profiles, path) must license this comparison. Evidence
from any earlier tool revision fails closed.
"""

import argparse
import datetime
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import uuid
from decimal import ROUND_HALF_EVEN, Decimal

import yaml

CANONICAL_FLOAT_DECIMALS = 6
QUANT = Decimal(1).scaleb(-CANONICAL_FLOAT_DECIMALS)
INT_RE = re.compile(r"^-?\d+$")
FILTER_PARAMS = ("filter_agent", "filter_user_id", "filter_trace_id",
                 "filter_span_id", "filter_tool_name")
CONTROL_TO_PARAM = {"Agent": "filter_agent", "User ID": "filter_user_id",
                    "Trace ID": "filter_trace_id",
                    "Span ID": "filter_span_id",
                    "Tool Name": "filter_tool_name"}


def sha256_file(path) -> str:
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
              location: str, start: str, end: str, filters: dict) -> tuple:
    sql = open(sql_path).read()
    sql = (sql.replace("{{PROJECT}}", project)
              .replace("{{DATASET}}", dataset)
              .replace("{{VIEW_PREFIX}}", prefix))
    # Explicit job id: synchronous bq output carries no job reference, and
    # evidence without job-level INFORMATION_SCHEMA.JOBS auditability is
    # invalid (PR #1 sixth review).
    job_id = f"oracle_{uuid.uuid4().hex}"
    cmd = ["bq", f"--project_id={project}", f"--location={location}",
           "query", "--nouse_legacy_sql", "--format=json",
           "--max_rows=100000", f"--job_id={job_id}",
           f"--parameter=start_date:DATE:{start}",
           f"--parameter=end_date:DATE:{end}"]
    for p in FILTER_PARAMS:
        vals = filters.get(p, [])
        cmd.append(f"--parameter={p}:ARRAY<STRING>:" + json.dumps(vals))
    proc = subprocess.run(cmd, input=sql, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{sql_path}: {proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout or "[]"), job_id


def values_equal(a, b, kind: str, comparison: str, tolerance,
                 tolerance_kind) -> bool:
    if a is None or b is None:
        return a is b or a == b
    sa, sb = str(a), str(b)
    if kind == "dimension":
        return sa == sb
    if comparison == "exact" and INT_RE.match(sa) and INT_RE.match(sb):
        return int(sa) == int(sb)
    try:
        da, db = Decimal(sa), Decimal(sb)
    except Exception:
        return sa == sb
    da = da.quantize(QUANT, rounding=ROUND_HALF_EVEN)
    db = db.quantize(QUANT, rounding=ROUND_HALF_EVEN)
    if comparison == "exact":
        return da == db
    tol = Decimal(str(tolerance))
    if tolerance_kind == "absolute":
        return abs(da - db) <= tol
    if db == 0:
        return abs(da) <= tol
    return abs(da - db) / abs(db) <= tol


def rows_equal(got: list, want: list, field_kinds: dict, comparison: str,
               tolerance, tolerance_kind) -> str:
    """Ordered, field-role-aware comparison. Returns '' or a reason."""
    if len(got) != len(want):
        return f"row count {len(got)} != {len(want)}"
    for i, (g, w) in enumerate(zip(got, want)):
        if set(g) != set(w):
            return f"row {i}: column sets differ"
        for k in g:
            kind = field_kinds.get(k, "measure")
            if not values_equal(g[k], w[k], kind, comparison, tolerance,
                                tolerance_kind):
                return f"row {i}: {k} ({kind}): {g[k]!r} != {w[k]!r}"
    return ""


def field_kinds_for(chart: dict) -> dict:
    kinds = {}
    for d in chart["dimensions"]:
        kinds[d.split(".", 1)[1]] = "dimension"
    for m in chart["measures"]:
        kinds[m.split(".", 1)[1]] = "measure"
    return kinds


def declared_entry(chart: dict, scenario: str, profile: str,
                   expected_dir) -> tuple:
    """The chart's declared expected-result matrix must license this
    scenario/profile/path combination."""
    for er in chart.get("expected_results") or []:
        if er["scenario"] == scenario:
            if profile not in er["profiles"]:
                return None, (f"profile {profile} not declared for "
                              f"scenario {scenario}")
            want = str(pathlib.Path(er["path"]))
            have = str(pathlib.Path(expected_dir) / f"{chart['id']}.json")
            if want != have:
                return None, f"declared path {want} != {have}"
            return er, ""
    return None, f"scenario {scenario} not declared in expected_results"


def verify_expected(exp: dict, chart: dict, scenario: str, window: dict,
                    filters: dict, manifest_sha: str,
                    current: dict) -> list:
    """Every provenance and declaration field must match — expected files
    from any earlier tool revision fail closed. Unit-tested by
    tools/test_runner_compare.py mutations."""
    problems = []
    if exp.get("chart_id") != chart["id"]:
        problems.append("chart_id mismatch")
    if exp.get("scenario") != scenario:
        problems.append("scenario mismatch")
    if exp.get("comparison") != current["comparison"]:
        problems.append("comparison mode mismatch")
    if exp.get("tolerance") != current["tolerance"] or \
            exp.get("tolerance_kind") != current["tolerance_kind"]:
        problems.append("tolerance declaration mismatch")
    if exp.get("window") != window:
        problems.append("window mismatch")
    if exp.get("filters", {}) != filters:
        problems.append("filter set mismatch")
    b = exp.get("bindings") or {}
    checks = [
        ("scenario_manifest_sha256", manifest_sha),
        ("query_sha256", current["query_sha256"]),
        ("runner_sha256", current["runner_sha256"]),
        ("generator_sha256", current["generator_sha256"]),
        ("spec_sha256", current["spec_sha256"]),
        ("inventory_fingerprint", current["recording_fingerprint"]),
    ]
    for key, want in checks:
        if b.get(key) != want:
            problems.append(f"bindings.{key} mismatch")
    if b.get("profile") != current["recording_profile"]:
        problems.append("bindings.profile mismatch")
    if not b.get("job_id"):
        problems.append("bindings.job_id absent")
    if "+dirty" in (b.get("repo_commit") or "") or not b.get("repo_commit"):
        problems.append("bindings.repo_commit dirty or absent")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["run", "compare"])
    ap.add_argument("--project", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--prefix", default="v")
    ap.add_argument("--location", default="US")
    ap.add_argument("--profile", required=True,
                    choices=["1.27.0", "1.36.1", "2.4.0"])
    ap.add_argument("--recording-profile", default="1.27.0",
                    help="Profile the expected results were recorded on")
    ap.add_argument("--scenario-manifest", required=True)
    ap.add_argument("--spec", default="spec/dashboard_spec.yaml")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--expected", default=None)
    ap.add_argument("--receipt-out", default=None)
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
    filters = {}
    for control, vals in (manifest.get("filters") or {}).items():
        if control not in CONTROL_TO_PARAM:
            raise SystemExit(f"scenario filter on unknown control "
                             f"{control!r}")
        filters[CONTROL_TO_PARAM[control]] = vals

    def fingerprint(profile):
        return json.load(open(
            f"evidence/inventories/adk-{profile}.json"))[
                "fingerprint_sha256"]

    spec_sha = sha256_file(args.spec)
    spec = yaml.safe_load(open(args.spec))
    charts = spec["charts"]
    runner_sha = sha256_file(__file__)
    gen_sha = sha256_file(str(pathlib.Path(__file__).parent
                               / "gen_oracle.py"))

    failures, receipt_charts = [], {}
    for c in charts:
        cid = c["id"]
        start, end = window_for(c, manifest["seed"]["end_date"])
        window = {"start_date": start, "end_date": end}
        target_dir = args.outdir if args.mode == "run" else args.expected
        er, why = declared_entry(c, scenario, args.profile, target_dir)
        if er is None:
            failures.append(f"{cid}: declaration: {why}")
            receipt_charts[cid] = {"pass": False, "detail": why,
                                   "job_id": None}
            continue
        qpath = c["oracle_query"]
        rows, job_id = run_query(qpath, args.project, args.dataset,
                                 args.prefix, args.location, start, end,
                                 filters)
        comparison = er["comparison"]
        tolerance = er.get("tolerance")
        tolerance_kind = er.get("tolerance_kind")
        current = {
            "comparison": comparison, "tolerance": tolerance,
            "tolerance_kind": tolerance_kind,
            "query_sha256": sha256_file(qpath),
            "runner_sha256": runner_sha, "generator_sha256": gen_sha,
            "spec_sha256": spec_sha,
            "recording_profile": args.recording_profile,
            "recording_fingerprint": fingerprint(args.recording_profile),
        }
        if args.mode == "run":
            record = {
                "chart_id": cid, "scenario": scenario, "window": window,
                "filters": filters, "comparison": comparison,
                "tolerance": tolerance, "tolerance_kind": tolerance_kind,
                "rows": rows,
                "bindings": {
                    "scenario_manifest_sha256": manifest_sha,
                    "seed_sha256": manifest["seed"]["ndjson_sha256"],
                    "profile": args.profile,
                    "inventory_fingerprint": fingerprint(args.profile),
                    "query_sha256": current["query_sha256"],
                    "generator_sha256": gen_sha,
                    "runner_sha256": runner_sha,
                    "spec_sha256": spec_sha,
                    "repo_commit": commit,
                    "job_id": job_id,
                    "executed_at_utc": datetime.datetime.now(
                        datetime.timezone.utc).isoformat(),
                },
            }
            out = pathlib.Path(args.outdir)
            out.mkdir(parents=True, exist_ok=True)
            with open(out / f"{cid}.json", "w") as fh:
                json.dump(record, fh, indent=1, sort_keys=True)
                fh.write("\n")
            print(f"ran {cid}: {len(rows)} rows [{job_id}]")
        else:
            exp = json.load(open(pathlib.Path(args.expected)
                                 / f"{cid}.json"))
            problems = verify_expected(exp, c, scenario, window, filters,
                                       manifest_sha, current)
            reason = "; ".join(problems)
            if not reason:
                reason = rows_equal(rows, exp["rows"], field_kinds_for(c),
                                    comparison, tolerance, tolerance_kind)
            receipt_charts[cid] = {"pass": not reason,
                                   "detail": reason or "match",
                                   "job_id": job_id}
            if reason:
                failures.append(f"{cid}: {reason}")
            else:
                print(f"match {cid}")

    if args.mode == "compare":
        receipt = {
            "scenario": scenario,
            "scenario_manifest_sha256": manifest_sha,
            "profile": args.profile,
            "dataset": f"{args.project}.{args.dataset}",
            "inventory_fingerprint": fingerprint(args.profile),
            "runner_sha256": runner_sha,
            "spec_sha256": spec_sha,
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
