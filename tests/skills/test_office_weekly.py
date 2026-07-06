from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from docx import Document
from openpyxl import Workbook
from pptx import Presentation

from nanobot.agent.skills import SkillsLoader

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "office_weekly"
SKILL_DIR = ROOT / "nanobot" / "skills" / "office-automation"
SCRIPTS_DIR = SKILL_DIR / "scripts"
REFERENCES_DIR = SKILL_DIR / "references"


def _run_script(script_name: str, args: list[str | Path], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script_name), *[str(arg) for arg in args]],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"{script_name} failed with {result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _extract_fixture_facts(tmp_path: Path) -> Path:
    facts_path = tmp_path / "verified_facts.json"
    _run_script(
        "extract_facts.py",
        [
            "--in",
            FIXTURE_DIR / "sales_data.xlsx",
            "--spec",
            REFERENCES_DIR / "metric_spec.example.json",
            "--out",
            facts_path,
        ],
    )
    return facts_path


def test_office_skill_is_discoverable_in_summary() -> None:
    loader = SkillsLoader(ROOT)

    entries = loader.list_skills(filter_unavailable=True)
    assert "office-automation" in {entry["name"] for entry in entries}

    summary = loader.build_skills_summary()
    assert "**office-automation**" in summary
    assert "Excel analysis" in summary
    assert str(SKILL_DIR / "SKILL.md") in summary


def test_inspect_workbook_emits_compact_schema(tmp_path: Path) -> None:
    output_path = tmp_path / "workbook_schema.json"

    _run_script(
        "inspect_workbook.py",
        [
            "--in",
            FIXTURE_DIR / "sales_data.xlsx",
            "--out",
            output_path,
        ],
    )

    payload = _read_json(output_path)
    assert payload["schema_version"] == 1
    sheet = payload["sheets"][0]
    assert sheet["name"] == "sales_data"
    assert sheet["row_count"] == 8
    columns = {column["name"]: column for column in sheet["columns"]}
    assert columns["gmv_cny"]["type"] == "number"
    assert columns["region"]["type"] == "text"


def test_extract_facts_matches_expected_metrics(tmp_path: Path) -> None:
    facts_path = _extract_fixture_facts(tmp_path)
    payload = _read_json(facts_path)
    facts = {fact["fact_id"]: fact for fact in payload["facts"]}
    expected = {
        metric["id"]: metric
        for metric in _read_json(FIXTURE_DIR / "expected_metrics.json")["metrics"]
    }

    assert set(expected).issubset(facts)
    for fact_id, expected_metric in expected.items():
        fact = facts[fact_id]
        if isinstance(expected_metric["value"], float):
            assert abs(fact["value"] - expected_metric["value"]) < 1e-10
        else:
            assert fact["value"] == expected_metric["value"]
        assert fact["display_value"] == expected_metric["display_value"]
        assert fact["confidence"] == 1.0


