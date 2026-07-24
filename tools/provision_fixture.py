#!/usr/bin/env python3
"""Provision an isolated BQAA fixture dataset using a specific installed
ADK release's OWN plugin code (M0 prerequisite).

The table schema and the generated views are created by the installed
plugin's `_ensure_schema_exists()` and `_create_analytics_views()` — this
script never composes view SQL itself, so the captured inventory is
authentic to the release that produced it.

Run inside a venv with the target `google-adk` version installed:
  python3 tools/provision_fixture.py --project P --dataset D --location US \
      --expected-adk-version 1.27.0 [--replace] [--receipt out.receipt.json]
Prints the installed ADK version and the created view list as JSON.
"""

import argparse
import datetime
import importlib.metadata
import inspect
import json
import sys

from google.cloud import bigquery
from google.cloud import exceptions as cloud_exceptions

INTERSECTION_VIEWS = {
    f"v_{s}" for s in (
        "user_message_received", "llm_request", "llm_response", "llm_error",
        "tool_starting", "tool_completed", "tool_error", "agent_starting",
        "agent_completed", "invocation_starting", "invocation_completed",
        "state_delta", "hitl_credential_request",
        "hitl_confirmation_request", "hitl_input_request")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--location", default="US")
    ap.add_argument("--expected-adk-version", required=True,
                    help="Fails BEFORE any mutation if the installed "
                         "google-adk differs — prevents mislabeled evidence.")
    ap.add_argument("--replace", action="store_true",
                    help="Delete and recreate an existing dataset. Without "
                         "this flag an existing dataset is an error (stale "
                         "reuse must be explicit).")
    ap.add_argument("--receipt", default=None,
                    help="Path for the provisioning receipt consumed by "
                         "capture_inventory.py (default: "
                         "<dataset>.receipt.json)")
    args = ap.parse_args()

    adk_version = importlib.metadata.version("google-adk")
    if adk_version != args.expected_adk_version:
        print(f"ERROR: installed google-adk {adk_version} != expected "
              f"{args.expected_adk_version}; refusing to provision.",
              file=sys.stderr)
        return 1

    import importlib as _il
    mod = _il.import_module(
        "google.adk.plugins.bigquery_agent_analytics_plugin")
    BigQueryAgentAnalyticsPlugin = mod.BigQueryAgentAnalyticsPlugin

    client = bigquery.Client(project=args.project)
    ds_id = f"{args.project}.{args.dataset}"
    try:
        client.get_dataset(ds_id)
        if not args.replace:
            print(f"ERROR: dataset {ds_id} already exists; pass --replace "
                  "to recreate it (silent reuse would risk stale evidence).",
                  file=sys.stderr)
            return 1
        client.delete_dataset(ds_id, delete_contents=True)
    except cloud_exceptions.NotFound:
        pass
    ds_ref = bigquery.Dataset(ds_id)
    ds_ref.location = args.location
    client.create_dataset(ds_ref)

    sig = inspect.signature(BigQueryAgentAnalyticsPlugin.__init__)
    kwargs = {"project_id": args.project, "dataset_id": args.dataset}
    if "location" in sig.parameters:
        kwargs["location"] = args.location
    plugin = BigQueryAgentAnalyticsPlugin(**kwargs)
    if getattr(plugin, "client", None) is None:
        plugin.client = client
    # Replicate the plugin's own _lazy_setup preamble (we drive the sync
    # setup methods directly instead of running an agent event loop).
    if getattr(plugin, "full_table_id", None) is None:
        plugin.full_table_id = (
            f"{args.project}.{args.dataset}.{plugin.table_id}")
    if getattr(plugin, "_schema", None) is None \
            and hasattr(mod, "_get_events_schema"):
        plugin._schema = mod._get_events_schema()

    plugin._ensure_schema_exists()
    plugin._create_analytics_views()

    views = sorted(
        t.table_id for t in client.list_tables(f"{args.project}.{args.dataset}")
        if t.table_type == "VIEW")
    tables = sorted(
        t.table_id for t in client.list_tables(f"{args.project}.{args.dataset}")
        if t.table_type == "TABLE")

    # The plugin's setup methods log-and-swallow creation errors, so a
    # successful exit MUST be earned by asserting the expected objects.
    problems = []
    if "agent_events" not in tables:
        problems.append("agent_events table missing")
    missing = INTERSECTION_VIEWS - set(views)
    if missing:
        problems.append(f"intersection views missing: {sorted(missing)}")
    if problems:
        for p in problems:
            print(f"ERROR: {p}", file=sys.stderr)
        return 1

    receipt = {
        "installed_adk_version": adk_version,
        "expected_adk_version": args.expected_adk_version,
        "project": args.project,
        "dataset": args.dataset,
        "location": args.location,
        "provisioned_at_utc":
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "tables": tables,
        "views": views,
        "view_count": len(views),
    }
    receipt_path = args.receipt or f"{args.dataset}.receipt.json"
    with open(receipt_path, "w") as fh:
        json.dump(receipt, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    print(f"receipt: {receipt_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
