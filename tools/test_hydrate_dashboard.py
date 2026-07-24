#!/usr/bin/env python3
"""Unit tests for the public Looker Studio hydration contract."""

import contextlib
import io
import pathlib
import sys
import urllib.parse

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.hydrate_dashboard import (  # noqa: E402
    ID_RE,
    LOCATION_RE,
    PREFIX_RE,
    PROJECT_RE,
    build_link,
    require_identifier,
    table_preflight_sql,
)


def expect_invalid(label: str, value: str, pattern) -> None:
    try:
        require_identifier(label, value, pattern)
    except ValueError:
        return
    raise AssertionError(f"{label} accepted unsafe value {value!r}")


def main() -> int:
    for label, value, pattern in [
        ("project", "project;DROP", PROJECT_RE),
        ("project", "UPPERCASE", PROJECT_RE),
        ("dataset", "data`set", ID_RE),
        ("dataset", "data-set", ID_RE),
        ("prefix", "v,evil", PREFIX_RE),
        ("prefix", "v.*", PREFIX_RE),
        ("location", "US;echo", LOCATION_RE),
    ]:
        expect_invalid(label, value, pattern)

    link = build_link(
        "customer-project-123",
        "agent_analytics",
        "v",
        "billing-project-123",
        "My BQAA Dashboard",
    )
    parsed = urllib.parse.urlparse(link)
    params = urllib.parse.parse_qs(parsed.query)
    report = yaml.safe_load(
        (ROOT / "bindings/report_template.yaml").read_text()
    )
    assert parsed.scheme == "https"
    assert parsed.netloc == "lookerstudio.google.com"
    assert params["c.reportId"] == ["5a3f85ef-fc9c-4730-8ef2-8ef9129ddb40"]
    assert params["c.mode"] == ["view"]
    assert params["ds.ds230.billingProjectId"] == ["billing-project-123"]
    assert params["ds.ds230.refreshFields"] == ["false"]
    assert report["credential_mode"] == "VIEWERS"
    assert report["link_access"] == "PUBLIC"
    assert report["publishing_mode"] == "MANUAL"
    assert report["default_date_range"] == {
        "mode": "rolling",
        "start_offset_days": 365,
        "end_offset_days": 1,
        "include_today": False,
        "page_scope": "all_dashboard_pages",
    }
    replacements = params["ds.ds230.sqlReplace"][0].split(",")
    assert replacements == [
        "test-project-0728-467323",
        "customer-project-123",
        "bqaa_fixture_adk_1_27_0",
        "agent_analytics",
        "vsentinelbqaa",
        "v",
    ]
    assert "connector" not in " ".join(params)
    assert "CUSTOM_QUERY" not in link

    anchor_sql = table_preflight_sql(
        "customer-project-123", "agent_analytics", "agent_events"
    )
    for required in [
        "BASE TABLE",
        "'timestamp' AS column_name",
        "'content' AS column_name",
        "'attributes' AS column_name",
        "'latency_ms' AS column_name",
    ]:
        assert required in anchor_sql

    # Keep test output free of the generated public URL.
    with contextlib.redirect_stdout(io.StringIO()):
        pass
    print(
        "hydration contract OK: identifiers fail closed, BQAA table anchored, "
        "single-source Linking API URL deterministic"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
