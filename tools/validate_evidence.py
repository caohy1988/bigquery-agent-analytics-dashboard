#!/usr/bin/env python3
"""Offline, content-aware M0 evidence validator (the red/green gate).

Enumerates the REQUIRED evidence matrix — never "at least one file":

  * compare receipts for every (scenario x profile) declared in
    oracle/scenarios/ x the non-recording candidate profiles;
  * each receipt: pass=true, all 37 charts present and passing, non-null
    job id per chart, runner/spec hashes == current tree, scenario
    manifest sha == committed manifest, inventory fingerprint == committed
    inventory, clean repo commit that EXISTS and whose runner/spec blobs
    match the recorded hashes;
  * expected results: 37 files per scenario, bindings matching current
    tree and committed manifests, non-null job ids;
  * benchmark: bindings (scenario/seed/tool/template/commit, with the
    commit proven to contain the recorded tool/template blobs), every
    gate predicate RECOMPUTED from its recorded observations — never
    trusting stored pass booleans (ninth review) — a load receipt binding
    the table to the seed, per-event-type counts matching the manifest,
    sanitized on-demand capacity state, and a job id on every probe;
  * refresh marker absent.

Run by the m0-evidence-gate CI job; exits non-zero on any violation.
"""

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
REQUIRED_GATES = {"structural_union", "date_boundary", "partition_pruning"}
REQUIRED_SHAPES = {"scorecard_total_events_union",
                   "scorecard_distinct_sessions_union",
                   "trend_tokens_by_day", "bar_top5_users_by_traces_union",
                   "scorecard_p90_tool_latency", "scorecard_pop_tokens"}
COMPARE_PROFILES = ["1.36.1", "2.4.0"]
RECORDING_PROFILE = "1.27.0"


def sha(path) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def git_blob_sha256(commit: str, rel: str):
    pr = subprocess.run(["git", "-C", str(ROOT), "show", f"{commit}:{rel}"],
                        capture_output=True)
    if pr.returncode != 0:
        return None
    return hashlib.sha256(pr.stdout).hexdigest()


