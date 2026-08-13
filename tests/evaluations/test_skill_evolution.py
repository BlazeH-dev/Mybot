from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.runner import AgentRunner
from nanobot.evaluations.catalog import ROOT
from nanobot.evaluations.skill_evolution import (
    DERIVED_SKILL,
    SkillEvolutionService,
    SkillEvolutionStore,
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
    assert provider.chat_with_retry.await_count == 6
    assert captured_specs[0].total_token_budget is None
    assert captured_specs[0].max_tool_calls is None
    assert captured_specs[0].llm_timeout_s is None


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


def test_test_scope_adds_deterministic_same_model_high_score_regression_cases(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    task = {
        "source_run_id": "job-1",
        "source_model_preset": "gpt",
        "threshold": 0.6,
        "selected_cases": [{
            "case_key": "\0".join(("ocb", "officecli", "gpt", "1")),
            "case_id": "1",
            "benchmark": "ocb",
            "skill": "officecli",
            "model_preset": "gpt",
            "baseline_score": 0.25,
        }],
    }

    scope = service._test_scope(task)

    assert [(row["case_id"], row["scope"]) for row in scope] == [
        ("1", "selected"),
        ("2", "regression"),
    ]


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
