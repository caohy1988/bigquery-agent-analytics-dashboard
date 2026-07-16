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
import subprocess
import sys

REQUIRED_COLUMNS = {
    "v_llm_response": {"usage_metadata": "JSON",
                       "usage_prompt_tokens": "INT64",
                       "usage_completion_tokens": "INT64",
                       "usage_total_tokens": "INT64",
                       "total_ms": "INT64", "ttft_ms": "INT64",
                       "model_version": "STRING"},
    "v_tool_completed": {"tool_name": "STRING", "tool_origin": "STRING",
                          "total_ms": "INT64"},
    "v_tool_error": {"tool_name": "STRING", "tool_origin": "STRING",
                      "total_ms": "INT64"},
}
COMMON = {"timestamp": "TIMESTAMP", "event_type": "STRING",
          "agent": "STRING", "session_id": "STRING",
          "invocation_id": "STRING", "user_id": "STRING",
          "trace_id": "STRING", "span_id": "STRING",
          "parent_span_id": "STRING", "status": "STRING",
          "error_message": "STRING", "is_truncated": "BOOL"}


def recompute_fingerprint(manifest: dict) -> str:
    prefix = manifest["view_prefix"]
    canonical = json.dumps(
        [{**iv, "view": re.sub(f"^{re.escape(prefix)}_", "__PREFIX___",
                               iv["view"])} for iv in manifest["views"]],
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="evidence/inventories")
    ap.add_argument("--profile-json",
                    default="spec/compatibility_profile.json")
    ap.add_argument("--ignore-refresh-marker", action="store_true",
                    help="For synthetic adversarial tests only")
    args = ap.parse_args()
    if not args.ignore_refresh_marker and \
            pathlib.Path("evidence/.refresh-in-progress").exists():
        print("inventories: REFRESH IN PROGRESS marker present — "
              "skipping (the m0-evidence-gate CI job fails on it)")
        return 0
    profile = json.load(open(args.profile_json))
    intersection = set(profile["intersection_views"])
    errors = []
    view_sets = {}

    receipts = {p.name: p for p in
                pathlib.Path(args.dir).glob("*.receipt.json")}
    for path in sorted(pathlib.Path(args.dir).glob("*.json")):
        if path.name.endswith(".receipt.json"):
            continue
        m = json.load(open(path))
        rel = m.get("declared_adk_release")
        if not rel or not m.get("declared_adk_source_ref"):
            errors.append(f"{path.name}: missing declared release/ref")
            continue
        if m["declared_adk_source_ref"] != \
                profile["profiles"][rel]["source_ref"]:
            errors.append(f"{path.name}: source ref "
                          f"{m['declared_adk_source_ref']} != profile "
                          f"{profile['profiles'][rel]['source_ref']}")
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
        if m.get("view_count") != len(m["views"]):
            errors.append(f"{path.name}: stored view_count "
                          f"{m.get('view_count')} != {len(m['views'])}")

        # Receipt binding: the manifest must embed the sha of a COMMITTED
        # provisioning receipt whose object set covers the observed views.
        rsha = m.get("provisioning_receipt_sha256")
        if not rsha:
            errors.append(f"{path.name}: not receipt-bound")
        else:
            rp = receipts.get(f"adk-{rel}.receipt.json")
            if rp is None:
                errors.append(f"{path.name}: receipt file not committed")
            else:
                raw = open(rp, "rb").read()
                if hashlib.sha256(raw).hexdigest() != rsha:
                    errors.append(f"{path.name}: receipt sha mismatch")
                else:
                    receipt = json.loads(raw)
                    if receipt.get("installed_adk_version") != rel:
                        errors.append(f"{path.name}: receipt version "
                                      "mismatch")
                    if set(receipt.get("views", [])) != views:
                        errors.append(f"{path.name}: receipt view set != "
                                      "observed inventory")
                    for rk, mk in (("project", "source_project"),
                                    ("dataset", "source_dataset"),
                                    ("location", "dataset_location")):
                        if receipt.get(rk) != m.get(mk):
                            errors.append(f"{path.name}: receipt {rk} "
                                          f"{receipt.get(rk)!r} != manifest "
                                          f"{m.get(mk)!r}")
                    if "agent_events" not in receipt.get("tables", []):
                        errors.append(f"{path.name}: receipt lacks "
                                      "agent_events table")
                    if receipt.get("view_count") != m.get("view_count"):
                        errors.append(f"{path.name}: receipt view_count != "
                                      "manifest view_count")

        # Required source columns and types, per view.
        cols_by_view = {v["view"]: {c["name"]: c["type"]
                                    for c in v["columns"]}
                        for v in m["views"]}
        for view in expected & set(cols_by_view):
            need = dict(COMMON)
            need.update(REQUIRED_COLUMNS.get(view, {}))
            for cname, ctype in need.items():
                got_t = cols_by_view[view].get(cname)
                if got_t != ctype:
                    errors.append(f"{path.name}: {view}.{cname} type "
                                  f"{got_t} != required {ctype}")
                    break

        # Tool provenance: the recorded content sha must equal the capture
        # tool's blob at the recorded commit (requires git history in CI).
        commit = m.get("capture_tool_commit")
        tool_sha = m.get("capture_tool_sha256")
        if not commit or not tool_sha:
            errors.append(f"{path.name}: capture tool provenance fields "
                          "are REQUIRED (commit + content sha)")
        else:
            blob = subprocess.run(
                ["git", "show", f"{commit}:tools/capture_inventory.py"],
                capture_output=True)
            if blob.returncode != 0:
                errors.append(f"{path.name}: capture commit {commit[:7]} "
                              "not in history")
            elif hashlib.sha256(blob.stdout).hexdigest() != tool_sha:
                errors.append(f"{path.name}: capture_tool_sha256 does not "
                              "match the tool blob at the recorded commit")

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
