"""Deterministic native vs PTC Code Mode comparison reporting."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def compare(native: dict[str, Any], ptc: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "success_rate",
        "wall_clock_ms",
        "llm_round_trips",
        "tool_calls",
        "model_visible_tool_result_chars",
        "input_tokens",
        "output_tokens",
        "failures",
    )
    return {
        "schema_version": 1,
        "measurement_kind": "deterministic_fake_provider",
        "native": native,
        "ptc": ptc,
        "delta": {
            key: float(ptc.get(key, 0) or 0) - float(native.get(key, 0) or 0)
            for key in keys
        },
    }


def deterministic_baseline() -> tuple[dict[str, Any], dict[str, Any]]:
    """Fixed three-read aggregation workload used by CI documentation."""
    native = {
        "success_rate": 1.0,
        "wall_clock_ms": 150,
        "llm_round_trips": 4,
        "tool_calls": 3,
        "model_visible_tool_result_chars": 3000,
        "input_tokens": 900,
        "output_tokens": 180,
        "failures": 0,
    }
    ptc = {
        "success_rate": 1.0,
        "wall_clock_ms": 70,
        "llm_round_trips": 2,
        "tool_calls": 3,
        "model_visible_tool_result_chars": 240,
        "input_tokens": 620,
        "output_tokens": 130,
        "failures": 0,
    }
    return native, ptc


def markdown(result: dict[str, Any]) -> str:
    native = result["native"]
    ptc = result["ptc"]
    rows = []
    for label, row in (("Native", native), ("PTC", ptc)):
        rows.append(
            f"| {label} | {row['success_rate']:.2f} | {row['wall_clock_ms']} | "
            f"{row['llm_round_trips']} | {row['tool_calls']} | "
            f"{row['model_visible_tool_result_chars']} | {row['input_tokens']} | "
            f"{row['output_tokens']} | {row['failures']} |"
        )
    return "\n".join([
        "# Native vs PTC Deterministic Comparison",
        "",
        "| Mode | Success | Wall ms | LLM round-trips | Tool calls | Visible result chars | Input tokens | Output tokens | Failures |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        *rows,
        "",
        "This fixed fake-provider workload validates reporting and protocol expectations; it does not measure real-model quality or production savings.",
        "",
    ])


def write_report(*, json_path: str | Path, markdown_path: str | Path) -> dict[str, Any]:
    native, ptc = deterministic_baseline()
    result = compare(native, ptc)
    Path(json_path).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    Path(markdown_path).write_text(markdown(result), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", dest="json_path", required=True)
    parser.add_argument("--markdown", dest="markdown_path", required=True)
    args = parser.parse_args()
    write_report(json_path=args.json_path, markdown_path=args.markdown_path)


if __name__ == "__main__":
    main()
