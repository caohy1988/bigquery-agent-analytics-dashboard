#!/usr/bin/env python3
"""Generate spec/dashboard_spec.yaml from the pinned Looker agent-analytics-block.

The parity manifest is DERIVED, never hand-edited: every chart record, control
edge, geometry box, and style payload comes from the pinned LookML dashboard
files, which are valid YAML. Re-running against the same pinned commit must be
byte-identical (CI asserts this).

Usage:
  python3 tools/lookml_to_spec.py --block-repo <path-to-agent-analytics-block> \
      --pinned-commit fe6423cc9775b6dc61f7f7047dd4424603ddb3a1 \
      --out spec/dashboard_spec.yaml
"""

import argparse
import sys
import yaml

DASHBOARD_FILES = [
    ("usage", "dashboards/agent_analytics_usage.dashboard.lookml"),
    ("performance", "dashboards/agent_analytics_performance.dashboard.lookml"),
]

# Keys that describe the query/interaction contract; everything else on an
# element is visual configuration and is preserved verbatim under `style`.
STRUCTURAL_KEYS = {
    "title", "name", "model", "explore", "type", "fields", "pivots",
    "fill_fields", "filters", "sorts", "limit", "column_limit", "listen",
    "row", "col", "width", "height", "tab_name", "dynamic_fields",
}

# looker viz type -> Looker Studio chart type (initial hypothesis; M0/M2 may
# refine individual mappings, recorded per-record via `ls_chart`).
LS_CHART_MAP = {
    "looker_bar": "BAR",
    "looker_column": "COLUMN",
    "looker_line": "TIME_SERIES",
    "looker_area": "AREA",
    "single_value": "SCORECARD",
    "looker_grid": "TABLE",
    "looker_donut_multiples": "PIE",
    "looker_pie": "PIE",
    "text": "TEXT",
    "button": "BUTTON",
}

PERCENTILE_TOKENS = ("p50_", "p75_", "p90_", "p99_")


def slugify(dashboard_key: str, title: str, seen: dict) -> str:
    base = "-".join(
        "".join(c if c.isalnum() else " " for c in title.lower()).split()
    )
    slug = f"{dashboard_key}-{base}"
    n = seen.get(slug, 0)
    seen[slug] = n + 1
    return slug if n == 0 else f"{slug}-{n + 1}"


def element_record(dashboard_key: str, el: dict, seen: dict) -> dict:
    fields = el.get("fields") or []
    measures = [f for f in fields if any(t in f for t in (
        "total_", "average_", "p50_", "p75_", "p90_", "p99_", "pop_"))]
    dimensions = [f for f in fields if f not in measures]
    style = {k: v for k, v in el.items() if k not in STRUCTURAL_KEYS}
    rec = {
        "id": slugify(dashboard_key, el["title"], seen),
        "source_dashboard": dashboard_key,
        "source_tile": el["title"],
        "page": el.get("tab_name"),
        "looker_type": el.get("type"),
        "ls_chart": LS_CHART_MAP.get(el.get("type"), "UNMAPPED"),
        "fields": fields,
        "dimensions": dimensions,
        "measures": measures,
        "pivots": el.get("pivots") or [],
        "fill_fields": el.get("fill_fields") or [],
        "tile_filters": el.get("filters") or {},
        "sorts": el.get("sorts") or [],
        "limit": el.get("limit"),
        "listen": el.get("listen") or {},
        "geometry": {k: el.get(k) for k in ("row", "col", "width", "height")},
        "comparison_previous_period": any("pop_" in f for f in fields),
        "percentile": any(t in f for f in fields for t in PERCENTILE_TOKENS),
        "style": style,
        # Filled during M0 oracle work; None means "not yet generated".
        "oracle_query": None,
        "expected_fixture_result": None,
        "screenshot_id": None,
    }
    return rec


def non_data_record(dashboard_key: str, el: dict, idx: int) -> dict:
    return {
        "id": f"{dashboard_key}-nondata-{idx}",
        "source_dashboard": dashboard_key,
        "page": el.get("tab_name"),
        "looker_type": el.get("type"),
        "ls_chart": LS_CHART_MAP.get(el.get("type"), "UNMAPPED"),
        "geometry": {k: el.get(k) for k in ("row", "col", "width", "height")},
        "payload": {k: v for k, v in el.items()
                    if k not in ("row", "col", "width", "height", "tab_name")},
    }


def control_record(dashboard_key: str, f: dict) -> dict:
    return {
        "id": f"{dashboard_key}-control-{f['name'].lower().replace(' ', '-')}",
        "source_dashboard": dashboard_key,
        "name": f["name"],
        "field": f.get("field"),
        "default_value": f.get("default_value", ""),
        "allow_multiple_values": f.get("allow_multiple_values", False),
        "ui_config": f.get("ui_config") or {},
        # Placement (page vs report level) is decided by the frozen M0
        # listener design; recorded here after that decision.
        "placement": None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--block-repo", required=True)
    ap.add_argument("--pinned-commit", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    spec = {
        "meta": {
            "source_repo": "looker-open-source/agent-analytics-block",
            "pinned_commit": args.pinned_commit,
            "source_files": [p for _, p in DASHBOARD_FILES],
            "generator": "tools/lookml_to_spec.py",
            "contract_issue":
                "GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK#365",
        },
        "controls": [],
        "charts": [],
        "non_data_elements": [],
        "tabs": [],
    }

    seen: dict = {}
    for key, rel in DASHBOARD_FILES:
        with open(f"{args.block_repo}/{rel}") as fh:
            dash = yaml.safe_load(fh)[0]
        for tab in dash.get("tabs") or []:
            spec["tabs"].append({
                "source_dashboard": key,
                "name": tab.get("name"),
                "title": tab.get("title", tab.get("name")),
            })
        for f in dash.get("filters") or []:
            spec["controls"].append(control_record(key, f))
        nd_idx = 0
        for el in dash.get("elements") or []:
            if el.get("model") and el.get("explore"):
                spec["charts"].append(element_record(key, el, seen))
            else:
                nd_idx += 1
                spec["non_data_elements"].append(
                    non_data_record(key, el, nd_idx))

    with open(args.out, "w") as fh:
        fh.write("# GENERATED by tools/lookml_to_spec.py — do not hand-edit.\n"
                 f"# Source: {spec['meta']['source_repo']}"
                 f"@{args.pinned_commit}\n")
        yaml.safe_dump(spec, fh, sort_keys=False, width=100,
                       allow_unicode=True)

    charts = spec["charts"]
    by_dash = {}
    for c in charts:
        by_dash.setdefault(c["source_dashboard"], []).append(c)
    print(f"charts: {len(charts)} "
          f"(usage {len(by_dash.get('usage', []))}, "
          f"performance {len(by_dash.get('performance', []))})")
    print(f"non_data_elements: {len(spec['non_data_elements'])}")
    print(f"controls: {len(spec['controls'])}")
    print(f"comparison scorecards: "
          f"{sum(1 for c in charts if c['comparison_previous_period'])}")
    print(f"percentile tiles: {sum(1 for c in charts if c['percentile'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