def test_extract_facts_reports_missing_columns(tmp_path: Path) -> None:
    spec_path = tmp_path / "bad_metric_spec.json"
    spec_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sheet": "sales_data",
                "metrics": [
                    {
                        "fact_id": "missing",
                        "name": "Missing",
                        "calculation": "sum",
                        "column": "does_not_exist",
                        "format": "integer",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = _run_script(
        "extract_facts.py",
        [
            "--in",
            FIXTURE_DIR / "sales_data.xlsx",
            "--spec",
            spec_path,
            "--out",
            tmp_path / "facts.json",
        ],
        check=False,
    )

    assert result.returncode != 0
    assert "missing required column" in result.stderr


def test_extract_facts_treats_empty_numeric_cells_as_zero(tmp_path: Path) -> None:
    workbook_path = tmp_path / "with_empty.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "sales_data"
    sheet.append(["gmv_cny", "orders"])
    sheet.append([None, 1])
    sheet.append([100, 2])
    workbook.save(workbook_path)

    spec_path = tmp_path / "metric_spec.json"
    spec_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sheet": "sales_data",
                "metrics": [
                    {
                        "fact_id": "total_gmv_cny",
                        "name": "GMV total",
                        "calculation": "sum",
                        "column": "gmv_cny",
                        "unit": "CNY",
                        "format": "currency_cny",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    facts_path = tmp_path / "facts.json"

    _run_script(
        "extract_facts.py",
        ["--in", workbook_path, "--spec", spec_path, "--out", facts_path],
    )

    fact = _read_json(facts_path)["facts"][0]
    assert fact["value"] == 100
    assert fact["display_value"] == "CNY 100"


def test_validate_catches_unknown_fact_and_slide_limit(tmp_path: Path) -> None:
    facts_path = _extract_fixture_facts(tmp_path)
    invalid_slide_dsl = tmp_path / "invalid_slide_dsl.json"
    invalid_slide_dsl.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "type": "slides",
                "slides": [
                    {
                        "title": f"Slide {index}",
                        "metrics": [{"label": "Bad", "fact_ref": "not_a_fact"}],
                    }
                    for index in range(7)
                ],
            }
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "quality_report.json"

    result = _run_script(
        "validate.py",
        [
            "--dsl",
            invalid_slide_dsl,
            "--facts",
            facts_path,
            "--constraints",
            FIXTURE_DIR / "expected_constraints.json",
            "--out",
            report_path,
        ],
        check=False,
    )

    assert result.returncode != 0
    report = _read_json(report_path)
    assert report["passed"] is False
    codes = {issue["code"] for issue in report["issues"]}
    assert {"too_many_slides", "unknown_fact_ref"}.issubset(codes)


def test_office_weekly_deterministic_artifact_chain(tmp_path: Path) -> None:
    artifact_root = tmp_path / ".nanobot-runtime" / "artifacts" / "task_office_weekly"
    artifact_root.mkdir(parents=True)
    workbook_schema = artifact_root / "workbook_schema.json"
    facts_path = artifact_root / "verified_facts.json"
    report_dsl = artifact_root / "report_dsl.json"
    slide_dsl = artifact_root / "slide_dsl.json"
    plan_path = artifact_root / "plan.json"
    quality_report = artifact_root / "quality_report.json"
    docx_path = artifact_root / "weekly_report.docx"
    pptx_path = artifact_root / "weekly_review.pptx"

    shutil.copyfile(FIXTURE_DIR / "fixed_report_dsl.json", report_dsl)
    shutil.copyfile(FIXTURE_DIR / "fixed_slide_dsl.json", slide_dsl)
    shutil.copyfile(FIXTURE_DIR / "fixed_plan_done.json", plan_path)

    _run_script(
        "inspect_workbook.py",
        ["--in", FIXTURE_DIR / "sales_data.xlsx", "--out", workbook_schema],
    )
    _run_script(
        "extract_facts.py",
        [
            "--in",
            FIXTURE_DIR / "sales_data.xlsx",
            "--spec",
            REFERENCES_DIR / "metric_spec.example.json",
            "--out",
            facts_path,
        ],
    )
    _run_script(
        "validate.py",
        [
            "--dsl",
            report_dsl,
            "--dsl",
            slide_dsl,
            "--facts",
            facts_path,
            "--constraints",
            FIXTURE_DIR / "expected_constraints.json",
            "--plan",
            plan_path,
            "--out",
            quality_report,
        ],
    )
    _run_script(
        "render_docx.py",
        ["--dsl", report_dsl, "--facts", facts_path, "--out", docx_path],
    )
    _run_script(
        "render_pptx.py",
        [
            "--dsl",
            slide_dsl,
            "--facts",
            facts_path,
            "--constraints",
            FIXTURE_DIR / "expected_constraints.json",
            "--out",
            pptx_path,
        ],
    )
    _run_script(
        "validate.py",
        [
            "--dsl",
            report_dsl,
            "--dsl",
            slide_dsl,
            "--facts",
            facts_path,
            "--constraints",
            FIXTURE_DIR / "expected_constraints.json",
            "--plan",
            plan_path,
            "--artifact-root",
            artifact_root,
            "--out",
            quality_report,
        ],
    )

    facts = {fact["fact_id"]: fact for fact in _read_json(facts_path)["facts"]}
    report = _read_json(quality_report)
    assert report["passed"] is True

    document = Document(docx_path)
    docx_text = "\n".join(
        [paragraph.text for paragraph in document.paragraphs]
        + [cell.text for table in document.tables for row in table.rows for cell in row.cells]
    )
    assert "{{fact:" not in docx_text
    for fact_id in ["total_gmv_cny", "total_orders", "conversion_rate", "top_region_by_gmv"]:
        assert facts[fact_id]["display_value"] in docx_text

    deck = Presentation(pptx_path)
    constraints = _read_json(FIXTURE_DIR / "expected_constraints.json")
    assert len(deck.slides) <= constraints["outputs"]["pptx_max_pages"]
    pptx_text = "\n".join(
        shape.text
        for slide in deck.slides
        for shape in slide.shapes
        if hasattr(shape, "text")
    )
    assert "{{fact:" not in pptx_text
    for fact_id in ["total_gmv_cny", "total_orders", "conversion_rate"]:
        assert facts[fact_id]["display_value"] in pptx_text
    for slide in deck.slides:
        assert any(shape.text.strip() for shape in slide.shapes if hasattr(shape, "text"))

    for artifact in constraints["outputs"]["required_artifacts"]:
        assert (artifact_root / artifact).exists()
