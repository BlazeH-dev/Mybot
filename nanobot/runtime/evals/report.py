"""Generate deterministic JSON and Markdown Runtime eval reports."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from nanobot.runtime.evals.metrics import load_case, metric_registry


def evaluate_cases(case_paths: list[str | Path]) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    fixture_hasher = hashlib.sha256()
    for path in sorted((Path(item) for item in case_paths), key=lambda item: str(item)):
        case = load_case(path)
        fixture_hasher.update(
            json.dumps(
                case.data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        fixture_hasher.update(b"\n")
        results: dict[str, Any] = {}
        for metric_name in case.data.get("metrics", []):
            metric = metric_registry.get(str(metric_name))
            if metric is None:
                results[str(metric_name)] = {
                    "passed": False,
                    "score": 0.0,
                    "issues": ["unknown metric"],
                    "hard_gate": True,
                }
                continue
            results[metric.name] = metric.score(case).as_dict()
        passed = all(result["passed"] for result in results.values())
        cases.append({"case_id": case.case_id, "passed": passed, "metrics": results})
    hard_failures = sum(
        1
        for case in cases
        for result in case["metrics"].values()
        if result.get("hard_gate") and not result.get("passed")
    )
    return {
        "schema_version": 1,
        "fixture_digest": fixture_hasher.hexdigest(),
        "passed": hard_failures == 0 and all(case["passed"] for case in cases),
        "case_count": len(cases),
        "hard_failures": hard_failures,
        "cases": cases,
    }


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Mybot Runtime Eval Report",
        "",
        f"- Cases: {report['case_count']}",
        f"- Hard failures: {report['hard_failures']}",
        f"- Overall: {'PASS' if report['passed'] else 'FAIL'}",
        "",
        "| Case | Result | Metrics |",
        "| --- | --- | --- |",
    ]
    for case in report["cases"]:
        metric_text = ", ".join(
            f"{name}={'PASS' if result['passed'] else 'FAIL'}"
            for name, result in case["metrics"].items()
        )
        lines.append(
            f"| {case['case_id']} | {'PASS' if case['passed'] else 'FAIL'} | {metric_text} |"
        )
    lines.append("")
    for case in report["cases"]:
        issues = [
            f"{name}: {issue}"
            for name, result in case["metrics"].items()
            for issue in result.get("issues", [])
        ]
        if issues:
            lines.extend((f"## {case['case_id']}", "", *[f"- {issue}" for issue in issues], ""))
    lines.extend(("", "该报告使用固定 fixture 与 fake provider，验证治理和回归开销；不代表真实模型质量。"))
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("cases", nargs="+")
    parser.add_argument("--json", dest="json_path", required=True)
    parser.add_argument("--markdown", dest="markdown_path", required=True)
    args = parser.parse_args()
    report = evaluate_cases(args.cases)
    Path(args.json_path).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_path).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    Path(args.markdown_path).write_text(markdown_report(report), encoding="utf-8")
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
