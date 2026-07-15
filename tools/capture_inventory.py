#!/usr/bin/env python3
"""Capture a BQAA dataset's generated-view inventory and compute the
normalized fingerprint used by the hydration helper's provenance rule.

INFORMATION_SCHEMA describes deployed objects; it cannot identify the ADK
release that created them. This tool therefore captures OBSERVED inventories;
expected per-release manifests are produced by running it against isolated
fixture datasets created by each candidate ADK profile (1.27.0, 1.36.1,
2.4.0) and committing the outputs.

Normalization before hashing: the project and dataset identifiers are
replaced with fixed tokens and whitespace is collapsed, so the same ADK
release produces the same fingerprint in any environment (no false
mismatches from project/dataset substitution).

Requires the `bq` CLI authenticated as the current user, with BigQuery
metadata permissions on the dataset. Dataset location must be passed
explicitly.

Usage:
  python3 tools/capture_inventory.py --project P --dataset D \
      --location US --prefix v [--adk-release 1.27.0] --out inventory.json
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys


def bq_query(project: str, location: str, sql: str) -> list:
    cmd = [
        "bq", "query", "--nouse_legacy_sql", "--format=json",
        f"--project_id={project}", f"--location={location}",
        "--max_rows=10000", sql,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        print(out.stderr, file=sys.stderr)
        sys.exit(1)
    return json.loads(out.stdout or "[]")


def normalize(definition: str, project: str, dataset: str) -> str:
    d = definition.replace(project, "__PROJECT__")
    d = d.replace(dataset, "__DATASET__")
    return re.sub(r"\s+", " ", d).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--location", required=True)
    ap.add_argument("--prefix", default="v")
    ap.add_argument("--adk-release", default=None,
                    help="Operator-declared provenance; never inferred.")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    views = bq_query(args.project, args.location, f"""
        SELECT table_name, view_definition
        FROM `{args.project}.{args.dataset}`.INFORMATION_SCHEMA.VIEWS
        WHERE STARTS_WITH(table_name, '{args.prefix}_')
        ORDER BY table_name
    """)
    columns = bq_query(args.project, args.location, f"""
        SELECT table_name, column_name, ordinal_position, data_type
        FROM `{args.project}.{args.dataset}`.INFORMATION_SCHEMA.COLUMNS
        WHERE STARTS_WITH(table_name, '{args.prefix}_')
        ORDER BY table_name, ordinal_position
    """)

    cols_by_view: dict = {}
    for c in columns:
        cols_by_view.setdefault(c["table_name"], []).append(
            {"name": c["column_name"], "type": c["data_type"],
             "position": int(c["ordinal_position"])})

    inventory = []
    for v in views:
        inventory.append({
            "view": v["table_name"],
            "columns": cols_by_view.get(v["table_name"], []),
            "definition_normalized": normalize(
                v["view_definition"], args.project, args.dataset),
        })

    canonical = json.dumps(
        [{**iv, "view": re.sub(f"^{re.escape(args.prefix)}_", "__PREFIX___",
                               iv["view"])} for iv in inventory],
        sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical.encode()).hexdigest()

    manifest = {
        "declared_adk_release": args.adk_release,
        "dataset_location": args.location,
        "view_prefix": args.prefix,
        "view_count": len(inventory),
        "fingerprint_sha256": fingerprint,
        "views": inventory,
    }
    with open(args.out, "w") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"captured {len(inventory)} views; fingerprint {fingerprint}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
