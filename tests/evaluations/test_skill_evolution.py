from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.evaluations.catalog import ROOT
from nanobot.evaluations.skill_evolution import (
    DERIVED_SKILL,
    SkillEvolutionService,
    SkillEvolutionStore,
    _tree_digest,
)


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
            "status": "ready_for_review",
            "candidate_digest": _tree_digest(candidate),
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
