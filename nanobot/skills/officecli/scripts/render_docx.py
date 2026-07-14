"""Render the optional grounded report DSL through the OfficeCLI skill."""

from __future__ import annotations

import argparse
from pathlib import Path

from officecli_backend import render_with_officecli


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsl", dest="dsl_path", required=True, type=Path)
    parser.add_argument("--facts", dest="facts_path", required=True, type=Path)
    parser.add_argument("--out", dest="output_path", required=True, type=Path)
    parser.add_argument("--officecli-bin")
    parser.add_argument("--allow-unverified-officecli", action="store_true")
    parser.add_argument("--preview-dir", type=Path)
    args = parser.parse_args()

    render_with_officecli(
        "docx",
        dsl_path=args.dsl_path,
        facts_path=args.facts_path,
        output_path=args.output_path,
        binary=args.officecli_bin,
        allow_unverified_version=args.allow_unverified_officecli,
        preview_dir=args.preview_dir,
    )


if __name__ == "__main__":
    main()
