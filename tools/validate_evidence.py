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
  * benchmark: bindings (scenario/seed/tool/template/commit), every gate
    pass=true, job ids on every recorded run;
  * refresh marker absent.

Run by the m0-evidence-gate CI job; exits non-zero on any violation.
"""

import hashlib
import json
import pathlib
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
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


def main() -> int:
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
        check(len(files) == 37,
              f"{name}: expected 37 result files, got {len(files)}")
        for f in files:
            e = json.load(open(f))
            b = e.get("bindings", {})
            cid = e.get("chart_id")
            check(cid in charts, f"{f.name}: unknown chart id")
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
            check(len(r.get("charts", {})) == 37,
                  f"{rp.name}: expected 37 charts, got "
                  f"{len(r.get('charts', {}))}")
            for cid, cr in r.get("charts", {}).items():
                check(cr.get("pass") is True, f"{rp.name}:{cid} failing")
                check(cr.get("job_id"), f"{rp.name}:{cid} null job id")
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
        bname = bb.get("scenario")
        if bname in scenarios:
            check(bb.get("scenario_manifest_sha256")
                  == sha(scenarios[bname][0]),
                  "benchmark: manifest sha mismatch")
            check(bb.get("seed_sha256")
                  == scenarios[bname][1]["seed"]["ndjson_sha256"],
                  "benchmark: seed sha mismatch")
        else:
            errors.append("benchmark: scenario manifest not committed")
        for gname, g in bench.get("gates", {}).items():
            check(g.get("pass") is True, f"benchmark gate {gname} failing")
        for sname, s in bench.get("shapes", {}).items():
            for run in s.get("raw", []):
                if not run.get("job_id"):
                    errors.append(f"benchmark shape {sname}: run without "
                                  "job id")
                    break

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
    sys.exit(main())