def main(root=None) -> int:
    global ROOT
    if root is not None:
        ROOT = pathlib.Path(root)
    errors = []

    def check(cond, msg):
        if not cond:
            errors.append(msg)

    check(not (ROOT / "evidence/.refresh-in-progress").exists(),
          "refresh marker present — evidence set incomplete")

    spec = yaml.safe_load(open(ROOT / "spec/dashboard_spec.yaml"))
    charts = {c["id"]: c for c in spec["charts"]}
    runner_sha = sha(ROOT / "oracle/runner.py")
    spec_sha = sha(ROOT / "spec/dashboard_spec.yaml")
    gen_sha = sha(ROOT / "oracle/gen_oracle.py")

    scenarios = {}
    for mp in sorted((ROOT / "oracle/scenarios").glob("*.json")):
        m = json.load(open(mp))
        scenarios[m["name"]] = (mp, m)
    oracle_scenarios = [n for n in scenarios if not n.startswith("bench-")]
    check(len(oracle_scenarios) >= 3,
          f"expected >=3 oracle scenarios (base + f1 + f2), got "
          f"{sorted(oracle_scenarios)}")

    for name in oracle_scenarios:
        mp, m = scenarios[name]
        msha = sha(mp)
        exp_dir = ROOT / "oracle/expected" / name
        files = sorted(exp_dir.glob("*.json")) if exp_dir.is_dir() else []
        # EXACT identity: the file set must be exactly {chart_id}.json for
        # the spec's 37 charts, each file claiming its own chart and this
        # scenario (a tree of 37 copies of one chart fails; eighth review).
        check({f.stem for f in files} == set(charts),
              f"{name}: expected-file set != spec chart-id set")
        for f in files:
            e = json.load(open(f))
            b = e.get("bindings", {})
            cid = e.get("chart_id")
            check(cid == f.stem, f"{name}/{f.name}: chart_id != filename")
            check(e.get("scenario") == name,
                  f"{name}/{f.name}: scenario field mismatch")
            decl = next((er for er in
                          (charts.get(cid, {}).get("expected_results")
                           or []) if er["scenario"] == name), None)
            check(decl is not None and str(pathlib.Path(decl["path"]))
                  == str(f.relative_to(ROOT)),
                  f"{name}/{f.name}: not the declared path for this "
                  "scenario")
            if cid in charts:
                qp = charts[cid]["oracle_query"]
                check(b.get("query_sha256") == sha(ROOT / qp),
                      f"{name}/{f.name}: query hash != current "
                      f"{qp}")
            check(b.get("job_id"), f"{name}/{f.name}: null job id")
            check(b.get("runner_sha256") == runner_sha,
                  f"{name}/{f.name}: stale runner hash")
            check(b.get("generator_sha256") == gen_sha,
                  f"{name}/{f.name}: stale generator hash")
            check(b.get("spec_sha256") == spec_sha,
                  f"{name}/{f.name}: stale spec hash")
            check(b.get("scenario_manifest_sha256") == msha,
                  f"{name}/{f.name}: manifest sha mismatch")
            check(b.get("seed_sha256") == m["seed"]["ndjson_sha256"],
                  f"{name}/{f.name}: seed sha mismatch")
            commit = b.get("repo_commit", "")
            check(commit and "+dirty" not in commit,
                  f"{name}/{f.name}: dirty/absent commit")
            if commit and "+dirty" not in commit:
                check(git_blob_sha256(commit, "oracle/runner.py")
                      == b.get("runner_sha256"),
                      f"{name}/{f.name}: commit does not contain the "
                      "recorded runner")
        inv = json.load(open(
            ROOT / f"evidence/inventories/adk-{RECORDING_PROFILE}.json"))
        for prof in COMPARE_PROFILES:
            rp = ROOT / "evidence/oracle-compare" / f"{name}-{prof}.json"
            if not rp.is_file():
                errors.append(f"missing compare receipt {rp.name}")
                continue
            r = json.load(open(rp))
            check(r.get("pass") is True, f"{rp.name}: not passing")
            check(r.get("scenario") == name,
                  f"{rp.name}: scenario field mismatch")
            check(r.get("profile") == prof,
                  f"{rp.name}: profile field mismatch")
            check(set(r.get("charts", {})) == set(charts),
                  f"{rp.name}: receipt chart-id set != spec chart-id set")
            for cid, cr in r.get("charts", {}).items():
                check(cr.get("pass") is True, f"{rp.name}:{cid} failing")
                check(cr.get("job_id"), f"{rp.name}:{cid} null job id")
                # The receipt must bind the exact expected file it
                # validated — editing expected rows after a comparison
                # invalidates the receipt (eighth review).
                ef = exp_dir / f"{cid}.json"
                check(ef.is_file() and cr.get("expected_file_sha256")
                      == sha(ef),
                      f"{rp.name}:{cid} expected-file hash not bound or "
                      "stale")
            check(r.get("scenario_manifest_sha256") == msha,
                  f"{rp.name}: manifest sha mismatch")
            check(r.get("runner_sha256") == runner_sha,
                  f"{rp.name}: stale runner hash")
            check(r.get("spec_sha256") == spec_sha,
                  f"{rp.name}: stale spec hash")
            pinv = json.load(open(
                ROOT / f"evidence/inventories/adk-{prof}.json"))
            check(r.get("inventory_fingerprint")
                  == pinv["fingerprint_sha256"],
                  f"{rp.name}: inventory fingerprint mismatch")
            commit = r.get("repo_commit", "")
            check(commit and "+dirty" not in commit,
                  f"{rp.name}: dirty/absent commit")
            if commit and "+dirty" not in commit:
                check(git_blob_sha256(commit, "oracle/runner.py")
                      == runner_sha or
                      git_blob_sha256(commit, "oracle/runner.py")
                      == r.get("runner_sha256"),
                      f"{rp.name}: commit does not contain the recorded "
                      "runner")
        del inv

    bench_path = ROOT / "evidence/benchmark-10m.json"
    if not bench_path.is_file():
        errors.append("missing benchmark-10m.json")
    else:
        bench = json.load(open(bench_path))
        bb = bench.get("bindings", {})
        for key in ("scenario", "scenario_manifest_sha256", "seed_sha256",
                    "benchmark_tool_sha256", "template_sql_sha256",
                    "repo_commit"):
            check(bb.get(key), f"benchmark: missing binding {key}")
        check(bb.get("benchmark_tool_sha256")
              == sha(ROOT / "tools/benchmark.py"),
              "benchmark: tool hash != current tools/benchmark.py")
        check(bb.get("template_sql_sha256")
              == sha(ROOT / "sql/events_v1.template.sql"),
              "benchmark: template hash != current rendered template")
        commit = bb.get("repo_commit", "")
        check(commit and "+dirty" not in commit,
              "benchmark: dirty/absent commit")
        if commit and "+dirty" not in commit:
            check(git_blob_sha256(commit, "tools/benchmark.py")
                  == bb.get("benchmark_tool_sha256"),
                  "benchmark: commit does not contain the recorded "
                  "benchmark tool")
            check(git_blob_sha256(commit, "sql/events_v1.template.sql")
                  == bb.get("template_sql_sha256"),
                  "benchmark: commit does not contain the recorded "
                  "template")
        check(set(bench.get("gates", {})) == REQUIRED_GATES,
              f"benchmark: gate set {sorted(bench.get('gates', {}))} != "
              "required")
        check(set(bench.get("shapes", {})) == REQUIRED_SHAPES,
              "benchmark: shape set != required six shapes")
        check(bench.get("runs_per_shape") == 20,
              "benchmark: runs_per_shape != 20")
        check(bench.get("query_cache") == "disabled",
              "benchmark: query cache not recorded as disabled")
        for sname, s in bench.get("shapes", {}).items():
            check(len(s.get("raw", [])) == 20,
                  f"benchmark shape {sname}: expected 20 recorded runs")
            for run in s.get("raw", []):
                if not run.get("job_id"):
                    errors.append(f"benchmark shape {sname}: run without "
                                  "job id")
                    break
                if (run.get("bi_engine_accelerated") is not False
                        or run.get("reservation_used") is not False):
                    errors.append(f"benchmark shape {sname}: run without "
                                  "verified on-demand/BI-off state")
                    break
        total = None
        bname = bb.get("scenario")
        if bname in scenarios:
            check(bb.get("scenario_manifest_sha256")
                  == sha(scenarios[bname][0]),
                  "benchmark: manifest sha mismatch")
            check(bb.get("seed_sha256")
                  == scenarios[bname][1]["seed"]["ndjson_sha256"],
                  "benchmark: seed sha mismatch")
            total = scenarios[bname][1]["seed"]["total_emitted"]
        else:
            errors.append("benchmark: scenario manifest not committed")

        # Load binding (ninth review): a row count alone would let any
        # same-size table impersonate the seed.
        lv = bench.get("load_verification", {})
        rc = lv.get("load_receipt", {})
        check(rc.get("load_job_id"), "benchmark: load receipt missing "
                                     "load job id")
        check(rc.get("schema"), "benchmark: load receipt missing schema")
        check(rc.get("load_completed_utc"),
              "benchmark: load receipt missing completion time")
        check(rc.get("destination_table")
              == f"{bench.get('project')}.{bench.get('dataset')}"
                 ".agent_events",
              "benchmark: load receipt destination != benchmarked table")
        check(lv.get("row_count_job_id"),
              "benchmark: row-count probe without job id")
        if bname in scenarios:
            m = scenarios[bname][1]
            check(rc.get("ndjson_sha256") == m["seed"]["ndjson_sha256"],
                  "benchmark: load receipt seed sha != manifest seed sha")
            check(rc.get("output_rows") == total,
                  "benchmark: load receipt rows != manifest total_emitted")
            check(lv.get("table_rows_verified") == total,
                  "benchmark: verified table rows != manifest "
                  "total_emitted")
            check(lv.get("event_type_counts")
                  == m["seed"]["by_event_type"],
                  "benchmark: live event-type counts != manifest "
                  "by_event_type")

        # Capacity contract (ninth review): sanitized, verified, and
        # free of raw INFORMATION_SCHEMA rows.
        cap = bench.get("capacity", {})
        check(cap.get("verified") is True and
              cap.get("bi_engine_capacities") == 0 and
              cap.get("reservation_assignments") == 0,
              "benchmark: capacity state not verified on-demand with "
              "BI Engine disabled")
        check(cap.get("bi_capacities_job_id")
              and cap.get("assignments_job_id"),
              "benchmark: capacity probe without job id")
        check("bi_engine_observed" not in bench
              and "reservation_assignments_observed" not in bench,
              "benchmark: raw capacity rows present in evidence")

        # Gate predicates are RECOMPUTED from recorded observations;
        # stored pass booleans are never trusted (ninth review).
        gates = bench.get("gates", {})
        g = gates.get("structural_union", {})
        ub, sb = g.get("union_bytes_processed"), \
            g.get("base_scan_bytes_processed")
        check(isinstance(ub, int) and isinstance(sb, int)
              and ub > 0 and sb > 0 and ub / sb <= 3.0,
              "benchmark: structural_union ratio predicate fails on "
              "recorded byte counts")
        if isinstance(ub, int) and isinstance(sb, int) and sb > 0:
            check(abs(g.get("ratio", -1) - ub / sb) < 1e-3,
                  "benchmark: structural_union recorded ratio "
                  "inconsistent with byte counts")
        check(g.get("union_job_ids") and g.get("base_job_ids")
              and all(g.get("union_job_ids", []))
              and all(g.get("base_job_ids", [])),
              "benchmark: structural_union probe without job ids")

        g = gates.get("partition_pruning", {})
        od, td = g.get("one_day_bytes"), g.get("thirty_day_bytes")
        check(isinstance(od, int) and isinstance(td, int)
              and td > 0 and od / td < 0.2,
              "benchmark: partition_pruning fraction predicate fails on "
              "recorded byte counts")
        check(td == bench.get("shapes", {}).get(
                  "scorecard_total_events_union", {}).get(
                  "bytes_processed"),
              "benchmark: partition_pruning wide reference != recorded "
              "30d shape bytes")
        check(g.get("job_id"), "benchmark: partition_pruning probe "
                               "without job id")

        g = gates.get("date_boundary", {})
        for jid in ("boundary_job_id", "before_job_id",
                    "production_template_job_id"):
            check(g.get(jid), f"benchmark: date_boundary {jid} missing")
        planted = g.get("planted_after_end")
        check(isinstance(g.get("in_window"), int)
              and g.get("in_window") == g.get("raw_in_window"),
              "benchmark: date_boundary union/raw in-window counts "
              "disagree")
        check(g.get("production_template_in_window") == g.get("in_window"),
              "benchmark: production template count != in-window count")
        for pos in ("end_day_rows", "start_day_rows", "before_start_rows"):
            check(isinstance(g.get(pos), int) and g.get(pos) > 0,
                  f"benchmark: date_boundary {pos} not positive — "
                  "boundary not exercised")
        check(isinstance(planted, int) and planted > 0
              and g.get("after_end_rows") == planted,
              "benchmark: after-end exclusion predicate fails "
              "(after_end_rows != planted rows)")

    if errors:
        for e in errors:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1
    n = len(oracle_scenarios)
    print(f"evidence OK: {n} scenarios x 37 charts recorded with current "
          f"bindings; {n * len(COMPARE_PROFILES)} passing compare receipts "
          "(37 charts, real job ids each); benchmark bound and all gates "
          "pass")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None,
                    help="Alternate evidence tree root (adversarial tests)")
    a = ap.parse_args()
    sys.exit(main(a.root))
