#!/usr/bin/env python3
"""Regression guard for the two observed BQAA usage_metadata key families."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from oracle.gen_oracle import TOK_TOTAL  # noqa: E402
from tools.gen_events_tmpl import LLM_EXPRS  # noqa: E402


def ordered(expr: str, *needles: str) -> bool:
    positions = [expr.find(needle) for needle in needles]
    return all(pos >= 0 for pos in positions) and positions == sorted(positions)


def main() -> int:
    cases = {
        "prompt": (
            LLM_EXPRS["usage_prompt_tokens"],
            "$.prompt_token_count", "$.prompt_tokens",
            "usage_prompt_tokens"),
        "completion": (
            LLM_EXPRS["usage_completion_tokens"],
            "$.candidates_token_count", "$.completion_tokens",
            "usage_completion_tokens"),
        "total": (
            LLM_EXPRS["usage_total_tokens"],
            "$.total_token_count", "$.total_tokens",
            "usage_total_tokens"),
        "oracle total": (
            TOK_TOTAL, "$.total_token_count", "$.total_tokens",
            "usage_total_tokens"),
    }
    failures = [
        name for name, (expr, *needles) in cases.items()
        if not ordered(expr, *needles)
    ]
    if failures:
        for name in failures:
            print(f"FAIL: token precedence missing/out of order: {name}",
                  file=sys.stderr)
        return 1
    if not all("SAFE_CAST(" in expr for expr, *_ in cases.values()):
        print("FAIL: malformed metadata can still abort token extraction",
              file=sys.stderr)
        return 1
    print("token contract OK: pinned keys, observed alternate keys, then "
          "generated fallbacks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
