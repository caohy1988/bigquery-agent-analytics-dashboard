#!/usr/bin/env python3
"""Mutation tests for the M0 evidence validator.

Builds ONE minimal evidence tree that the validator ACCEPTS (the baseline
proves the tree isolates nothing but the mutation under test), then
mutates a single field per case and asserts the validator rejects it with
the expected diagnostic — so each regression is pinned to its own check
rather than to any incidental defect of a fabricated tree (ninth review).

Covers the eighth-review forgery (37 copies of one chart), benchmark
predicate tampering (stored pass booleans are never trusted), seed/
scenario counter tampering, load-receipt binding, capacity sanitization,
unauditable probes, and fabricated commits.
"""

import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEED_SHA = "5" * 64
BENCH_SEED_SHA = "b" * 64
SCENARIOS = ("seed-20260715-30d-100k", "seed-20260715-30d-100k-f1",
             "seed-20260715-30d-100k-f2")
PROFILES = ("1.36.1", "2.4.0")
SHAPES = ("scorecard_total_events_union",
          "scorecard_distinct_sessions_union", "trend_tokens_by_day",
          "bar_top5_users_by_traces_union", "scorecard_p90_tool_latency",
          "scorecard_pop_tokens")
BENCH_COUNTS = {"LLM_RESPONSE": 60, "TOOL_COMPLETED": 25,
                "USER_MESSAGE_RECEIVED": 15}
BENCH_TOTAL = 100


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def wjson(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)
        fh.write("\n")


