from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.runner import AgentRunner
from nanobot.evaluations.catalog import ROOT
from nanobot.evaluations.skill_evolution import (
    DERIVED_SKILL,
    AnalysisResponse,
    InterventionProbe,
    SkillEvolutionService,
    SkillEvolutionStore,
    _numeric_score,
    _skill_editor_tool_call_budget,
    _tree_digest,
)
from nanobot.evaluations.skill_evolution_tools import (
    ReadEvolutionEvidenceTool,
    WriteSkillFileTool,
)
from nanobot.providers.base import LLMResponse, ToolCallRequest


class FakeEvaluations:
    def __init__(self, job: dict, cases: list[dict]) -> None:
        self.job = job
        self.case_rows = cases

    def get(self, run_id: str):
        return self.job if run_id == self.job["job_id"] else None

    def cases(self, run_id: str):
        assert run_id == self.job["job_id"]
        return self.case_rows


class FakeResults:
    def __init__(self, cases: list[dict]) -> None:
        self.cases = cases

    def list_runs(self, *, limit: int = 50):
        return {
            "available": True,
            "runs": [{
                "dataset_run_id": "remote-1",
                "name": "mybot-office-release-ocb-officecli-gpt-job-job-1",
                "cases": self.cases,
            }],
        }

    def list_runs_by_ids(self, run_ids: set[str]):
        assert run_ids == {"remote-1"}
        return self.list_runs()


def _service(tmp_path: Path) -> SkillEvolutionService:
    local_cases = [
        {"case_id": "1", "benchmark": "ocb", "skill": "officecli", "model_preset": "gpt"},
        {"case_id": "2", "benchmark": "ocb", "skill": "officecli", "model_preset": "gpt"},
    ]
    remote_cases = [
        {**local_cases[0], "scores": {"mybot_score": 0.25}},
        {**local_cases[1], "scores": {"mybot_score": 0.9}},
    ]
    job = {
        "job_id": "job-1",
        "resume_token": "job-1",
        "profile": "office-release",
        "dataset_run_ids": ["remote-1"],
    }
    return SkillEvolutionService(
        FakeEvaluations(job, local_cases),
        FakeResults(remote_cases),
        SkillEvolutionStore(tmp_path / "evolution"),
    )


def _workflow_category(
    category_id: str,
    finding_ids: list[str],
    case_ids: list[str],
    *,
    owner: str = "skill",
) -> dict:
    return {
        "category_id": category_id,
        "title": f"Category {category_id}",
        "root_cause": "The final answer omits required grounded claims.",
        "fix_owner": owner,
        "confidence": 0.9,
        "finding_ids": finding_ids,
        "case_ids": case_ids,
        "risk": "May add a short verification step.",
        "should_modify_skill": owner == "skill",
        "intervention": {
            "repair_mode": "workflow_required" if owner == "skill" else "not_skill_repairable",
            "trigger": "A request requires exact claims from a supplied source.",
            "required_action": "Build and verify a claim-to-source checklist.",
            "entrypoint": "SKILL.md",
            "required_outputs": [],
            "final_answer_check": ["Every requested claim is present and sourced."],
            "observable_success": "The trace and final answer contain the completed checklist.",
        },
    }


def _script_category(category_id: str, case_ids: list[str]) -> dict:
    return {
        "category_id": category_id,
        "title": "Inspect formulas",
        "root_cause": "Formula inspection omitted effective formulas.",
        "fix_owner": "skill",
        "confidence": 0.9,
        "finding_ids": ["f1"],
        "case_ids": case_ids,
        "risk": "Parser compatibility.",
        "should_modify_skill": True,
        "intervention": {
            "repair_mode": "script_required",
            "trigger": "A workbook request asks for formulas.",
            "required_action": "Inspect every requested formula cell.",
            "entrypoint": "scripts/inspect_formulas.py",
            "required_outputs": ["requested_cells", "formulas"],
            "final_answer_check": ["Every requested cell is reported."],
            "observable_success": "Trace calls the script and returns complete formula JSON.",
        },
    }


def test_analysis_response_defaults_missing_skill_recommendation_to_false() -> None:
    response = AnalysisResponse.model_validate({
        "summary": "The failure is visible, but no automatic Skill edit was recommended.",
        "findings": [{
            "finding_id": "ev-1",
            "case_ids": ["1"],
            "root_cause": "The generated document omitted a required table.",
            "fix_owner": "skill",
            "confidence": 0.8,
            "evidence_refs": ["ev-1"],
        }],
    })

    assert response.findings[0].should_modify_skill is False
    assert response.model_dump()["findings"][0]["should_modify_skill"] is False
    finding_schema = AnalysisResponse.model_json_schema()["$defs"]["RootCauseFinding"]
    assert "should_modify_skill" not in finding_schema["required"]
    assert finding_schema["properties"]["should_modify_skill"]["default"] is False


