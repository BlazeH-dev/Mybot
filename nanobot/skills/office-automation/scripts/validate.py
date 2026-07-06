"""Validate office report/slide DSL and optional delivery plan completion."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from _common import collect_fact_refs, load_facts, read_json, write_json


def _issue(code: str, message: str, *, path: str = "$", severity: str = "error") -> dict[str, str]:
    return {
        "severity": severity,
        "code": code,
        "path": path,
        "message": message,
    }


def _validate_report_dsl(dsl: dict[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if not isinstance(dsl.get("title"), str) or not dsl["title"].strip():
        issues.append(_issue("missing_title", "report title is required", path="$.title"))

    sections = dsl.get("sections")
    if not isinstance(sections, list) or not sections:
        issues.append(_issue("missing_sections", "report must contain at least one section", path="$.sections"))
        return issues

    for index, section in enumerate(sections):
        base = f"$.sections[{index}]"
        if not isinstance(section, dict):
            issues.append(_issue("invalid_section", "section must be an object", path=base))
            continue
        if not isinstance(section.get("title"), str) or not section["title"].strip():
            issues.append(_issue("missing_section_title", "section title is required", path=f"{base}.title"))
        blocks = section.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            issues.append(_issue("missing_blocks", "section must contain at least one block", path=f"{base}.blocks"))
    return issues


def _validate_slide_dsl(dsl: dict[str, Any], constraints: dict[str, Any] | None) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    slides = dsl.get("slides")
    if not isinstance(slides, list) or not slides:
        return [_issue("missing_slides", "slide deck must contain at least one slide", path="$.slides")]

    max_pages = None
    if constraints:
        max_pages = constraints.get("outputs", {}).get("pptx_max_pages")
    if isinstance(max_pages, int) and len(slides) > max_pages:
        issues.append(
            _issue(
                "too_many_slides",
                f"slide count {len(slides)} exceeds limit {max_pages}",
                path="$.slides",
            )
        )

    for index, slide in enumerate(slides):
        base = f"$.slides[{index}]"
        if not isinstance(slide, dict):
            issues.append(_issue("invalid_slide", "slide must be an object", path=base))
            continue
        if not isinstance(slide.get("title"), str) or not slide["title"].strip():
            issues.append(_issue("missing_slide_title", "slide title is required", path=f"{base}.title"))
    return issues


def _validate_fact_refs(dsl: dict[str, Any], facts_path: Path | None) -> list[dict[str, str]]:
    if facts_path is None:
        return []
    facts = load_facts(facts_path)
    known = set(facts)
    missing = sorted(collect_fact_refs(dsl) - known)
    return [
        _issue("unknown_fact_ref", f"fact_ref does not exist: {fact_id}", path="$")
        for fact_id in missing
    ]


def _validate_plan(plan: dict[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if not isinstance(plan.get("goal"), str) or not plan["goal"].strip():
        issues.append(_issue("missing_goal", "plan goal is required", path="$.goal"))
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        issues.append(_issue("missing_steps", "plan must include at least one step", path="$.steps"))
        return issues

    ids: set[str] = set()
    valid_statuses = {"pending", "in_progress", "done", "skipped"}
    for index, step in enumerate(steps):
        base = f"$.steps[{index}]"
        if not isinstance(step, dict):
            issues.append(_issue("invalid_step", "plan step must be an object", path=base))
            continue
        step_id = step.get("id")
        if not isinstance(step_id, str) or not step_id:
            issues.append(_issue("missing_step_id", "step id is required", path=f"{base}.id"))
        elif step_id in ids:
            issues.append(_issue("duplicate_step_id", f"duplicate step id: {step_id}", path=f"{base}.id"))
        else:
            ids.add(step_id)
        if step.get("status") not in valid_statuses:
            issues.append(_issue("invalid_step_status", "step status is invalid", path=f"{base}.status"))
        artifacts = step.get("expected_artifacts", [])
        if not isinstance(artifacts, list):
            issues.append(
                _issue("invalid_expected_artifacts", "expected_artifacts must be a list", path=f"{base}.expected_artifacts")
            )
    return issues


def _check_plan_artifacts(plan: dict[str, Any], artifact_root: Path | None) -> list[dict[str, str]]:
    if artifact_root is None:
        return []
    issues: list[dict[str, str]] = []
    for step_index, step in enumerate(plan.get("steps", [])):
        if not isinstance(step, dict):
            continue
        for artifact in step.get("expected_artifacts", []):
            if not isinstance(artifact, str) or not artifact:
                continue
            path = artifact_root / artifact
            if not path.exists():
                issues.append(
                    _issue(
                        "missing_artifact",
                        f"planned artifact was not delivered: {artifact}",
                        path=f"$.steps[{step_index}].expected_artifacts",
                    )
                )
    return issues


def validate_payload(
    *,
    dsl_paths: list[Path],
    facts_path: Path | None,
    constraints_path: Path | None,
    plan_path: Path | None,
    artifact_root: Path | None,
) -> dict[str, Any]:
    constraints = read_json(constraints_path) if constraints_path else None
    issues: list[dict[str, str]] = []
    checked: list[str] = []

    for dsl_path in dsl_paths:
        dsl = read_json(dsl_path)
        dsl_type = dsl.get("type")
        if dsl_type == "report":
            issues.extend(_validate_report_dsl(dsl))
        elif dsl_type == "slides":
            issues.extend(_validate_slide_dsl(dsl, constraints))
        else:
            issues.append(_issue("unknown_dsl_type", "DSL type must be report or slides", path="$.type"))
        issues.extend(_validate_fact_refs(dsl, facts_path))
        checked.append(str(dsl_path))

    if plan_path:
        plan = read_json(plan_path)
        issues.extend(_validate_plan(plan))
        issues.extend(_check_plan_artifacts(plan, artifact_root))
        checked.append(str(plan_path))

    return {
        "schema_version": 1,
        "passed": not issues,
        "checked": checked,
        "issues": issues,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsl", dest="dsl_paths", action="append", type=Path, default=[])
    parser.add_argument("--facts", dest="facts_path", type=Path)
    parser.add_argument("--constraints", dest="constraints_path", type=Path)
    parser.add_argument("--plan", dest="plan_path", type=Path)
    parser.add_argument("--artifact-root", dest="artifact_root", type=Path)
    parser.add_argument("--out", dest="output_path", required=True, type=Path)
    args = parser.parse_args()

    report = validate_payload(
        dsl_paths=args.dsl_paths,
        facts_path=args.facts_path,
        constraints_path=args.constraints_path,
        plan_path=args.plan_path,
        artifact_root=args.artifact_root,
    )
    write_json(args.output_path, report)
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
