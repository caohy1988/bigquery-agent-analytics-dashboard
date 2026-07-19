#!/usr/bin/env python3
"""The eighth review fabricated an evidence tree (37 copies of one chart,
alien receipt ids, empty benchmark) that the old validator green-lit.
This test rebuilds that tree and requires the validator to REJECT it."""

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        fake = pathlib.Path(td)
        for rel in ("spec/dashboard_spec.yaml", "spec/overrides.yaml",
                    "oracle/runner.py", "oracle/gen_oracle.py"):
            (fake / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / rel, fake / rel)
        (fake / "oracle/queries").mkdir(parents=True)
        for q in (ROOT / "oracle/queries").glob("*.sql"):
            shutil.copy(q, fake / "oracle/queries" / q.name)
        (fake / "oracle/scenarios").mkdir()
        man = {"name": "seed-20260715-30d-100k", "base_scenario":
               "seed-20260715-30d-100k",
               "seed": {"ndjson_sha256": "s" * 64, "end_date": "2026-07-15"},
               "filters": {}}
        for n in ("seed-20260715-30d-100k", "seed-20260715-30d-100k-f1",
                  "seed-20260715-30d-100k-f2"):
            json.dump({**man, "name": n},
                      open(fake / "oracle/scenarios" / f"{n}.json", "w"))
            d = fake / "oracle/expected" / n
            d.mkdir(parents=True)
            for i in range(37):
                json.dump({"chart_id": "usage-total-tokens",
                           "scenario": n, "rows": [],
                           "bindings": {"job_id": f"fake{i}"}},
                          open(d / f"fake-{i}.json", "w"))
        (fake / "evidence/inventories").mkdir(parents=True)
        for prof in ("1.27.0", "1.36.1", "2.4.0"):
            json.dump({"fingerprint_sha256": "f" * 64},
                      open(fake / "evidence/inventories"
                           / f"adk-{prof}.json", "w"))
        (fake / "evidence/oracle-compare").mkdir()
        for n in ("seed-20260715-30d-100k", "seed-20260715-30d-100k-f1",
                  "seed-20260715-30d-100k-f2"):
            for prof in ("1.36.1", "2.4.0"):
                json.dump({"pass": True, "scenario": n, "profile": prof,
                           "charts": {f"alien-{i}": {"pass": True,
                                                      "job_id": "x"}
                                      for i in range(37)},
                           "repo_commit": "abc"},
                          open(fake / "evidence/oracle-compare"
                               / f"{n}-{prof}.json", "w"))
        json.dump({"bindings": {"scenario": "bench",
                                 "scenario_manifest_sha256": "x",
                                 "seed_sha256": "x",
                                 "benchmark_tool_sha256": "x",
                                 "template_sql_sha256": "x",
                                 "repo_commit": "x"},
                   "gates": {}, "shapes": {}},
                  open(fake / "evidence/benchmark-10m.json", "w"))
        (fake / "sql").mkdir()
        shutil.copy(ROOT / "sql/events_v1.template.sql", fake / "sql/")
        (fake / "tools").mkdir()
        shutil.copy(ROOT / "tools/benchmark.py", fake / "tools/")

        r = subprocess.run(
            [sys.executable, str(ROOT / "tools/validate_evidence.py"),
             "--root", str(fake)], capture_output=True, text=True)
        if r.returncode == 0:
            print("FAIL: fabricated evidence tree was accepted",
                  file=sys.stderr)
            return 1
    print("fabricated-tree test OK: validator rejects the forged evidence")
    return 0


if __name__ == "__main__":
    sys.exit(main())
