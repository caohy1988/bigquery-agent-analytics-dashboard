#!/usr/bin/env python3
"""Provision an isolated BQAA fixture dataset using a specific installed
ADK release's OWN plugin code (M0 prerequisite).

The table schema and the generated views are created by the installed
plugin's `_ensure_schema_exists()` and `_create_analytics_views()` — this
script never composes view SQL itself, so the captured inventory is
authentic to the release that produced it.

Run inside a venv with the target `google-adk` version installed:
  python3 tools/provision_fixture.py --project P --dataset D --location US
Prints the installed ADK version and the created view list as JSON.
"""

import argparse
import importlib.metadata
import inspect
import json
import sys

from google.cloud import bigquery


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--location", default="US")
    args = ap.parse_args()

    adk_version = importlib.metadata.version("google-adk")

    from google.adk.plugins.bigquery_agent_analytics_plugin import (
        BigQueryAgentAnalyticsPlugin,
    )

    client = bigquery.Client(project=args.project)
    ds_ref = bigquery.Dataset(f"{args.project}.{args.dataset}")
    ds_ref.location = args.location
    client.create_dataset(ds_ref, exists_ok=True)

    sig = inspect.signature(BigQueryAgentAnalyticsPlugin.__init__)
    kwargs = {"project_id": args.project, "dataset_id": args.dataset}
    if "location" in sig.parameters:
        kwargs["location"] = args.location
    plugin = BigQueryAgentAnalyticsPlugin(**kwargs)
    if getattr(plugin, "client", None) is None:
        plugin.client = client

    plugin._ensure_schema_exists()
    plugin._create_analytics_views()

    views = sorted(
        t.table_id for t in client.list_tables(f"{args.project}.{args.dataset}")
        if t.table_type == "VIEW")
    tables = sorted(
        t.table_id for t in client.list_tables(f"{args.project}.{args.dataset}")
        if t.table_type == "TABLE")
    print(json.dumps({
        "adk_version": adk_version,
        "project": args.project,
        "dataset": args.dataset,
        "location": args.location,
        "tables": tables,
        "views": views,
        "view_count": len(views),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
