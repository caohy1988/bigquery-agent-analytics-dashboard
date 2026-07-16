#!/usr/bin/env python3
"""CI negative tests for the spec contract (PR #1 review regressions).

Proves the guards actually reject what they claim to reject:
  * overrides referencing an unknown chart ID fail generation;
  * overrides touching a non-overridable key fail generation;
  * a valid override round-trips into the manifest;
  * non-integer non-data geometry fails schema validation;
  * an unexpected property on a control record fails schema validation.

Requires a pinned block checkout path as argv[1] (CI passes .block-pinned).
"""

import copy
import json
import os
import subprocess
import sys
import tempfile

import yaml
from jsonschema import Draft7Validator

PINNED = "fe6423cc9775b6dc61f7f7047dd4424603ddb3a1"


def gen(block: str, overrides: dict, out: str):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                     delete=False) as fh:
        yaml.safe_dump(overrides, fh)
        path = fh.name
    try:
        return subprocess.run(
            [sys.executable, "tools/lookml_to_spec.py", "--block-repo",
             block, "--pinned-commit", PINNED, "--overrides", path,
             "--out", out],
            capture_output=True, text=True)
    finally:
        os.unlink(path)


def main() -> int:
    block = sys.argv[1] if len(sys.argv) > 1 else "../aab"
    errors = []
    out = tempfile.mktemp(suffix=".yaml")

    r = gen(block, {"charts": {"nope": {"oracle_query": "x.sql"}}}, out)
    if r.returncode == 0 or "unknown chart id" not in (r.stderr + r.stdout):
        errors.append("unknown chart id was not rejected")

    r = gen(block, {"charts": {"usage-token-usage-split-by-agent":
                               {"fields": []}}}, out)
    if r.returncode == 0 or "non-overridable" not in (r.stderr + r.stdout):
        errors.append("non-overridable key was not rejected")

    # Malformed sections must fail closed, never coerce to {} (review P2).
    for bad in ({"decisions": []}, {"charts": []}, {"controls": []},
                {"charts": {"usage-token-usage-split-by-agent": []}},
                {"decisions": {"listener_design_detail": {"aliases": []},
                               "listener_design": None}}):
        r = gen(block, bad, out)
        if r.returncode == 0:
            errors.append(f"malformed overrides accepted: {bad}")

    good_id = "usage-token-usage-split-by-agent"
    r = gen(block, {
        "charts": {good_id: {
            "oracle_query": "oracle/queries/x.sql",
            "ls_chart": "COLUMN",
            "expected_results": [
                {"scenario": "seed-42-30d", "profiles": ["1.27.0", "2.4.0"],
                 "path": "oracle/expected/seed-42-30d/x.json",
                 "comparison": "exact", "tolerance": None}]}},
        "decisions": {
            "call_row_policy": "terminal_row_per_key",
            "listener_design": "global_controls_with_exceptions",
            "listener_design_detail": {
                "inspector_persistence": True,
                "intentional_exceptions": [
                    {"control_id": "usage-control-user-id",
                     "scope": "usage-token-usage-split-by-agent",
                     "rationale": "pinned block quirk"}]}}}, out)
    if r.returncode != 0:
        errors.append(f"valid override failed: {r.stderr[:300]}")
    else:
        s = yaml.safe_load(open(out))
        c = next(x for x in s["charts"] if x["id"] == good_id)
        if c["oracle_query"] != "oracle/queries/x.sql":
            errors.append("chart override did not round-trip")
        if c["ls_chart"] != "COLUMN":
            errors.append("ls_chart override did not round-trip")
        if not c["expected_results"] or \
                c["expected_results"][0]["comparison"] != "exact":
            errors.append("expected_results override did not round-trip")
        d = s["decisions"]
        if d["call_row_policy"] != "terminal_row_per_key" or \
                d["listener_design"] != "global_controls_with_exceptions":
            errors.append("decision overrides did not round-trip")

    r = gen(block, {"decisions": {
        "listener_design": "data_source_aliases",
        "listener_design_detail": {
            "aliases": [{"name": "events", "purpose": "primary"}],
            "chart_alias_assignments": {"no-such-chart": "events"}}}}, out)
    if r.returncode == 0:
        errors.append("listener detail with unknown chart id accepted")

    schema = json.load(open("spec/dashboard_spec.schema.json"))
    base = yaml.safe_load(open("spec/dashboard_spec.yaml"))

    mutated = copy.deepcopy(base)
    mutated["non_data_elements"][0]["geometry"]["width"] = "not-an-integer"
    if not list(Draft7Validator(schema).iter_errors(mutated)):
        errors.append("schema accepted non-integer non-data geometry")

    mutated = copy.deepcopy(base)
    mutated["controls"][0]["unexpected_prop"] = 1
    if not list(Draft7Validator(schema).iter_errors(mutated)):
        errors.append("schema accepted unexpected control property")

    if os.path.exists(out):
        os.unlink(out)
    if errors:
        for e in errors:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print("spec contract negative tests OK: bad overrides rejected, "
          "valid override round-trips, schema rejects malformed "
          "geometry/extra properties")
    return 0


if __name__ == "__main__":
    sys.exit(main())
