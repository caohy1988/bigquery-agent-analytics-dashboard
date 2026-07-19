#!/usr/bin/env python3
"""Write a scenario manifest binding a seed artifact to oracle evidence.

The manifest records the exact seed NDJSON sha256, generator arguments,
fixture counters, and (optionally) dashboard control filter values, so
every expected-result file produced under it is reproducible from committed
inputs.
"""

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--base-scenario", default=None,
                    help="Scenario whose tolerance declarations in the "
                         "manifest apply (defaults to --name)")
    ap.add_argument("--seed-file", required=True)
    ap.add_argument("--events", type=int, required=True)
    ap.add_argument("--days", type=int, required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--end-date", default="2026-07-15")
    ap.add_argument("--summary", required=True,
                    help="JSON summary printed by seed_events.py")
    ap.add_argument("--filters", default="{}",
                    help='JSON control filters, e.g. {"Agent": ["a"]}')
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    sha = hashlib.sha256()
    lines = 0
    with open(args.seed_file, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(chunk)
            lines += chunk.count(b"\n")
    summary = json.load(open(args.summary))
    # Cryptographic binding: the summary must carry the sha of THIS file
    # and the same generation arguments (a same-row-count summary from a
    # different seed fails; seventh review).
    if summary.get("ndjson_sha256") != sha.hexdigest():
        print("ERROR: summary ndjson_sha256 does not match the seed file",
              file=sys.stderr)
        return 1
    if summary.get("args") != {"events": args.events, "days": args.days,
                                "seed": args.seed}:
        print("ERROR: summary generation args do not match", file=sys.stderr)
        return 1
    if summary.get("end_date") != args.end_date:
        print("ERROR: summary end_date does not match", file=sys.stderr)
        return 1
    # Recompute event-type and fixture counters FROM the bound NDJSON —
    # a tampered summary paired with a genuine seed fails (eighth review).
    counts, fixtures = {}, {"token_metadata_only": 0, "token_content_only": 0,
                             "token_conflict": 0, "streaming_partial_rows": 0,
                             "duplicate_tool_terminal_rows": 0,
                             "base_table_only_rows": 0,
                             "boundary_after_end_rows": 0}
    llm_spans, tool_spans = {}, {}
    with open(args.seed_file) as fh:
        for line in fh:
            r = json.loads(line)
            et = r["event_type"]
            counts[et] = counts.get(et, 0) + 1
            if et in ("HITL_CREDENTIAL_COMPLETED",
                      "HITL_CONFIRMATION_COMPLETED"):
                fixtures["base_table_only_rows"] += 1
            if r["timestamp"][:10] > args.end_date:
                fixtures["boundary_after_end_rows"] += 1
            if et == "LLM_RESPONSE":
                key = (r["trace_id"], r["span_id"])
                llm_spans[key] = llm_spans.get(key, 0) + 1
            if et == "TOOL_COMPLETED":
                key = (r["trace_id"], r["span_id"])
                tool_spans[key] = tool_spans.get(key, 0) + 1
    # Second pass for token variants — every row of a span shares its
    # variant, so classify per-span from the LAST seen row, storing only
    # two booleans per span (memory-safe on 10M rows).
    variants = {}
    with open(args.seed_file) as fh:
        for line in fh:
            r = json.loads(line)
            if r["event_type"] != "LLM_RESPONSE":
                continue
            a = r.get("attributes") or {}
            c = r.get("content") or {}
            variants[(r["trace_id"], r["span_id"])] = (
                "usage_metadata" in a, "usage" in c)
    for key, (has_meta, has_content) in variants.items():
        variant = ("token_conflict" if has_meta and has_content else
                   "token_metadata_only" if has_meta else
                   "token_content_only")
        fixtures[variant] += 1
        fixtures["streaming_partial_rows"] += llm_spans[key] - 1
    for key, n in tool_spans.items():
        fixtures["duplicate_tool_terminal_rows"] += n - 1
    if counts != summary["by_event_type"]:
        print("ERROR: recomputed event counts != summary by_event_type",
              file=sys.stderr)
        return 1
    if fixtures != summary["fixtures"]:
        print(f"ERROR: recomputed fixtures != summary fixtures\n"
              f"  recomputed: {fixtures}\n"
              f"  summary:    {summary['fixtures']}", file=sys.stderr)
        return 1
    if lines != summary["total_emitted"]:
        print(f"ERROR: seed file has {lines} rows but the summary claims "
              f"{summary['total_emitted']} — mismatched artifacts",
              file=sys.stderr)
        return 1

    # Provenance anchored to THIS repository (never the caller's cwd),
    # fail-closed on a dirty generator (same policy as capture/runner).
    repo = str(pathlib.Path(__file__).resolve().parent.parent)
    def git(*argv):
        pr = subprocess.run(["git", "-C", repo, *argv],
                            capture_output=True, text=True)
        if pr.returncode != 0:
            print(f"ERROR: git {' '.join(argv)}: {pr.stderr.strip()}",
                  file=sys.stderr)
            raise SystemExit(1)
        return pr.stdout.strip()
    root = git("rev-parse", "HEAD")
    if git("status", "--porcelain", "--untracked-files=no"):
        print("ERROR: modified tracked files — scenario manifests must be "
              "produced from committed code", file=sys.stderr)
        return 1
    gen_blob = git("rev-parse", "HEAD:tools/seed_events.py")
    gen_wt = git("hash-object", str(pathlib.Path(repo) / "tools"
                                    / "seed_events.py"))
    if gen_blob != gen_wt:
        print("ERROR: tools/seed_events.py differs from HEAD",
              file=sys.stderr)
        return 1
    gen_sha256 = hashlib.sha256(open(
        pathlib.Path(repo) / "tools" / "seed_events.py", "rb"
    ).read()).hexdigest()

    manifest = {
        "name": args.name,
        "base_scenario": args.base_scenario or args.name,
        "seed": {
            "generator": "tools/seed_events.py",
            "generator_repo_commit": root,
            "generator_sha256": gen_sha256,
            "args": {"events": args.events, "days": args.days,
                     "seed": args.seed},
            "end_date": args.end_date,
            "ndjson_sha256": sha.hexdigest(),
            "total_emitted": summary["total_emitted"],
            "fixture_counters": summary["fixtures"],
            "by_event_type": summary["by_event_type"],
        },
        "filters": json.loads(args.filters),
    }
    with open(args.out, "w") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print(f"scenario manifest: {args.out} (seed sha "
          f"{sha.hexdigest()[:12]}…)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
