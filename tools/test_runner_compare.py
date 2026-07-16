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
import pathlib
import sys

spec = importlib.util.spec_from_file_location(
    "runner", pathlib.Path(__file__).parent.parent / "oracle" / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def main() -> int:
    errors = []
    veq, req = runner.values_equal, runner.rows_equal

    if veq("9007199254740992", "9007199254740993", "exact", None, None):
        errors.append("float-coercion false positive on large integers")
    if not veq("9007199254740992", "9007199254740992", "exact", None, None):
        errors.append("identical large integers reported unequal")
    if veq("4538.911743515851", "4538.9117435158505", "exact", None, None):
        errors.append("exact mode applied a hidden epsilon to decimals")
    if not veq("4538.911743", "4538.911743", "exact", None, None):
        errors.append("identical decimals reported unequal")
    if veq("100", "101", "percentile_tolerance", 0.0, "relative"):
        errors.append("tolerance 0.0 did not mean exact")
    if not veq("100", "101", "percentile_tolerance", 0.02, "relative"):
        errors.append("relative tolerance not applied")
    if veq("100", "101", "percentile_tolerance", 0.5, "absolute"):
        errors.append("absolute tolerance behaved like relative")
    if not veq("100", "100.4", "percentile_tolerance", 0.5, "absolute"):
        errors.append("absolute tolerance not applied")
    if not veq(None, None, "exact", None, None):
        errors.append("null != null")
    if veq(None, "0", "exact", None, None):
        errors.append("null == 0")

    a = [{"user": "u1", "n": "5"}, {"user": "u2", "n": "3"}]
    b = [{"user": "u2", "n": "3"}, {"user": "u1", "n": "5"}]
    if req(a, b, "exact", None, None) == "":
        errors.append("reordered rows passed — ordering must be enforced")
    if req(a, list(a), "exact", None, None) != "":
        errors.append("identical ordered rows failed")
    if req(a, a[:1], "exact", None, None) == "":
        errors.append("row-count mismatch passed")

    if errors:
        for e in errors:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print("comparator tests OK: integer exactness, no hidden epsilon, "
          "both tolerance kinds, ordered comparison")
    return 0


if __name__ == "__main__":
    sys.exit(main())
