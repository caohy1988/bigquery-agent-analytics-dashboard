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
    with open(args.seed_file, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(chunk)
    summary = json.load(open(args.summary))
    root = subprocess.run(["git", "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()

    manifest = {
        "name": args.name,
        "base_scenario": args.base_scenario or args.name,
        "seed": {
            "generator": "tools/seed_events.py",
            "generator_repo_commit": root,
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
