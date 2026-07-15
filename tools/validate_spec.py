#!/usr/bin/env python3
"""CI assertions over spec/dashboard_spec.yaml per the governing contract
(GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK#365).

Evidence-driven correction recorded here: the pinned block contains NINE
non-data (text/navigation) elements, not the seven the contract text
estimated — 4 on Usage (2 h1 section headers, 1 button, 1 empty text) and
5 on Performance (2 h1 headers, 1 button, 2 h2 headers). Counted from the
pinned LookML by tools/lookml_to_spec.py.
"""

import sys
import yaml

USER_ID_EXCEPTIONS = {
    "Token Usage split by Agent",
    "Top 5 users with most Tokens consumption",
    "Top 5 users with most Traces",
    "Traces split by Agent",
}


def check(cond: bool, msg: str, errors: list) -> None:
    if not cond:
        errors.append(msg)


def main() -> int:
    spec = yaml.safe_load(open("spec/dashboard_spec.yaml"))
    charts = spec["charts"]
    errors: list = []

    usage = [c for c in charts if c["source_dashboard"] == "usage"]
    perf = [c for c in charts if c["source_dashboard"] == "performance"]
    check(len(charts) == 37, f"expected 37 charts, got {len(charts)}", errors)
    check(len(usage) == 21, f"expected 21 usage charts, got {len(usage)}",
          errors)
    check(len(perf) == 16, f"expected 16 performance charts, got {len(perf)}",
          errors)

    comparison = [c for c in charts if c["comparison_previous_period"]]
    check(len(comparison) == 6,
          f"expected 6 previous-period scorecards, got {len(comparison)}",
          errors)
    percentile = [c for c in charts if c["percentile"]]
    check(len(percentile) == 8,
          f"expected 8 percentile tiles, got {len(percentile)}", errors)

    nd = spec["non_data_elements"]
    check(len(nd) == 9,
          f"expected 9 non-data elements (evidence-corrected), got {len(nd)}",
          errors)

    ids = [c["id"] for c in charts] + [n["id"] for n in nd]
    check(len(ids) == len(set(ids)), "duplicate record ids", errors)

    for c in charts:
        check(c["page"] is not None, f"{c['id']}: missing page", errors)
        check(c["ls_chart"] != "UNMAPPED",
              f"{c['id']}: unmapped chart type {c['looker_type']}", errors)
        geo = c["geometry"]
        check(all(geo.get(k) is not None
                  for k in ("row", "col", "width", "height")),
              f"{c['id']}: incomplete geometry", errors)

    controls = spec["controls"]
    date_defaults = {c["source_dashboard"]: c["default_value"]
                     for c in controls if c["name"] == "Date"}
    check(date_defaults.get("usage") == "14 day",
          f"usage Date default should be '14 day', got "
          f"{date_defaults.get('usage')!r}", errors)
    check(date_defaults.get("performance") == "7 day",
          f"performance Date default should be '7 day', got "
          f"{date_defaults.get('performance')!r}", errors)

    tool_name_listeners = [
        c["source_tile"] for c in charts
        if any(k == "Tool Name" for k in c["listen"])
    ]
    check(tool_name_listeners == ["Average Tool Latency (ms)"],
          f"Tool Name must listen only on 'Average Tool Latency (ms)', "
          f"got {tool_name_listeners}", errors)

    usage_no_user_id = {
        c["source_tile"] for c in usage
        if "User ID" not in c["listen"]
    }
    check(usage_no_user_id == USER_ID_EXCEPTIONS,
          f"User ID listener exceptions changed: {sorted(usage_no_user_id)}",
          errors)

    if errors:
        for e in errors:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(f"spec OK: 37 charts (21+16), 6 comparison, 8 percentile, "
          f"9 non-data, listener matrix verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