def build_passing_tree(fake: pathlib.Path) -> None:
    for rel in ("spec/dashboard_spec.yaml", "spec/overrides.yaml",
                "oracle/runner.py", "oracle/gen_oracle.py",
                "sql/events_v1.template.sql", "tools/benchmark.py"):
        (fake / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, fake / rel)
    (fake / "oracle/queries").mkdir(parents=True)
    for q in (ROOT / "oracle/queries").glob("*.sql"):
        shutil.copy(q, fake / "oracle/queries" / q.name)

    # The commit-existence checks run `git show` against the fake root, so
    # the code files are committed there; evidence then binds that commit.
    def git(*argv):
        subprocess.run(["git", "-C", str(fake), "-c",
                        "user.email=t@t", "-c", "user.name=t", *argv],
                       capture_output=True, check=True)
    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "code")
    commit = subprocess.run(["git", "-C", str(fake), "rev-parse", "HEAD"],
                            capture_output=True, text=True,
                            check=True).stdout.strip()

    spec = yaml.safe_load(open(fake / "spec/dashboard_spec.yaml"))
    charts = {c["id"]: c for c in spec["charts"]}
    runner_sha = sha(fake / "oracle/runner.py")
    gen_sha = sha(fake / "oracle/gen_oracle.py")
    spec_sha = sha(fake / "spec/dashboard_spec.yaml")

    for n in SCENARIOS:
        wjson(fake / "oracle/scenarios" / f"{n}.json",
              {"name": n, "base_scenario": SCENARIOS[0],
               "seed": {"ndjson_sha256": SEED_SHA,
                        "end_date": "2026-07-15", "total_emitted": 90,
                        "fixture_counters": {"boundary_after_end_rows": 3},
                        "by_event_type": {"LLM_RESPONSE": 90}},
               "filters": {}})
    wjson(fake / "oracle/scenarios/bench-10m.json",
          {"name": "bench-10m", "base_scenario": "bench-10m",
           "seed": {"ndjson_sha256": BENCH_SEED_SHA,
                    "end_date": "2026-07-15",
                    "total_emitted": BENCH_TOTAL,
                    "fixture_counters": {"boundary_after_end_rows": 3},
                    "by_event_type": BENCH_COUNTS},
           "filters": {}})

    for prof in ("1.27.0",) + PROFILES:
        wjson(fake / "evidence/inventories" / f"adk-{prof}.json",
              {"fingerprint_sha256": hashlib.sha256(
                  prof.encode()).hexdigest()})

    for n in SCENARIOS:
        msha = sha(fake / "oracle/scenarios" / f"{n}.json")
        for cid, c in charts.items():
            wjson(fake / "oracle/expected" / n / f"{cid}.json",
                  {"chart_id": cid, "scenario": n, "rows": [],
                   "bindings": {
                       "query_sha256": sha(fake / c["oracle_query"]),
                       "job_id": f"job-{n}-{cid}",
                       "runner_sha256": runner_sha,
                       "generator_sha256": gen_sha,
                       "spec_sha256": spec_sha,
                       "scenario_manifest_sha256": msha,
                       "seed_sha256": SEED_SHA,
                       "repo_commit": commit}})
        for prof in PROFILES:
            fp = json.load(open(fake / "evidence/inventories"
                                / f"adk-{prof}.json"))["fingerprint_sha256"]
            wjson(fake / "evidence/oracle-compare" / f"{n}-{prof}.json",
                  {"pass": True, "scenario": n, "profile": prof,
                   "charts": {cid: {"pass": True,
                                    "job_id": f"cmp-{n}-{prof}-{cid}",
                                    "expected_file_sha256": sha(
                                        fake / "oracle/expected" / n
                                        / f"{cid}.json")}
                              for cid in charts},
                   "scenario_manifest_sha256": msha,
                   "runner_sha256": runner_sha, "spec_sha256": spec_sha,
                   "inventory_fingerprint": fp, "repo_commit": commit})

    run = {"job_id": "shape-run", "bytes_processed": 1000,
           "bytes_billed": 1000, "slot_ms": 5, "elapsed_ms": 100,
           "bi_engine_accelerated": False, "reservation_used": False}
    wjson(fake / "evidence/benchmark-10m.json", {
        "project": "fake-project", "dataset": "bench",
        "window": {"start": "2026-06-16", "end": "2026-07-15"},
        "runs_per_shape": 20, "query_cache": "disabled",
        "load_verification": {
            "load_receipt": {
                "scenario": "bench-10m",
                "ndjson_sha256": BENCH_SEED_SHA,
                "load_job_id": "load-1",
                "destination_table": "fake-project.bench.agent_events",
                "write_disposition": "WRITE_TRUNCATE",
                "output_rows": BENCH_TOTAL,
                "schema": [{"name": "timestamp", "type": "TIMESTAMP"}],
                "load_completed_utc": "2026-07-16T00:00:00+00:00"},
            "load_job_verified": True,
            "table_rows_verified": BENCH_TOTAL,
            "row_count_job_id": "rows-1",
            "event_type_counts": BENCH_COUNTS},
        "capacity": {"bi_engine_capacities": 0,
                     "reservation_assignments": 0,
                     "bi_capacities_job_id": "cap-1",
                     "assignments_job_id": "asn-1", "verified": True},
        "bindings": {
            "scenario": "bench-10m",
            "scenario_manifest_sha256": sha(
                fake / "oracle/scenarios/bench-10m.json"),
            "seed_sha256": BENCH_SEED_SHA,
            "benchmark_tool_sha256": sha(fake / "tools/benchmark.py"),
            "template_sql_sha256": sha(
                fake / "sql/events_v1.template.sql"),
            "repo_commit": commit,
            "executed_at_utc": "2026-07-16T00:00:00+00:00"},
        "shapes": {s: {"bytes_processed": 1000, "bytes_billed_max": 1000,
                       "slot_ms_median": 5,
                       "elapsed_ms": {"p50": 100, "p95": 100,
                                      "min": 100, "max": 100},
                       "raw": [dict(run) for _ in range(20)]}
                   for s in SHAPES},
        "gates": {
            "structural_union": {
                "union_job_ids": ["u1", "u2", "u3"],
                "base_job_ids": ["b1", "b2", "b3"],
                "union_bytes_processed": 1500,
                "base_scan_bytes_processed": 1000,
                "ratio": 1.5, "limit": 3.0, "pass": True},
            "date_boundary": {
                "in_window": 84, "raw_in_window": 84, "end_day_rows": 5,
                "start_day_rows": 5, "before_start_rows": 2,
                "after_end_rows": 3, "planted_after_end": 3,
                "production_template_in_window": 84,
                "boundary_job_id": "db-1", "before_job_id": "db-2",
                "production_template_job_id": "db-3", "pass": True},
            "partition_pruning": {
                "job_id": "pp-1", "one_day_bytes": 10,
                "thirty_day_bytes": 1000, "fraction": 0.01,
                "pass": True}}})


def run_validator(fake):
    return subprocess.run(
        [sys.executable, str(ROOT / "tools/validate_evidence.py"),
         "--root", str(fake)], capture_output=True, text=True)


def edit_json(path, fn):
    obj = json.load(open(path))
    fn(obj)
    wjson(path, obj)


def mutate_bench(field_fn):
    def apply(fake):
        edit_json(fake / "evidence/benchmark-10m.json", field_fn)
    return apply


