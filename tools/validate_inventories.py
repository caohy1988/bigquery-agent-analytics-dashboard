#!/usr/bin/env python3
"""CI validation of the committed inventory evidence (no BigQuery).

Asserts, for every manifest in evidence/inventories/:
  * the stored fingerprint recomputes exactly from the committed view
    definitions (same canonicalization as capture_inventory.py);
  * declared release and source ref are present;
  * the view set matches spec/compatibility_profile.json for that profile;
  * the three-way intersection equals the declared intersection list.
"""

import hashlib
import json
import pathlib
import re
import sys


def recompute_fingerprint(manifest: dict) -> str:
    prefix = manifest["view_prefix"]
    canonical = json.dumps(
        [{**iv, "view": re.sub(f"^{re.escape(prefix)}_", "__PREFIX___",
                               iv["view"])} for iv in manifest["views"]],
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def main() -> int:
    profile = json.load(open("spec/compatibility_profile.json"))
    intersection = set(profile["intersection_views"])
    errors = []
    view_sets = {}

    for path in sorted(pathlib.Path("evidence/inventories").glob("*.json")):
        m = json.load(open(path))
        rel = m.get("declared_adk_release")
        if not rel or not m.get("declared_adk_source_ref"):
            errors.append(f"{path.name}: missing declared release/ref")
            continue
        got = recompute_fingerprint(m)
        if got != m["fingerprint_sha256"]:
            errors.append(f"{path.name}: fingerprint does not recompute "
                          f"({got[:12]} != {m['fingerprint_sha256'][:12]})")
        views = {v["view"] for v in m["views"]}
        view_sets[rel] = views
        expected = intersection | set(profile["profiles"][rel]["adds"])
        if views != expected:
            errors.append(
                f"{path.name}: view set mismatch vs compatibility profile; "
                f"missing={sorted(expected - views)} "
                f"extra={sorted(views - expected)}")

    required = set(profile["profiles"])
    if set(view_sets) != required:
        errors.append(f"inventories present for {sorted(view_sets)}, "
                      f"required {sorted(required)}")
    elif set.intersection(*view_sets.values()) != intersection:
        errors.append("three-way intersection != declared intersection list")

    if errors:
        for e in errors:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(f"inventories OK: {len(view_sets)} profiles, fingerprints "
          "recompute, view sets match the compatibility profile, "
          "intersection verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
