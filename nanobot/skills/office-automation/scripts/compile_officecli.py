"""Compile report or slide DSL into a replayable OfficeCLI batch JSON file."""

from __future__ import annotations

import argparse
from pathlib import Path

from _common import write_json
from officecli_backend import compile_commands


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", required=True, choices=("docx", "pptx"))
    parser.add_argument("--dsl", dest="dsl_path", required=True, type=Path)
    parser.add_argument("--facts", dest="facts_path", required=True, type=Path)
    parser.add_argument("--out", dest="output_path", required=True, type=Path)
    args = parser.parse_args()

    commands = compile_commands(
        args.kind,
        dsl_path=args.dsl_path,
        facts_path=args.facts_path,
    )
    write_json(args.output_path, commands)


if __name__ == "__main__":
    main()
