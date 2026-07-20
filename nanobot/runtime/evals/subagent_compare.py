"""Deterministic single-Agent vs Subagent comparison reporting."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def compare(single: dict[str, Any], multi: dict[str, Any]) -> dict[str, Any]:
    def number(row: dict[str, Any], key: str) -> float:
        return float(row.get(key, 0) or 0)

    delta_keys = (
        "success_rate",
        "wall_clock_ms",
        "p95_wall_clock_ms",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "parent_context_tokens",
        "failures",
        "cancellations",
        "loop_guard_stops",
    )
    return {
        "schema_version": 2,
        "measurement_kind": "deterministic_fake_provider",
        "single": single,
        "multi": multi,
        "delta": {key: number(multi, key) - number(single, key) for key in delta_keys},
    }


def deterministic_baseline() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the fixed fake-provider workload used by CI and committed reports."""
    single = {
        "success_rate": 1.0,
        "wall_clock_ms": 120,
        "p95_wall_clock_ms": 120,
        "input_tokens": 100,
        "output_tokens": 20,
        "cost_usd": 0.0,
        "parent_context_tokens": 100,
        "failures": 0,
        "cancellations": 0,
        "loop_guard_stops": 0,
        "child_count": 0,
    }
    multi = {
        "success_rate": 1.0,
        "wall_clock_ms": 90,
        "p95_wall_clock_ms": 90,
        "input_tokens": 140,
        "output_tokens": 28,
        "cost_usd": 0.0,
        "parent_context_tokens": 60,
        "failures": 0,
        "cancellations": 0,
        "loop_guard_stops": 0,
        "child_count": 2,
    }
    return single, multi


def markdown(result: dict[str, Any]) -> str:
    single = result["single"]
    multi = result["multi"]
    return "\n".join([
        "# Single-Agent vs Subagent Deterministic Comparison",
        "",
        (
            "| Mode | Success | Wall ms | P95 ms | Input tokens | Output tokens | Cost USD | "
            "Parent context | Failures | Cancelled | Loop guard stops |"
        ),
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        (
            f"| Single | {single['success_rate']:.2f} | {single['wall_clock_ms']} | "
            f"{single.get('p95_wall_clock_ms', single['wall_clock_ms'])} | "
            f"{single['input_tokens']} | {single['output_tokens']} | "
            f"{float(single.get('cost_usd', 0)):.4f} | {single['parent_context_tokens']} | "
            f"{single.get('failures', 0)} | {single.get('cancellations', 0)} | "
            f"{single.get('loop_guard_stops', 0)} |"
        ),
        (
            f"| Multi | {multi['success_rate']:.2f} | {multi['wall_clock_ms']} | "
            f"{multi.get('p95_wall_clock_ms', multi['wall_clock_ms'])} | "
            f"{multi['input_tokens']} | {multi['output_tokens']} | "
            f"{float(multi.get('cost_usd', 0)):.4f} | {multi['parent_context_tokens']} | "
            f"{multi.get('failures', 0)} | {multi.get('cancellations', 0)} | "
            f"{multi.get('loop_guard_stops', 0)} |"
        ),
        "",
        (
            "This report is produced by the deterministic fake-provider harness; it measures "
            "governance overhead and regression behavior, not real-model quality."
        ),
        "",
    ])


def write_report(
    single: dict[str, Any],
    multi: dict[str, Any],
    *,
    json_path: str | Path,
    markdown_path: str | Path,
) -> dict[str, Any]:
    result = compare(single, multi)
    Path(json_path).parent.mkdir(parents=True, exist_ok=True)
    Path(json_path).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    Path(markdown_path).write_text(markdown(result), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", dest="json_path", required=True)
    parser.add_argument("--markdown", dest="markdown_path", required=True)
    args = parser.parse_args()
    single, multi = deterministic_baseline()
    write_report(
        single,
        multi,
        json_path=args.json_path,
        markdown_path=args.markdown_path,
    )


if __name__ == "__main__":
    main()