def test_cross_batch_category_synthesis_assigns_findings_and_unions_cases(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    task_id = "task-categories"
    service.store.write({"task_id": task_id, "created_at": "x", "cancel_requested": False})
    response = {
        "summary": "Formula failures share one deterministic repair.",
        "categories": [{
            "title": "Expand shared formulas",
            "root_cause": "Shared formula cells were reported without their effective formulas.",
            "fix_owner": "skill",
            "confidence": 0.95,
            "finding_ids": ["f1", "f2"],
            "risk": "Workbook parsers differ in shared formula representation.",
            "should_modify_skill": True,
            "intervention": {
                "repair_mode": "script_required",
                "trigger": "A spreadsheet formula inspection includes shared formulas.",
                "required_action": "Expand and return each requested cell formula.",
                "entrypoint": "scripts/inspect_formulas.py",
                "required_outputs": ["formulas"],
                "final_answer_check": ["Report every requested cell."],
                "observable_success": "Trace calls the script and returns formula JSON.",
            },
        }],
    }
    provider = SimpleNamespace(chat_with_retry=AsyncMock(return_value=LLMResponse(
        json.dumps(response),
        finish_reason="stop",
        usage={"total_tokens": 42},
    )))
    preset = SimpleNamespace(
        model="fake-model",
        max_tokens=4096,
        temperature=0.1,
        reasoning_effort=None,
    )
    findings = [
        {"finding_id": "f1", "case_ids": ["1", "2"], "fix_owner": "skill"},
        {"finding_id": "f2", "case_ids": ["2", "3"], "fix_owner": "skill"},
    ]

    categories, usage = asyncio.run(
        service._synthesize_categories(task_id, preset, provider, findings)
    )

    assert categories[0]["category_id"] == "c1"
    assert categories[0]["case_ids"] == ["1", "2", "3"]
    assert categories[0]["intervention"]["required_outputs"] == [
        "formulas",
        "requested_cells",
    ]
    assert usage["total_tokens"] == 42


def test_category_synthesis_splits_requests_by_finding_owner(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task_id = "task-owner-groups"
    service.store.write({"task_id": task_id, "created_at": "x", "cancel_requested": False})
    skill_response = {
        "summary": "Skill-owned findings grouped.",
        "categories": [{
            "title": "Grounded workflow",
            "root_cause": "The answer omitted grounded claims.",
            "fix_owner": "skill",
            "confidence": 0.9,
            "finding_ids": ["f1"],
            "risk": "Extra verification.",
            "should_modify_skill": True,
            "intervention": {
                "repair_mode": "workflow_required",
                "trigger": "A source-grounded answer is requested.",
                "required_action": "Verify every requested claim.",
                "entrypoint": "SKILL.md",
                "required_outputs": [],
                "final_answer_check": ["Every requested claim is present."],
                "observable_success": "The answer covers the checklist.",
            },
        }],
    }
    runtime_response = {
        "summary": "Runtime-owned findings excluded.",
        "categories": [{
            "title": "Runtime exhaustion",
            "root_cause": "The runtime stopped before completion.",
            "fix_owner": "runtime",
            "confidence": 0.9,
            "finding_ids": ["f2"],
            "risk": "Not repairable by the Skill.",
            "should_modify_skill": False,
            "intervention": {
                "repair_mode": "not_skill_repairable",
                "trigger": "The runtime exhausts its execution budget.",
                "required_action": "Fix the runtime policy.",
                "entrypoint": "runtime",
                "required_outputs": [],
                "final_answer_check": ["Do not claim a Skill repair."],
                "observable_success": "The category is excluded from editing.",
            },
        }],
    }
    provider = SimpleNamespace(chat_with_retry=AsyncMock(side_effect=[
        LLMResponse(json.dumps(skill_response), finish_reason="stop", usage={"total_tokens": 10}),
        LLMResponse(json.dumps(runtime_response), finish_reason="stop", usage={"total_tokens": 20}),
    ]))
    preset = SimpleNamespace(
        model="fake-model",
        max_tokens=4096,
        temperature=0.1,
        reasoning_effort=None,
    )

    categories, usage = asyncio.run(service._synthesize_categories(
        task_id,
        preset,
        provider,
        [
            {"finding_id": "f1", "case_ids": ["1"], "fix_owner": "skill"},
            {"finding_id": "f2", "case_ids": ["2"], "fix_owner": "runtime"},
        ],
    ))

    assert provider.chat_with_retry.await_count == 2
    assert [row["category_id"] for row in categories] == ["c1", "c2"]
    assert categories[1]["intervention"]["repair_mode"] == "not_skill_repairable"
    assert usage["total_tokens"] == 30


def test_reanalyze_resumes_categories_from_existing_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    task_id = "task-resume-categories"
    analysis = {
        "analysis_id": "a1",
        "digest": "analysis-digest",
        "evidence_digest": "evidence-digest",
        "summary": "Existing batch findings.",
        "findings": [{"finding_id": "f1", "case_ids": ["1"], "fix_owner": "skill"}],
        "categories": [],
    }
    service.store.write({
        "task_id": task_id,
        "created_at": "x",
        "status": "failed",
        "phase": "analyzing",
        "cancel_requested": True,
        "optimizer_model": "optimizer",
        "active_analysis_id": "a1",
        "analyses": [analysis],
        "revisions": [],
    })
    started = []
    monkeypatch.setattr(
        service,
        "_start_thread",
        lambda task, target, *args: started.append((task, target, args)),
    )
    monkeypatch.setattr(
        service,
        "_run_and_cases",
        lambda _run_id: (_ for _ in ()).throw(AssertionError("Evidence must not be loaded")),
    )

    task = service.reanalyze(task_id)

    assert task["status"] == "analyzing"
    assert task["cancel_requested"] is False
    assert len(started) == 1
    assert started[0][1] == service._run_category_retry
    assert started[0][2][1]["findings"] == analysis["findings"]


def test_non_repairable_category_cannot_start_evolution(tmp_path: Path) -> None:
    service = _service(tmp_path)
    analysis = {
        "analysis_id": "a1",
        "digest": "digest",
        "findings": [{"finding_id": "f1", "fix_owner": "runtime"}],
        "categories": [_workflow_category("c1", ["f1"], ["1"], owner="runtime")],
    }
    service.store.write({
        "task_id": "task-category-owner",
        "created_at": "x",
        "active_analysis_id": "a1",
        "analyses": [analysis],
        "revisions": [],
    })

    with pytest.raises(ValueError, match="not repairable by the Skill"):
        service.start_evolution("task-category-owner", category_ids=["c1"])


def test_incomplete_reason_categorization_cannot_start_evolution(tmp_path: Path) -> None:
    service = _service(tmp_path)
    analysis = {
        "analysis_id": "a1",
        "digest": "digest",
        "findings": [{"finding_id": "f1", "fix_owner": "skill"}],
        "categories": [],
    }
    service.store.write({
        "task_id": "task-incomplete-categories",
        "created_at": "x",
        "active_analysis_id": "a1",
        "analyses": [analysis],
        "revisions": [],
    })

    with pytest.raises(ValueError, match="reason categorization is incomplete"):
        service.start_evolution("task-incomplete-categories", ["f1"])

    task = service.get("task-incomplete-categories")
    assert task is not None
    assert task["revisions"] == []


def test_numeric_score_accepts_serialized_langfuse_score() -> None:
    assert _numeric_score({
        "scores": {
            "mybot_score": {
                "dataType": "NUMERIC",
                "name": "mybot-ocb-judge-v1",
                "value": 0.5,
            },
        },
    }) == pytest.approx(0.5)


def test_bad_cases_uses_remote_scores_and_threshold(tmp_path: Path) -> None:
    payload = _service(tmp_path).bad_cases("job-1", 0.6)

    assert [case["case_id"] for case in payload["cases"]] == ["1"]
    assert payload["cases"][0]["score"] == pytest.approx(0.25)
    assert payload["partial_regression_check"] is True


def test_analyze_accepts_more_than_twenty_cases_and_returns_background_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_cases = [
        {
            "case_id": str(index),
            "benchmark": "ocb",
            "skill": "officecli",
            "model_preset": "gpt-5-6-luna",
        }
        for index in range(21)
    ]
    remote_cases = [
        {**case, "scores": {"mybot_score": 0.25}}
        for case in local_cases
    ]
    job = {
        "job_id": "job-many",
        "resume_token": "job-many",
        "profile": "office-release",
        "dataset_run_ids": ["remote-1"],
    }
    service = SkillEvolutionService(
        FakeEvaluations(job, local_cases),
        FakeResults(remote_cases),
        SkillEvolutionStore(tmp_path / "evolution"),
    )
    monkeypatch.setattr(service, "_optimizer_runtime", lambda preset: (object(), object()))

    started = []
    monkeypatch.setattr(
        service,
        "_start_thread",
        lambda task_id, target, *args: started.append((task_id, target, args)),
    )

    task = service.generate(
        "job-many",
        [str(index) for index in range(21)],
        "gpt-5-6-luna",
        "gpt-5-6-terra",
        0.6,
    )

    assert task["schema_version"] == 2
    assert task["status"] == "collecting_evidence"
    assert task["source_model_preset"] == "gpt-5-6-luna"
    assert task["optimizer_model"] == "gpt-5-6-terra"
    assert len(task["selected_cases"]) == 21
    assert len(started) == 1
    assert len(started[0][2][1]) == 21


def test_generate_rejects_cases_outside_selected_evaluation_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(service, "_optimizer_runtime", lambda preset: (object(), object()))

    with pytest.raises(ValueError, match="not in the evaluation run"):
        service.generate("job-1", ["1"], "deepseek-v4-flash", "gpt-5-6-sol", 0.6)


def test_optimizer_runtime_rejects_implicit_default_preset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    config = SimpleNamespace(model_presets={"gpt-5-6-sol": object()})
    monkeypatch.setattr("nanobot.evaluations.skill_evolution.load_config", lambda: config)
    monkeypatch.setattr("nanobot.evaluations.skill_evolution.resolve_config_env_vars", lambda value: value)

    with pytest.raises(ValueError, match="not a named model preset"):
        service._optimizer_runtime("default")


def test_candidate_validation_freezes_manifest_and_provider_contract(tmp_path: Path) -> None:
    service = _service(tmp_path)
    base = ROOT / "nanobot" / "skills" / "officecli"
    candidate = tmp_path / DERIVED_SKILL
    shutil.copytree(base, candidate, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    service._normalize_derived_metadata(candidate)

    valid = service._validate_candidate(base, candidate)
    assert valid == {"valid": True, "errors": []}
    skill_text = (candidate / "SKILL.md").read_text(encoding="utf-8")
    assert skill_text.startswith(f"---\nname: {DERIVED_SKILL}\n")

    manifest = candidate / "skill.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "permissions: {}\n", encoding="utf-8")
    invalid = service._validate_candidate(base, candidate)
    assert invalid["valid"] is False
    assert any("skill.yaml" in error for error in invalid["errors"])


def test_activity_is_monotonic_redacted_and_cursor_readable(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.store.write({"task_id": "task-1", "created_at": "x", "activity_cursor": 0})

    first = service._emit(
        "task-1",
        phase="analyzing",
        kind="model",
        status="running",
        label="Calling model",
        detail="Authorization: Bearer secret-value and sk-abcdefghijklmnop",
    )
    second = service._emit(
        "task-1",
        phase="analyzing",
        kind="validation",
        status="completed",
        label="Validated",
    )

    assert (first["seq"], second["seq"]) == (1, 2)
    assert "secret-value" not in first["detail"]
    assert "abcdefghijklmnop" not in first["detail"]
    payload = service.activities("task-1", after=1)
    assert [row["seq"] for row in payload["activities"]] == [2]
    assert payload["cursor"] == 2


def test_stale_running_snapshot_cannot_clear_cancel_request(tmp_path: Path) -> None:
    service = _service(tmp_path)
    original = service.store.write({
        "task_id": "task-1",
        "created_at": "x",
        "status": "analyzing",
        "cancel_requested": False,
    })
    cancelled = dict(original)
    cancelled["cancel_requested"] = True
    service.store.write(cancelled)

    service.store.write(original)

    assert service.get("task-1")["cancel_requested"] is True


def test_resource_comparison_uses_same_model_benchmark_high_scores(tmp_path: Path) -> None:
    service = _service(tmp_path)
    selected = [{
        "case_id": "low",
        "benchmark": "ocb",
        "skill": "officecli",
        "model_preset": "gpt",
        "usage": {"total_tokens": 300},
        "metrics": {"latency_ms": 900},
        "scores": {"mybot_score": 0.2},
    }]
    peers = [
        {**selected[0], "case_id": "high-1", "usage": {"total_tokens": 100}, "metrics": {"latency_ms": 300}, "scores": {"mybot_score": 0.9}},
        {**selected[0], "case_id": "high-2", "usage": {"total_tokens": 200}, "metrics": {"latency_ms": 500}, "scores": {"mybot_score": 0.8}},
        {**selected[0], "case_id": "other", "model_preset": "other", "usage": {"total_tokens": 1}, "scores": {"mybot_score": 1.0}},
    ]

    comparison = service._resource_comparisons(selected, selected + peers, 0.6)[
        "\0".join(("ocb", "officecli", "gpt", "low"))
    ]

    assert comparison["peer_count"] == 2
    assert comparison["tokens"]["high_score_median"] == 150
    assert comparison["tokens"]["ratio"] == 2


def test_restricted_tools_reject_escape_frozen_and_unapproved_evidence(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "SKILL.md").write_text("skill", encoding="utf-8")
    emitted = []
    write = WriteSkillFileTool(candidate, lambda **row: emitted.append(row), cancelled=lambda: False)
    read_evidence = ReadEvolutionEvidenceTool(
        candidate,
        lambda **row: emitted.append(row),
        cancelled=lambda: False,
        evidence={"ev-1": {"evidence_id": "ev-1"}},
        allowed_ids={"ev-1"},
    )

    with pytest.raises(ValueError, match="safe relative"):
        asyncio.run(write.execute(path="../outside.md", content="x"))
    with pytest.raises(ValueError, match="frozen"):
        asyncio.run(write.execute(path="skill.yaml", content="name: changed"))
    with pytest.raises(ValueError, match="not approved"):
        asyncio.run(read_evidence.execute(evidence_ids=["ev-2"]))


def test_non_skill_finding_cannot_start_evolution(tmp_path: Path) -> None:
    service = _service(tmp_path)
    analysis = {
        "analysis_id": "a1",
        "digest": "digest",
        "findings": [{
            "finding_id": "f1",
            "fix_owner": "runtime",
            "should_modify_skill": False,
        }],
    }
    service.store.write({
        "task_id": "task-1",
        "created_at": "x",
        "active_analysis_id": "a1",
        "analyses": [analysis],
        "revisions": [],
    })

    with pytest.raises(ValueError, match="owned by runtime"):
        service.start_evolution("task-1", ["f1"])


def test_editing_agent_reads_patches_rereads_and_validates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    task_id = "task-agent"
    analysis = {
        "analysis_id": "a1",
        "digest": "analysis-digest",
        "evidence_digest": "evidence-digest",
        "summary": "The Skill needs an explicit repair loop.",
    }
    finding = {
        "finding_id": "f1",
        "fix_owner": "skill",
        "should_modify_skill": True,
        "evidence_refs": ["ev-1"],
        "root_cause": "Validation was skipped",
    }
    service.store.write({
        "task_id": task_id,
        "created_at": "x",
        "status": "editing",
        "phase": "editing",
        "optimizer_model": "optimizer",
        "selected_cases": [],
        "evidence_digest": "evidence-digest",
        "analyses": [analysis],
        "revisions": [],
        "activity_cursor": 0,
    })
    evidence_path = service.store.task_root(task_id) / "evidence-evidence-digest.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text('{"ev-1":{"evidence_id":"ev-1"}}', encoding="utf-8")
    provider = SimpleNamespace(chat_with_retry=AsyncMock(side_effect=[
        LLMResponse(None, [ToolCallRequest("1", "list_skill_files", {})], "tool_calls"),
        LLMResponse(None, [ToolCallRequest("2", "read_skill_file", {"path": "SKILL.md"})], "tool_calls"),
        LLMResponse(None, [ToolCallRequest("bad", "apply_skill_patch", {"edits": [{
            "path": "SKILL.md",
            "old_text": "missing text",
            "new_text": "replacement",
        }]})], "tool_calls"),
        LLMResponse(None, [ToolCallRequest("3", "apply_skill_patch", {"edits": [{
            "path": "SKILL.md",
            "old_text": "# OfficeCLI\n",
            "new_text": "# OfficeCLI\n\nValidate and repair the artifact before delivery.\n",
        }]})], "tool_calls"),
        LLMResponse(None, [ToolCallRequest("4", "read_skill_file", {"path": "SKILL.md"})], "tool_calls"),
        LLMResponse(None, [ToolCallRequest("5", "validate_skill_candidate", {})], "tool_calls"),
        LLMResponse("Updated the validation workflow.", finish_reason="stop"),
    ]))
    preset = SimpleNamespace(
        model="fake-model",
        max_tokens=8192,
        temperature=0.1,
        reasoning_effort=None,
        context_window_tokens=65536,
    )
    config = SimpleNamespace(agents=SimpleNamespace(defaults=SimpleNamespace(
        max_tool_iterations=20,
        max_tool_result_chars=16000,
    )))
    monkeypatch.setattr(service, "_optimizer_runtime", lambda _preset: (preset, provider))
    monkeypatch.setattr("nanobot.evaluations.skill_evolution.load_config", lambda: config)
    monkeypatch.setattr(
        "nanobot.evaluations.skill_evolution.resolve_config_env_vars",
        lambda value: value,
    )
    captured_specs = []
    original_run = AgentRunner.run

    async def capture_run(runner, spec):
        captured_specs.append(spec)
        return await original_run(runner, spec)

    monkeypatch.setattr(AgentRunner, "run", capture_run)

    service._run_editing(task_id, "r1", analysis, [finding], None, None)

    task = service.get(task_id)
    assert task is not None
    revision = task["revisions"][0]
    assert task["status"] == "ready_for_review"
    assert revision["agent"]["stop_reason"] == "completed"
    assert revision["changed_paths"] == ["SKILL.md"]
    assert "Validate and repair" in revision["diff"]
    assert provider.chat_with_retry.await_count == 7
    assert captured_specs[0].total_token_budget is None
    assert captured_specs[0].max_iterations == 20
    assert captured_specs[0].max_tool_calls == 40
    assert captured_specs[0].llm_timeout_s is None
    assert captured_specs[0].workspace == service.store.revision_root(task_id, "r1") / "agent-workspace"
    assert not (captured_specs[0].workspace / "candidate").exists()


def test_editor_tool_budget_scales_with_selected_interventions() -> None:
    categories = [
        *[_script_category(f"c{index}", [str(index)]) for index in range(1, 10)],
        _workflow_category("c10", ["f10"], ["10"]),
        _workflow_category("c11", ["f11"], ["11"]),
    ]

    assert _skill_editor_tool_call_budget([]) == 40
    assert _skill_editor_tool_call_budget(categories) == 80


def test_editing_accepts_valid_candidate_when_last_patch_hits_iteration_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    task_id = "task-agent-max-iterations"
    analysis = {
        "analysis_id": "a1",
        "digest": "analysis-digest",
        "evidence_digest": "evidence-digest",
        "summary": "The Skill needs a final verification rule.",
    }
    finding = {
        "finding_id": "f1",
        "fix_owner": "skill",
        "should_modify_skill": True,
        "evidence_refs": ["ev-1"],
        "root_cause": "Validation was skipped",
    }
    service.store.write({
        "task_id": task_id,
        "created_at": "x",
        "status": "editing",
        "phase": "editing",
        "optimizer_model": "optimizer",
        "selected_cases": [],
        "evidence_digest": "evidence-digest",
        "analyses": [analysis],
        "revisions": [],
        "activity_cursor": 0,
    })
    evidence_path = service.store.task_root(task_id) / "evidence-evidence-digest.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text('{"ev-1":{"evidence_id":"ev-1"}}', encoding="utf-8")
    provider = SimpleNamespace(chat_with_retry=AsyncMock(return_value=LLMResponse(
        None,
        [ToolCallRequest("1", "apply_skill_patch", {"edits": [{
            "path": "SKILL.md",
            "old_text": "# OfficeCLI\n",
            "new_text": "# OfficeCLI\n\nVerify the final artifact before delivery.\n",
        }]})],
        "tool_calls",
    )))
    preset = SimpleNamespace(
        model="fake-model",
        max_tokens=8192,
        temperature=0.1,
        reasoning_effort=None,
        context_window_tokens=65536,
    )
    config = SimpleNamespace(agents=SimpleNamespace(defaults=SimpleNamespace(
        max_tool_iterations=1,
        max_tool_result_chars=16000,
    )))
    monkeypatch.setattr(service, "_optimizer_runtime", lambda _preset: (preset, provider))
    monkeypatch.setattr("nanobot.evaluations.skill_evolution.load_config", lambda: config)
    monkeypatch.setattr(
        "nanobot.evaluations.skill_evolution.resolve_config_env_vars",
        lambda value: value,
    )

    service._run_editing(task_id, "r1", analysis, [finding], None, None)

    task = service.get(task_id)
    assert task is not None
    revision = task["revisions"][0]
    assert task["status"] == "ready_for_review"
    assert revision["status"] == "ready_for_review"
    assert revision["agent"]["stop_reason"] == "max_iterations"
    assert revision["validation"]["valid"] is True
    assert revision["changed_paths"] == ["SKILL.md"]
    assert "accepted by controller validation" in revision["summary"]


def test_revise_validates_inherited_interventions_against_original_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    task_id = "task-agent-inherited-intervention"
    analysis = {
        "analysis_id": "a1",
        "digest": "analysis-digest",
        "evidence_digest": "evidence-digest",
        "summary": "Preserve existing intervention implementations during revision.",
    }
    finding = {
        "finding_id": "f1",
        "fix_owner": "skill",
        "should_modify_skill": True,
        "evidence_refs": ["ev-1"],
        "case_ids": ["1"],
        "root_cause": "A grounded claim was omitted.",
    }
    category = _workflow_category("c1", ["f1"], ["1"])
    service.store.write({
        "task_id": task_id,
        "created_at": "x",
        "status": "editing",
        "phase": "editing",
        "optimizer_model": "optimizer",
        "selected_cases": [],
        "evidence_digest": "evidence-digest",
        "analyses": [analysis],
        "revisions": [{
            "revision_id": "r1",
            "status": "ready_for_review",
            "validation": {"valid": True, "errors": []},
            "intervention_validation": {"valid": True, "errors": [], "probe_results": []},
            "test_results": [],
        }],
        "activity_cursor": 0,
    })
    evidence_path = service.store.task_root(task_id) / "evidence-evidence-digest.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text('{"ev-1":{"evidence_id":"ev-1"}}', encoding="utf-8")
    source_candidate = tmp_path / "source-candidate"
    shutil.copytree(ROOT / "nanobot" / "skills" / "officecli", source_candidate)
    service._normalize_derived_metadata(source_candidate)
    source_skill = source_candidate / "SKILL.md"
    source_skill.write_text(
        source_skill.read_text(encoding="utf-8") + "\nInherited parent-only rule.\n",
        encoding="utf-8",
    )
    preset = SimpleNamespace(
        model="fake-model",
        max_tokens=8192,
        temperature=0.1,
        reasoning_effort=None,
        context_window_tokens=65536,
    )
    config = SimpleNamespace(agents=SimpleNamespace(defaults=SimpleNamespace(
        max_tool_iterations=20,
        max_tool_result_chars=16000,
    )))
    monkeypatch.setattr(service, "_optimizer_runtime", lambda _preset: (preset, SimpleNamespace()))
    monkeypatch.setattr("nanobot.evaluations.skill_evolution.load_config", lambda: config)
    monkeypatch.setattr(
        "nanobot.evaluations.skill_evolution.resolve_config_env_vars",
        lambda value: value,
    )

    async def fake_run(_runner, _spec):
        candidate = service.store.revision_root(task_id, "r2") / "candidate" / DERIVED_SKILL
        skill_path = candidate / "SKILL.md"
        skill_path.write_text(
            skill_path.read_text(encoding="utf-8") + "\nCurrent revision refinement.\n",
            encoding="utf-8",
        )
        return SimpleNamespace(
            stop_reason="completed",
            error=None,
            final_content="Refined inherited intervention.",
            tools_used=[],
            usage={},
        )

    intervention_baselines: list[Path] = []

    def validate_interventions(baseline, candidate, categories, evidence):
        intervention_baselines.append(baseline)
        assert "Inherited parent-only rule." not in (baseline / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "Inherited parent-only rule." in (candidate / "SKILL.md").read_text(
            encoding="utf-8"
        )
        return {"valid": True, "errors": [], "probe_results": []}

    monkeypatch.setattr(AgentRunner, "run", fake_run)
    monkeypatch.setattr(
        service,
        "_validate_candidate",
        lambda *args, **kwargs: {"valid": True, "errors": []},
    )
    monkeypatch.setattr(service, "_validate_interventions", validate_interventions)

    service._run_editing(
        task_id,
        "r2",
        analysis,
        [finding],
        [category],
        "r1",
        source_candidate,
    )

    task = service.get(task_id)
    assert task is not None
    assert task["status"] == "ready_for_review"
    assert len(intervention_baselines) == 1
    assert all("intervention-baseline" in path.parts for path in intervention_baselines)


def test_freeze_local_reference_assets_uses_manifest_without_recollecting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    source = tmp_path / "source.xlsx"
    source.write_bytes(b"frozen workbook")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(
        "nanobot.evaluations.skill_evolution._manifest_rows",
        lambda profile, benchmark: {
            "1": {
                "input": {
                    "reference_paths": [str(source)],
                    "reference_sha256": [source_hash],
                }
            }
        },
    )
    evidence = {
        "ev-1": {
            "evidence_id": "ev-1",
            "case_id": "1",
            "metadata": {"benchmark": "ocb"},
        }
    }

    result = service._freeze_local_reference_assets(
        "task-freeze",
        {"source_profile": "office-release"},
        {"evidence_digest": "digest-123"},
        evidence,
        {"ev-1"},
    )

    frozen = Path(result["ev-1"]["reference_paths"][0])
    assert frozen.read_bytes() == source.read_bytes()
    assert "frozen-assets" in frozen.parts
    sidecar = service.store.task_root("task-freeze") / "frozen-assets-digest-123.json"
    assert json.loads(sidecar.read_text(encoding="utf-8"))["cases"]["1"][
        "reference_sha256"
    ] == [source_hash]


def test_incomplete_revision_preserves_category_contract_and_scope(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task_id = "task-incomplete-category"
    service.store.write({"task_id": task_id, "created_at": "x", "revisions": []})
    candidate = service.store.revision_root(task_id, "r1") / "candidate" / DERIVED_SKILL
    candidate.mkdir(parents=True)
    category = _script_category("c1", ["1"])

    service._record_incomplete_revision(
        task_id,
        "r1",
        {"analysis_id": "a1", "digest": "analysis-digest", "summary": "summary"},
        [{"finding_id": "f1", "case_ids": ["1"]}],
        [category],
        None,
        candidate,
        "failed",
        "validation failed",
    )

    revision = service.get(task_id)["revisions"][0]
    assert revision["category_ids"] == ["c1"]
    assert revision["target_case_ids"] == ["1"]
    assert revision["interventions"] == [category["intervention"]]


def test_script_required_category_rejects_prose_only_candidate(tmp_path: Path) -> None:
    service = _service(tmp_path)
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    baseline.mkdir()
    (candidate / "references").mkdir(parents=True)
    (baseline / "SKILL.md").write_text("# Base\n", encoding="utf-8")
    (candidate / "SKILL.md").write_text(
        "# Base\nRun scripts/inspect_formulas.py for formula checks.\n",
        encoding="utf-8",
    )
    (candidate / "references" / "evolution-interventions.json").write_text(
        json.dumps({
            "schema_version": 1,
            "interventions": [{
                "category_id": "c1",
                "repair_mode": "script_required",
                "changed_paths": ["SKILL.md"],
                "entrypoint": "scripts/inspect_formulas.py",
                "skill_marker": "Run scripts/inspect_formulas.py",
                "probe": {
                    "args": ["{asset}"],
                    "required_json_fields": ["requested_cells", "formulas"],
                    "timeout_seconds": 30,
                },
            }],
        }),
        encoding="utf-8",
    )

    result = service._validate_interventions(
        baseline,
        candidate,
        [_script_category("c1", ["1"])],
        {},
    )

    assert result["valid"] is False
    assert any("must add or modify scripts/inspect_formulas.py" in error for error in result["errors"])


def test_workflow_required_accepts_a_direct_skill_activation_rule(tmp_path: Path) -> None:
    service = _service(tmp_path)
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    baseline.mkdir()
    (candidate / "references").mkdir(parents=True)
    marker = "When exact claims are requested, build a claim-to-source checklist."
    (baseline / "SKILL.md").write_text("# Base\n", encoding="utf-8")
    (candidate / "SKILL.md").write_text(f"# Base\n{marker}\n", encoding="utf-8")
    (candidate / "references" / "evolution-interventions.json").write_text(
        json.dumps({
            "schema_version": 1,
            "interventions": [{
                "category_id": "c1",
                "repair_mode": "workflow_required",
                "changed_paths": ["SKILL.md"],
                "entrypoint": "SKILL.md",
                "skill_marker": marker,
                "probe": None,
            }],
        }),
        encoding="utf-8",
    )

    result = service._validate_interventions(
        baseline,
        candidate,
        [_workflow_category("c1", ["f1"], ["1"])],
        {},
    )

    assert result == {"valid": True, "errors": [], "probe_results": []}


def test_intervention_probe_runs_read_only_and_validates_formula_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    candidate = tmp_path / "candidate"
    scripts = candidate / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "inspect_formulas.py").write_text(
        "import json\n"
        "print(json.dumps({'requested_cells': ['A1'], 'formulas': {'A1': '=1+1'}}))\n",
        encoding="utf-8",
    )
    asset = tmp_path / "input.xlsx"
    asset.write_bytes(b"fixture")
    modes = []

    def prepare(_launcher, **kwargs):
        modes.append(kwargs["mode"])
        return SimpleNamespace(argv=kwargs["argv"], cwd=str(kwargs["cwd"]), env=kwargs["env"])

    monkeypatch.setattr(
        "nanobot.evaluations.skill_evolution.SandboxLauncher.prepare_argv",
        prepare,
    )

    results, errors = service._run_intervention_probes(
        candidate,
        "c1",
        "scripts/inspect_formulas.py",
        InterventionProbe(
            args=["{asset}"],
            required_json_fields=["requested_cells", "formulas"],
        ),
        [{"case_id": "1", "reference_paths": [str(asset)]}],
        mandatory_fields=["requested_cells", "formulas"],
    )

    assert errors == []
    assert results[0]["valid"] is True
    assert modes == ["read_only"]


def test_intervention_probe_reports_missing_fields_and_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    candidate = tmp_path / "candidate"
    scripts = candidate / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "probe.py").write_text("print('{}')\n", encoding="utf-8")
    asset = tmp_path / "input.xlsx"
    asset.write_bytes(b"fixture")

    def prepare(_launcher, **kwargs):
        return SimpleNamespace(argv=kwargs["argv"], cwd=str(kwargs["cwd"]), env=kwargs["env"])

    monkeypatch.setattr(
        "nanobot.evaluations.skill_evolution.SandboxLauncher.prepare_argv",
        prepare,
    )
    probe = InterventionProbe(args=["{asset}"], required_json_fields=["items"])

    results, errors = service._run_intervention_probes(
        candidate,
        "c1",
        "scripts/probe.py",
        probe,
        [{"case_id": "1", "reference_paths": [str(asset)]}],
    )
    assert results[0]["missing_fields"] == ["items"]
    assert any("missing JSON fields" in error for error in errors)

    monkeypatch.setattr(
        "nanobot.evaluations.skill_evolution.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd=args[0], timeout=1)
        ),
    )
    results, errors = service._run_intervention_probes(
        candidate,
        "c1",
        "scripts/probe.py",
        probe,
        [{"case_id": "1", "reference_paths": [str(asset)]}],
    )
    assert results == []
    assert any("probe execution failed" in error for error in errors)


def test_recommendation_uses_overall_mean_and_requires_every_target_score(tmp_path: Path) -> None:
    service = _service(tmp_path)
    results = [
        {"status": "completed", "delta": 0.2, "category_ids": ["c1"]},
        {"status": "completed", "delta": -0.1, "category_ids": ["c1", "c2"]},
    ]

    recommendation = service._test_recommendation(results)

    assert recommendation["recommended"] is True
    assert recommendation["mean_delta"] == pytest.approx(0.05)
    assert recommendation["improved_cases"] == 1
    assert recommendation["regressed_cases"] == 1
    assert recommendation["category_summaries"][0]["category_id"] == "c1"

    missing_score = service._test_recommendation([
        *results,
        {"status": "completed", "delta": None, "category_ids": ["c2"]},
    ])
    failed_case = service._test_recommendation([
        {"status": "failed", "delta": 0.5, "category_ids": ["c1"]},
    ])
    assert missing_score["recommended"] is False
    assert missing_score["all_target_cases_scored"] is False
    assert failed_case["recommended"] is False


def test_manual_revise_feedback_includes_output_judge_trace_and_intervention_state() -> None:
    rows = SkillEvolutionService._revision_feedback_rows({
        "revision_id": "r1",
        "test_results": [
            {
                "case_id": "1",
                "status": "completed",
                "delta": -0.1,
                "candidate_output": "answer",
                "judge_reasoning": ["missing a field"],
                "tool_sequence": ["read_skill_file"],
                "tool_errors": ["probe not called"],
                "stop_reason": "completed",
                "intervention_feedback": [{"category_id": "c1", "entrypoint_observed": False}],
            },
            {"case_id": "2", "status": "completed", "delta": 0.2},
        ],
    })

    assert len(rows) == 1
    assert rows[0]["candidate_output"] == "answer"
    assert rows[0]["judge_reasoning"] == ["missing a field"]
    assert rows[0]["tool_errors"] == ["probe not called"]
    assert rows[0]["intervention_feedback"][0]["entrypoint_observed"] is False


def test_reanalyze_consumes_cancel_request_from_previous_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    task_id = "task-reanalyze-after-cancel"
    service.store.write({
        "task_id": task_id,
        "created_at": "x",
        "status": "cancelled",
        "phase": "testing",
        "cancel_requested": True,
        "source_run_id": "job-1",
        "threshold": 0.6,
        "selected_cases": [{
            "case_key": "\0".join(("ocb", "officecli", "gpt", "1")),
            "case_id": "1",
        }],
        "active_analysis_id": "a1",
        "analyses": [{"analysis_id": "a1"}],
        "revisions": [],
    })
    monkeypatch.setattr(service, "_start_thread", lambda *args: None)

    task = service.reanalyze(task_id)

    assert task["status"] == "collecting_evidence"
    assert task["cancel_requested"] is False
    assert task["error"] is None


def test_start_evolution_consumes_cancel_request_from_previous_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    task_id = "task-edit-after-cancel"
    analysis = {
        "analysis_id": "a1",
        "digest": "analysis-digest",
        "findings": [{
            "finding_id": "f1",
            "fix_owner": "skill",
        }],
        "categories": [_workflow_category("c1", ["f1"], ["1"])],
    }
    service.store.write({
        "task_id": task_id,
        "created_at": "x",
        "status": "cancelled",
        "phase": "editing",
        "cancel_requested": True,
        "active_analysis_id": "a1",
        "analyses": [analysis],
        "revisions": [],
    })
    monkeypatch.setattr(service, "_start_thread", lambda *args: None)

    task = service.start_evolution(
        task_id,
        category_ids=["c1"],
        analysis_id="a1",
        analysis_digest="analysis-digest",
    )

    assert task["status"] == "editing"
    assert task["cancel_requested"] is False
    assert task["error"] is None


def test_start_test_reuses_origin_intervention_baseline_and_frozen_asset_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    task_id = "task-test-inherited-intervention"
    revision_id = "r1"
    category = _script_category("c1", ["1"])
    analysis = {
        "analysis_id": "a1",
        "digest": "analysis-digest",
        "evidence_digest": "evidence-digest",
        "findings": [{
            "finding_id": "f1",
            "evidence_refs": ["ev-1"],
            "case_ids": ["1"],
        }],
        "categories": [category],
    }
    candidate = service.store.revision_root(task_id, revision_id) / "candidate" / DERIVED_SKILL
    shutil.copytree(ROOT / "nanobot" / "skills" / "officecli", candidate)
    service._normalize_derived_metadata(candidate)
    candidate_skill = candidate / "SKILL.md"
    candidate_skill.write_text(
        candidate_skill.read_text(encoding="utf-8") + "\nInherited candidate rule.\n",
        encoding="utf-8",
    )
    service.store.write({
        "task_id": task_id,
        "created_at": "x",
        "status": "ready_for_review",
        "phase": "validating",
        "cancel_requested": False,
        "active_analysis_id": "a1",
        "active_revision_id": revision_id,
        "analyses": [analysis],
        "selected_cases": [{"case_id": "1"}],
        "revisions": [{
            "revision_id": revision_id,
            "analysis_id": "a1",
            "category_ids": ["c1"],
            "finding_ids": ["f1"],
            "target_case_ids": ["1"],
            "status": "ready_for_review",
            "test_results": [],
        }],
    })
    evidence_path = service.store.task_root(task_id) / "evidence-evidence-digest.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps({"ev-1": {"evidence_id": "ev-1", "case_id": "1"}}),
        encoding="utf-8",
    )
    asset = service.store.task_root(task_id) / "frozen-assets" / "evidence-digest" / "1.xlsx"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"frozen workbook")
    asset_hash = hashlib.sha256(asset.read_bytes()).hexdigest()
    (service.store.task_root(task_id) / "frozen-assets-evidence-digest.json").write_text(
        json.dumps({
            "schema_version": 1,
            "evidence_digest": "evidence-digest",
            "cases": {
                "1": {
                    "evidence_id": "ev-1",
                    "reference_paths": [str(asset)],
                    "reference_sha256": [asset_hash],
                }
            },
        }),
        encoding="utf-8",
    )
    observed: dict[str, Any] = {}

    def validate_interventions(baseline, candidate_path, categories, evidence):
        observed["baseline"] = baseline
        observed["candidate"] = candidate_path
        observed["asset"] = evidence["ev-1"]["reference_paths"][0]
        return {"valid": True, "errors": [], "probe_results": [{"valid": True}]}

    monkeypatch.setattr(
        service,
        "_validate_candidate",
        lambda *args, **kwargs: {"valid": True, "errors": []},
    )
    monkeypatch.setattr(service, "_validate_interventions", validate_interventions)
    monkeypatch.setattr(service, "_test_scope", lambda task, revision: [{"case_id": "1"}])
    monkeypatch.setattr(service, "_start_thread", lambda *args: None)

    task = service.start_test(task_id, revision_id)

    assert task["status"] == "testing"
    assert "intervention-baseline" in observed["baseline"].parts
    assert "Inherited candidate rule." not in (observed["baseline"] / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert observed["candidate"] == candidate
    assert observed["asset"] == str(asset.resolve())


def test_revise_can_continue_retained_failed_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    task_id = "task-revise-failed"
    candidate = service.store.revision_root(task_id, "r1") / "candidate" / DERIVED_SKILL
    candidate.mkdir(parents=True)
    analysis = {
        "analysis_id": "a1",
        "digest": "analysis-digest",
        "findings": [{"finding_id": "f1", "fix_owner": "skill", "case_ids": ["1"]}],
        "categories": [_workflow_category("c1", ["f1"], ["1"])],
    }
    service.store.write({
        "task_id": task_id,
        "created_at": "x",
        "status": "failed",
        "active_analysis_id": "a1",
        "active_revision_id": "r1",
        "analyses": [analysis],
        "revisions": [{
            "revision_id": "r1",
            "analysis_id": "a1",
            "analysis_digest": "analysis-digest",
            "finding_ids": ["f1"],
            "category_ids": ["c1"],
            "status": "failed",
            "candidate_retained_for_audit": True,
        }],
    })
    started: list[tuple] = []
    monkeypatch.setattr(service, "_start_thread", lambda *args: started.append(args))

    task = service.revise(task_id)

    assert task["status"] == "editing"
    assert task["active_revision_id"] == "r2"
    assert started[0][-1] == candidate


def test_apply_installs_derived_skill_without_changing_builtin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    base = ROOT / "nanobot" / "skills" / "officecli"
    base_digest = _tree_digest(base)
    task_id = "task-1"
    revision_id = "r1"
    candidate = service.store.revision_root(task_id, revision_id) / "candidate" / DERIVED_SKILL
    shutil.copytree(base, candidate, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    service._normalize_derived_metadata(candidate)
    task = {
        "task_id": task_id,
        "status": "ready_for_review",
        "active_revision_id": revision_id,
        "revisions": [{
            "revision_id": revision_id,
            "status": "tested",
            "candidate_digest": _tree_digest(candidate),
            "recommendation": {"recommended": True},
        }],
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    service.store.write(task)
    config = SimpleNamespace(
        workspace_path=tmp_path / "workspace",
        agents=SimpleNamespace(defaults=SimpleNamespace(disabled_skills=[])),
    )
    saved = []
    monkeypatch.setattr("nanobot.evaluations.skill_evolution.load_config", lambda: config)
    monkeypatch.setattr("nanobot.evaluations.skill_evolution.save_config", lambda value: saved.append(value))

    applied = service.apply(task_id, revision_id)

    assert (config.workspace_path / "skills" / DERIVED_SKILL / "SKILL.md").is_file()
    assert config.agents.defaults.disabled_skills == ["officecli"]
    assert saved == [config]
    assert applied["status"] == "applied"
    assert _tree_digest(base) == base_digest


def test_apply_rejects_unverified_revision(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.store.write({
        "task_id": "task-1",
        "created_at": "x",
        "revisions": [{"revision_id": "r1", "status": "ready_for_review"}],
    })

    with pytest.raises(ValueError, match="tested and recommended"):
        service.apply("task-1", "r1")


def test_test_scope_is_exact_deduplicated_union_of_selected_categories(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    task = {
        "source_run_id": "job-1",
        "source_model_preset": "gpt",
        "threshold": 0.6,
        "active_analysis_id": "a1",
        "analyses": [{
            "analysis_id": "a1",
            "categories": [
                _workflow_category("c1", ["f1"], ["1", "2"]),
                _workflow_category("c2", ["f2"], ["2"]),
            ],
            "findings": [],
        }],
        "selected_cases": [
            {
                "case_key": "\0".join(("ocb", "officecli", "gpt", case_id)),
                "case_id": case_id,
                "benchmark": "ocb",
                "skill": "officecli",
                "model_preset": "gpt",
                "baseline_score": score,
            }
            for case_id, score in (("1", 0.25), ("2", 0.2))
        ],
    }
    revision = {"analysis_id": "a1", "category_ids": ["c1", "c2"]}

    scope = service._test_scope(task, revision)

    assert [(row["case_id"], row["scope"]) for row in scope] == [
        ("1", "target"),
        ("2", "target"),
    ]
    assert scope[0]["category_ids"] == ["c1"]
    assert scope[1]["category_ids"] == ["c1", "c2"]


def test_cancelled_test_persists_failed_revision_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    task_id = "task-cancelled-test"
    revision_id = "r1"
    candidate = service.store.revision_root(task_id, revision_id) / "candidate" / DERIVED_SKILL
    candidate.mkdir(parents=True)
    service.store.write({
        "task_id": task_id,
        "created_at": "x",
        "status": "testing",
        "phase": "testing",
        "cancel_requested": True,
        "source_profile": "office-release",
        "revisions": [{
            "revision_id": revision_id,
            "status": "testing",
            "test_scope": [{"case_id": "1"}],
            "test_results": [],
        }],
    })

    monkeypatch.setattr(service, "_check_cancelled", lambda _task_id: (_ for _ in ()).throw(
        asyncio.CancelledError()
    ))

    service._run_tests(task_id, revision_id)

    task = service.get(task_id)
    assert task is not None
    assert task["status"] == "cancelled"
    assert task["revisions"][0]["status"] == "test_failed"
    assert task["revisions"][0]["test_error"] == "Cancelled by user"


def test_start_test_blocks_legacy_pure_text_revision_without_intervention_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    task_id = "task-legacy-cancelled-test"
    revision_id = "r1"
    candidate = service.store.revision_root(task_id, revision_id) / "candidate" / DERIVED_SKILL
    candidate.mkdir(parents=True)
    service.store.write({
        "task_id": task_id,
        "created_at": "x",
        "status": "cancelled",
        "phase": "testing",
        "cancel_requested": True,
        "source_run_id": "job-1",
        "revisions": [{
            "revision_id": revision_id,
            "status": "testing",
            "test_error": "Cancelled by user",
            "test_results": [{"case_id": "1", "status": "failed"}],
        }],
    })
    monkeypatch.setattr(service, "_validate_candidate", lambda *args, **kwargs: {
        "valid": True,
        "errors": [],
    })

    with pytest.raises(ValueError, match="no reason-category intervention contract"):
        service.start_test(task_id, revision_id)

    task = service.get(task_id)
    assert task is not None
    revision = task["revisions"][0]
    assert task["status"] == "cancelled"
    assert revision["status"] == "test_failed"
    assert revision["intervention_validation"]["valid"] is False


def test_switch_back_enables_base_and_disables_derived(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    service.store.write({"task_id": "task-1", "created_at": "x", "revisions": []})
    config = SimpleNamespace(
        agents=SimpleNamespace(defaults=SimpleNamespace(disabled_skills=["officecli"])),
    )
    monkeypatch.setattr("nanobot.evaluations.skill_evolution.load_config", lambda: config)
    monkeypatch.setattr("nanobot.evaluations.skill_evolution.save_config", lambda value: None)

    task = service.switch_back("task-1")

    assert config.agents.defaults.disabled_skills == [DERIVED_SKILL]
    assert task["status"] == "switched_back"
