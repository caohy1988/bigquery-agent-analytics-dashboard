#!/usr/bin/env python3
"""Adversarial unit tests for the oracle comparator (no BigQuery).

Covers the false-positive classes from PR #1's fifth review:
  * large integers must never pass through float coercion
    (9007199254740992 vs ...993 are DIFFERENT);
  * exact mode is exact decimal equality — no hidden epsilon;
  * declared relative vs absolute tolerance kinds behave differently;
  * ordered comparison — equal row SETS in a different order fail.
"""

import importlib.util
import json
import pathlib
import sys

spec = importlib.util.spec_from_file_location(
    "runner", pathlib.Path(__file__).parent.parent / "oracle" / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def main() -> int:
    errors = []
    req = runner.rows_equal

    def veq(a, b, comparison, tol, kind, role="measure"):
        return runner.values_equal(a, b, role, comparison, tol, kind)

    if veq("9007199254740992", "9007199254740993", "exact", None, None):
        errors.append("float-coercion false positive on large integers")
    if not veq("9007199254740992", "9007199254740992", "exact", None, None):
        errors.append("identical large integers reported unequal")
    # Declared canonical float precision (6dp) — the ONLY float leniency,
    # for measures only, absorbing IEEE summation-order noise.
    if not veq("4538.911743515851", "4538.9117435158505", "exact",
               None, None):
        errors.append("canonical-precision policy not applied to floats")
    if veq("4538.911843", "4538.911743", "exact", None, None):
        errors.append("distinct floats at canonical precision passed")
    # String DIMENSIONS are lexical — numeric-looking IDs must not
    # normalize ("001" vs "1", "1e1" vs "10"; sixth review).
    if veq("001", "1", "exact", None, None, role="dimension"):
        errors.append("dimension '001' == '1' — lexical equality violated")
    if veq("1e1", "10", "exact", None, None, role="dimension"):
        errors.append("dimension '1e1' == '10' — lexical equality violated")
    if not veq("user-0001", "user-0001", "exact", None, None,
               role="dimension"):
        errors.append("identical dimensions reported unequal")
    if veq("100", "101", "percentile_tolerance", 0.0, "relative"):
        errors.append("tolerance 0.0 did not mean exact")
    if not veq("100", "101", "percentile_tolerance", 0.02, "relative"):
        errors.append("relative tolerance not applied")
    if veq("100", "101", "percentile_tolerance", 0.5, "absolute"):
        errors.append("absolute tolerance behaved like relative")
    if not veq("100", "100.4", "percentile_tolerance", 0.5, "absolute"):
        errors.append("absolute tolerance not applied")
    # Integer measures never inherit float canonicalization (7th review).
    if veq("1", "1.0000001", "exact", None, None, role="integer_measure"):
        errors.append("integer measure accepted float noise")
    if veq("9007199254740992", "9007199254740992.0000001", "exact", None,
           None, role="integer_measure"):
        errors.append("large integer measure accepted float noise")
    if not veq("42", "42", "exact", None, None, role="integer_measure"):
        errors.append("identical integer measures unequal")
    if not veq("6152.743617021271", "6152.7436170212705", "exact", None,
               None, role="float_measure"):
        errors.append("float measure canonical precision not applied")
    if not veq(None, None, "exact", None, None):
        errors.append("null != null")
    if veq(None, "0", "exact", None, None):
        errors.append("null == 0")

    kinds = {"user": "dimension", "n": "integer_measure"}
    if runner.rows_equal([{"x": "1"}], [{"x": "1"}], {}, "exact", None,
                          None) == "":
        errors.append("undeclared field kind did not fail closed")
    a = [{"user": "u1", "n": "5"}, {"user": "u2", "n": "3"}]
    b = [{"user": "u2", "n": "3"}, {"user": "u1", "n": "5"}]
    if req(a, b, kinds, "exact", None, None) == "":
        errors.append("reordered rows passed — ordering must be enforced")
    if req(a, list(a), kinds, "exact", None, None) != "":
        errors.append("identical ordered rows failed")
    if req(a, a[:1], kinds, "exact", None, None) == "":
        errors.append("row-count mismatch passed")

    # verify_expected: every provenance/declaration field, mutated one at
    # a time, must produce a problem (stale-runner evidence fails closed).
    chart = {"id": "c1"}
    window = {"start_date": "2026-07-02", "end_date": "2026-07-15"}
    filters = {"filter_agent": ["a"]}
    current = {"comparison": "exact", "tolerance": None,
               "tolerance_kind": None, "query_sha256": "Q",
               "runner_sha256": "R", "generator_sha256": "G",
               "spec_sha256": "S", "recording_profile": "1.27.0",
               "recording_fingerprint": "F"}
    good = {"chart_id": "c1", "scenario": "sc", "comparison": "exact",
            "tolerance": None, "tolerance_kind": None, "window": window,
            "filters": filters,
            "bindings": {"scenario_manifest_sha256": "M",
                          "query_sha256": "Q", "runner_sha256": "R",
                          "generator_sha256": "G", "spec_sha256": "S",
                          "inventory_fingerprint": "F",
                          "profile": "1.27.0", "job_id": "oracle_x",
                          "repo_commit": "abc123"}}
    if runner.verify_expected(good, chart, "sc", window, filters, "M",
                              current):
        errors.append("valid expected file rejected")
    mutations = [
        ("chart_id", "other"), ("scenario", "other"),
        ("comparison", "percentile_tolerance"), ("tolerance", 0.1),
        ("window", {"start_date": "2026-01-01", "end_date": "2026-01-02"}),
        ("filters", {}),
    ]
    for key, val in mutations:
        bad = json.loads(json.dumps(good))
        bad[key] = val
        if not runner.verify_expected(bad, chart, "sc", window, filters,
                                      "M", current):
            errors.append(f"mutation of {key} not detected")
    for bkey, bval in [("scenario_manifest_sha256", "X"),
                        ("query_sha256", "X"), ("runner_sha256", "X"),
                        ("generator_sha256", "X"), ("spec_sha256", "X"),
                        ("inventory_fingerprint", "X"),
                        ("profile", "2.4.0"), ("job_id", None),
                        ("repo_commit", "abc123+dirty")]:
        bad = json.loads(json.dumps(good))
        bad["bindings"][bkey] = bval
        if not runner.verify_expected(bad, chart, "sc", window, filters,
                                      "M", current):
            errors.append(f"bindings mutation of {bkey} not detected")

    if errors:
        for e in errors:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print("comparator tests OK: integer exactness, no hidden epsilon, "
          "both tolerance kinds, ordered comparison")
    return 0


if __name__ == "__main__":
    sys.exit(main())