def forge_chart_set(fake):
    # The eighth-review forgery: 37 copies of one chart under fake names.
    d = fake / "oracle/expected" / SCENARIOS[0]
    src = json.load(open(d / sorted(p.name for p in d.glob("*.json"))[0]))
    for p in d.glob("*.json"):
        p.unlink()
    for i in range(37):
        wjson(d / f"fake-{i}.json", src)


CASES = [
    ("structural ratio tampered (pass=true kept)",
     mutate_bench(lambda b: b["gates"]["structural_union"].update(
         union_bytes_processed=999000, ratio=1.5)),
     "structural_union ratio predicate fails"),
    ("pruning fraction tampered (pass=true kept)",
     mutate_bench(lambda b: b["gates"]["partition_pruning"].update(
         one_day_bytes=999000)),
     "partition_pruning fraction predicate fails"),
    ("after-end exclusion tampered",
     mutate_bench(lambda b: b["gates"]["date_boundary"].update(
         after_end_rows=0)),
     "after-end exclusion predicate fails"),
    ("verified row count tampered",
     mutate_bench(lambda b: b["load_verification"].update(
         table_rows_verified=1)),
     "verified table rows != manifest total_emitted"),
    ("live event-type counts tampered",
     mutate_bench(lambda b: b["load_verification"]
                  ["event_type_counts"].update(LLM_RESPONSE=61)),
     "live event-type counts != manifest by_event_type"),
    ("load receipt bound to a different seed",
     mutate_bench(lambda b: b["load_verification"]["load_receipt"].update(
         ndjson_sha256="e" * 64)),
     "load receipt seed sha != manifest seed sha"),
    ("gate probe without job id",
     mutate_bench(lambda b: b["gates"]["date_boundary"].pop(
         "before_job_id")),
     "date_boundary before_job_id missing"),
    ("shape run without job id",
     mutate_bench(lambda b: b["shapes"][SHAPES[0]]["raw"][7].update(
         job_id=None)),
     "run without job id"),
    ("nonempty BI Engine capacity accepted",
     mutate_bench(lambda b: b["capacity"].update(bi_engine_capacities=1)),
     "capacity state not verified on-demand"),
    ("raw capacity rows leaked into evidence",
     mutate_bench(lambda b: b.update(
         bi_engine_observed=[{"project_id": "leak"}])),
     "raw capacity rows present in evidence"),
    ("fabricated benchmark commit",
     mutate_bench(lambda b: b["bindings"].update(repo_commit="0" * 40)),
     "commit does not contain the recorded benchmark tool"),
    ("benchmark scenario counters tampered",
     lambda fake: edit_json(
         fake / "oracle/scenarios/bench-10m.json",
         lambda m: m["seed"]["by_event_type"].update(LLM_RESPONSE=61)),
     "benchmark: manifest sha mismatch"),
    ("oracle scenario manifest tampered",
     lambda fake: edit_json(
         fake / "oracle/scenarios" / f"{SCENARIOS[1]}.json",
         lambda m: m["seed"]["fixture_counters"].update(
             boundary_after_end_rows=99)),
     "manifest sha mismatch"),
    ("37 copies of one chart under fake names",
     forge_chart_set,
     "expected-file set != spec chart-id set"),
    ("refresh marker present",
     lambda fake: (fake / "evidence/.refresh-in-progress").write_text(""),
     "refresh marker present"),
]


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = pathlib.Path(td) / "base"
        build_passing_tree(base)
        r = run_validator(base)
        if r.returncode != 0:
            print("FAIL: baseline minimal tree must PASS the validator "
                  "(otherwise mutations prove nothing):\n" + r.stderr,
                  file=sys.stderr)
            return 1
        failures = []
        for i, (name, mutate, expect) in enumerate(CASES):
            fake = pathlib.Path(td) / f"case-{i}"
            shutil.copytree(base, fake)
            mutate(fake)
            r = run_validator(fake)
            if r.returncode == 0:
                failures.append(f"{name}: forged tree was ACCEPTED")
            elif expect not in r.stderr:
                failures.append(
                    f"{name}: rejected, but not for the isolated reason; "
                    f"expected {expect!r} in:\n{r.stderr}")
        if failures:
            for f in failures:
                print(f"FAIL: {f}", file=sys.stderr)
            return 1
    print(f"validator mutation tests OK: baseline passes, "
          f"{len(CASES)} single-field forgeries each rejected with the "
          "expected diagnostic")
    return 0


if __name__ == "__main__":
    sys.exit(main())
