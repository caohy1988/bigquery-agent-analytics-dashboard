#!/usr/bin/env python3
"""Load a seed NDJSON into agent_events and write a load receipt.

The receipt is the only accepted proof that the benchmark dataset IS the
scenario's seed (ninth review): it binds the NDJSON sha256, the BigQuery
load job id, the destination table, the table schema, the loaded row
count, and the load completion time. tools/benchmark.py refuses to run
without it and re-verifies the job server-side.

WRITE_TRUNCATE is deliberate: after a successful load the table contains
exactly the seed rows, nothing else.

Requires google-cloud-bigquery (run inside one of the fixture venvs).
"""

import argparse
import hashlib
import json
import sys

from google.cloud import bigquery


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--location", default="US")
    ap.add_argument("--seed-file", required=True)
    ap.add_argument("--scenario-manifest", required=True)
    ap.add_argument("--receipt", required=True)
    args = ap.parse_args()

    manifest = json.load(open(args.scenario_manifest))
    sha = hashlib.sha256()
    with open(args.seed_file, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(chunk)
    ndjson_sha = sha.hexdigest()
    if ndjson_sha != manifest["seed"]["ndjson_sha256"]:
        print("ERROR: seed file sha256 does not match the scenario "
              "manifest — refusing to load an unbound artifact",
              file=sys.stderr)
        return 1

    client = bigquery.Client(project=args.project)
    table_id = f"{args.project}.{args.dataset}.agent_events"
    # Existing schema (created by the plugin's provisioning) is reused;
    # autodetect is off so a drifted seed fails the load instead of
    # silently reshaping the table.
    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    with open(args.seed_file, "rb") as fh:
        job = client.load_table_from_file(fh, table_id,
                                          job_config=job_config)
    job.result()
    if job.errors:
        print(f"ERROR: load job reported errors: {job.errors}",
              file=sys.stderr)
        return 1
    if job.output_rows != manifest["seed"]["total_emitted"]:
        print(f"ERROR: load job wrote {job.output_rows} rows but the "
              f"manifest binds {manifest['seed']['total_emitted']}",
              file=sys.stderr)
        return 1

    table = client.get_table(table_id)
    receipt = {
        "scenario": manifest["name"],
        "scenario_manifest_sha256": hashlib.sha256(
            open(args.scenario_manifest, "rb").read()).hexdigest(),
        "ndjson_sha256": ndjson_sha,
        "load_job_id": job.job_id,
        "load_job_location": job.location,
        "destination_table": table_id,
        "write_disposition": "WRITE_TRUNCATE",
        "output_rows": job.output_rows,
        "schema": [{"name": f.name, "type": f.field_type}
                   for f in table.schema],
        "load_started_utc": job.started.isoformat(),
        "load_completed_utc": job.ended.isoformat(),
    }
    with open(args.receipt, "w") as fh:
        json.dump(receipt, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print(f"loaded {job.output_rows} rows into {table_id} "
          f"(job {job.job_id}); receipt: {args.receipt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
