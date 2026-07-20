"""CLI helper: convert Mybot JSONL traces to a minimal OTLP/JSON envelope."""

from __future__ import annotations

import argparse

from nanobot.runtime.trace import export_jsonl_to_otlp


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("output")
    args = parser.parse_args()
    count = export_jsonl_to_otlp(args.input, args.output)
    print(f"exported {count} span(s) to {args.output}")


if __name__ == "__main__":
    main()
