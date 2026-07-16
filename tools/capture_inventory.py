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

Usage (release and source ref are REQUIRED — committed evidence manifests
must carry operator-declared provenance):
  python3 tools/capture_inventory.py --project P --dataset D \
      --location US --prefix v \
      --adk-release 1.27.0 --adk-source-ref v1.27.0 --out inventory.json
"""

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys


class ProvenanceError(RuntimeError):
    pass


def tool_provenance(repo_dir: str, script: "pathlib.Path") -> tuple:
    """Return (commit, content_sha256) for the executing tool.

    Fails closed when git state cannot be read OR when the working-tree
    script differs from its blob at HEAD — a modified ("dirty") tool must
    never write provenance evidence stamped with a clean commit (PR #1
    review P1). Covered by tools/test_provenance.py without BigQuery.
    """
    rel = script.resolve().relative_to(
        pathlib.Path(repo_dir).resolve()).as_posix()

    def git(*argv):
        p = subprocess.run(["git", "-C", repo_dir, *argv],
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise ProvenanceError(
                f"git {' '.join(argv)} failed: {p.stderr.strip()}")
        return p.stdout.strip()

    commit = git("rev-parse", "HEAD")
    blob_at_head = git("rev-parse", f"HEAD:{rel}")
    blob_in_tree = git("hash-object", str(script))
    if blob_at_head != blob_in_tree:
        raise ProvenanceError(
            f"{rel} differs from its content at HEAD ({commit[:7]}); "
            "refusing to record provenance for a modified tool — commit "
            "the change first")
    content_sha = hashlib.sha256(script.read_bytes()).hexdigest()
    return commit, content_sha


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
    ap.add_argument("--adk-release", required=True,
                    help="Operator-declared provenance; never inferred. "
                         "Required: committed evidence manifests must carry "
                         "it (e.g. 1.27.0).")
    ap.add_argument("--adk-source-ref", required=True,
                    help="ADK source tag/commit the release was installed "
                         "from (e.g. v1.27.0). Required for evidence.")
    ap.add_argument("--receipt", default=None,
                    help="Provisioning receipt from provision_fixture.py; "
                         "when given, the declared release must match the "
                         "receipt's installed version and the dataset must "
                         "match — binding observed provenance to the "
                         "provisioning run.")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    receipt_sha = None
    if args.receipt:
        raw = open(args.receipt, "rb").read()
        receipt = json.loads(raw)
        problems = []
        if receipt.get("installed_adk_version") != args.adk_release:
            problems.append(
                f"receipt installed version "
                f"{receipt.get('installed_adk_version')!r} != declared "
                f"{args.adk_release!r}")
        for key in ("project", "dataset", "location"):
            if receipt.get(key) != getattr(args, key):
                problems.append(f"receipt {key} {receipt.get(key)!r} != "
                                f"{getattr(args, key)!r}")
        if problems:
            for p in problems:
                print(f"ERROR: {p}", file=sys.stderr)
            return 1
        receipt_sha = hashlib.sha256(raw).hexdigest()

    # Tool provenance must be THIS repository's commit — anchored to the
    # script's own repo, verified against the working tree, fail-closed.
    script = pathlib.Path(__file__).resolve()
    repo_dir = str(script.parent.parent)
    try:
        tool_commit, tool_sha256 = tool_provenance(repo_dir, script)
    except ProvenanceError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

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
        "declared_adk_source_ref": args.adk_source_ref,
        "source_project": args.project,
        "source_dataset": args.dataset,
        "dataset_location": args.location,
        "view_prefix": args.prefix,
        "capture_tool_commit": tool_commit,
        "capture_tool_sha256": tool_sha256,
        "provisioning_receipt_sha256": receipt_sha,
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
