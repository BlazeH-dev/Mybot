from __future__ import annotations

import json
import stat
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.cli.benchmark import (
    _load_case_result,
    _remote_variant_state,
    _resume_run_name,
    _run_agent_item,
    _write_case_result,
)
from nanobot.evaluations.catalog import (
    TERMINAL_JOB_STATUSES,
    EvaluationCatalog,
    EvaluationRequest,
    PreflightResult,
)
from nanobot.evaluations.jobs import EvaluationJobService, EvaluationJobStore
from nanobot.evaluations.results import LangfuseEvaluationReader, _trace_metrics
from nanobot.evaluations.worker import _consume_progress


def test_office_suite_manifest_exposes_extension_contract() -> None:
    suite = EvaluationCatalog().payload([])["suites"][0]

    assert suite["id"] == "office"
    assert suite["version"] == "1.0.0"
    assert {item["id"] for item in suite["benchmarks"]} == {
        "ocb",
        "officebench",
        "presentbench",
    }
    assert {item["id"] for item in suite["skills"]} == {"officecli", "office-python"}
    assert suite["benchmark_samples"] == {
        "ocb": [255, 509, 1018],
        "officebench": [24, 47, 93],
        "presentbench": [60, 119, 238],
    }
    assert suite["extension"]["manifest"].endswith("manifest.yaml")
    assert suite["extension"]["adapter"].endswith("adapter.py")


def test_release_samples_apply_to_estimate_and_cli_command() -> None:
    catalog = EvaluationCatalog()
    request = EvaluationRequest.from_payload({
        "profile": "office-release",
        "benchmark_samples": {"ocb": 255, "officebench": 24, "presentbench": 60},
    })

    estimate = catalog.adapter("office").estimate(request)  # type: ignore[attr-defined]
    command = catalog.command(request)

    assert estimate["case_counts"] == {"ocb": 255, "officebench": 24, "presentbench": 60}
    assert estimate["skill_runs"] == 678
    assert estimate["judge_runs"] == 630
    assert command[command.index("--ocb-sample") + 1] == "255"
    assert command[command.index("--officebench-sample") + 1] == "24"
    assert command[command.index("--presentbench-sample") + 1] == "60"


def test_legacy_presentbench_sample_is_merged_with_new_defaults() -> None:
    request = EvaluationRequest.from_payload({"presentbench_sample": 60})

    assert request.benchmark_samples == {
        "ocb": 1018,
        "officebench": 93,
        "presentbench": 60,
    }
    assert EvaluationRequest(presentbench_sample=119).benchmark_samples["presentbench"] == 119


def test_ci_preflight_is_offline_and_estimate_is_zero() -> None:
    result = EvaluationCatalog().preflight(EvaluationRequest(profile="ci"))

    assert result.ready is True
    assert result.checks["offline"] is True
    assert result.estimate["estimated_tokens"]["total"] == 0


class _FakeCatalog:
    def preflight(self, request: EvaluationRequest) -> PreflightResult:
        return PreflightResult(
            ready=True,
            estimate={"skill_runs": 2, "estimated_tokens": {"total": 10}},
        )

    def command(self, request: EvaluationRequest) -> list[str]:
        return ["benchmark", request.action, "--profile", request.profile]


def test_job_store_queues_and_cancel_keeps_terminal_state(tmp_path: Path) -> None:
    store = EvaluationJobStore(tmp_path / "jobs")
    service = EvaluationJobService(catalog=_FakeCatalog(), store=store)
    service._start_next = lambda: None

    first = service.submit(EvaluationRequest(profile="ci"))
    second = service.submit(EvaluationRequest(profile="ci"))

    assert first["status"] == "queued"
    assert second["status"] == "queued"
    cancelled = service.cancel(first["job_id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_requested"] is True
    assert service.cancel(first["job_id"])["status"] == "cancelled"


def test_awaiting_review_releases_single_worker_lock(tmp_path: Path) -> None:
    store = EvaluationJobStore(tmp_path / "jobs")
    store.write({
        "job_id": "review-job",
        "status": "awaiting_review",
        "phase": "awaiting_review",
        "created_at": "2026-01-01T00:00:00+00:00",
    })
    service = EvaluationJobService(catalog=_FakeCatalog(), store=store)

    assert service._active() is False
    assert "awaiting_review" in TERMINAL_JOB_STATUSES


def test_langfuse_run_projection_is_cached_between_ui_polls() -> None:
    reader = LangfuseEvaluationReader(cache_ttl_seconds=60)
    calls: list[int] = []

    def fake_list_runs(*, limit: int) -> dict[str, object]:
        calls.append(limit)
        return {"available": True, "error": None, "runs": [{"dataset_run_id": "run-1"}]}

    reader._list_runs_uncached = fake_list_runs  # type: ignore[method-assign]
    reader._refresh_cache(20)

    first = reader.list_runs(limit=20)
    first["runs"].clear()
    second = reader.list_runs(limit=20)

    assert calls == [20]
    assert second["runs"] == [{"dataset_run_id": "run-1"}]


def test_langfuse_trace_metrics_aggregate_generation_usage_and_timing() -> None:
    pages = iter([
        SimpleNamespace(
            data=[
                SimpleNamespace(
                    usage_details={
                        "input": 100,
                        "output": 20,
                        "total": 120,
                        "cache_read_input_tokens": 30,
                    },
                    latency=1.25,
                    time_to_first_token=0.4,
                ),
            ],
            meta=SimpleNamespace(cursor="next"),
        ),
        SimpleNamespace(
            data=[
                SimpleNamespace(
                    usage_details={"input_tokens": 50, "output_tokens": 10},
                    latency=0.75,
                    time_to_first_token=0.2,
                ),
            ],
            meta=SimpleNamespace(cursor=None),
        ),
    ])
    runtime = SimpleNamespace(
        client=SimpleNamespace(
            api=SimpleNamespace(
                observations=SimpleNamespace(
                    get_many=lambda **_kwargs: next(pages),
                ),
            ),
        ),
    )

    assert _trace_metrics(runtime, "trace-1") == {
        "usage": {
            "input_tokens": 150,
            "output_tokens": 30,
            "total_tokens": 180,
            "cached_input_tokens": 30,
            "cache_creation_input_tokens": 0,
        },
        "metrics": {
            "generation_count": 2,
            "latency_seconds": 2.0,
            "ttft_seconds": 0.6,
        },
    }


def test_resume_requeues_same_job_and_preserves_completed_cases(tmp_path: Path) -> None:
    store = EvaluationJobStore(tmp_path / "jobs")
    service = EvaluationJobService(catalog=_FakeCatalog(), store=store)
    service._start_next = lambda: None
    submitted = service.submit(EvaluationRequest(profile="office-smoke"))
    original_started_at = "2026-07-29T01:00:00+00:00"
    store.update(
        submitted["job_id"],
        status="interrupted",
        phase="interrupted",
        started_at=original_started_at,
        completed_cases=1,
        remaining_cases=1,
        cases=[{"benchmark": "ocb", "skill": "officecli", "case_id": "602", "status": "completed"}],
        dataset_run_ids=["run-1"],
        langfuse_links=["https://langfuse.test/run-1"],
    )

    resumed = service.resume(submitted["job_id"])

    assert resumed["job_id"] == submitted["job_id"]
    assert resumed["status"] == "queued"
    assert resumed["started_at"] == original_started_at
    assert resumed["completed_cases"] == 1
    assert resumed["remaining_cases"] == 1
    assert resumed["cases"][0]["case_id"] == "602"
    assert resumed["dataset_run_ids"] == ["run-1"]
    assert resumed["resume_count"] == 1
    assert resumed["resume_history"][0]["from_status"] == "interrupted"


def test_resume_rejects_non_terminal_job(tmp_path: Path) -> None:
    store = EvaluationJobStore(tmp_path / "jobs")
    service = EvaluationJobService(catalog=_FakeCatalog(), store=store)
    service._start_next = lambda: None
    submitted = service.submit(EvaluationRequest(profile="office-smoke"))

    with pytest.raises(ValueError, match="cannot be resumed"):
        service.resume(submitted["job_id"])


def test_progress_checkpoint_is_idempotent_from_persisted_offset(tmp_path: Path) -> None:
    progress = tmp_path / "job.progress.jsonl"
    events = [
        {"event": "run_started", "total_cases": 2},
        {"event": "case_completed", "benchmark": "ocb", "skill": "officecli", "case_id": "602", "status": "completed"},
        {"event": "case_reconciled", "benchmark": "ocb", "skill": "officecli", "case_id": "602", "status": "completed", "source": "langfuse"},
    ]
    progress.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    job: dict[str, object] = {"total_cases": 2, "cases": []}

    offset = _consume_progress(progress, 0, job)
    same_offset = _consume_progress(progress, offset, job)

    assert same_offset == offset
    assert job["completed_cases"] == 1
    assert job["remaining_cases"] == 1
    assert job["resumed_cases"] == 1
    assert len(job["cases"]) == 1


def test_remote_resume_state_uses_exact_run_and_latest_terminal_item() -> None:
    first = SimpleNamespace(
        experiment_item_id="dataset-item-1",
        end_time=datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc),
        level="ERROR",
        trace_id="trace-failed",
    )
    latest = SimpleNamespace(
        experiment_item_id="dataset-item-1",
        end_time=datetime(2026, 7, 29, 1, 5, tzinfo=timezone.utc),
        level="DEFAULT",
        trace_id="trace-completed",
    )
    calls: list[tuple[str, str]] = []

    def get_dataset_run(*, dataset_name: str, run_name: str):
        calls.append((dataset_name, run_name))
        return SimpleNamespace(id="run-1", dataset_id="dataset-1")

    client = SimpleNamespace(
        get_dataset_run=get_dataset_run,
        _get_project_id=lambda: "project-1",
        api=SimpleNamespace(experiments=SimpleNamespace(
            list=lambda **_kwargs: SimpleNamespace(data=[SimpleNamespace(start_time=first.end_time)]),
            list_items=lambda **_kwargs: SimpleNamespace(
                data=[first, latest],
                meta=SimpleNamespace(cursor=None),
            ),
        )),
    )
    runtime = SimpleNamespace(client=client, base_url="https://langfuse.test")

    state = _remote_variant_state(
        runtime,
        dataset_name="office-smoke-ocb",
        run_name="stable-run",
    )

    assert calls == [("office-smoke-ocb", "stable-run")]
    assert state["run_id"] == "run-1"
    assert state["items"] == {
        "dataset-item-1": {
            "status": "completed",
            "trace_id": "trace-completed",
            "trace_url": "https://langfuse.test/project/project-1/traces/trace-completed",
        },
    }


def test_resume_run_name_is_stable_for_job() -> None:
    expected = "mybot-office-smoke-ocb-officecli-job-job_123"

    assert _resume_run_name("office-smoke", "ocb", "officecli", "job_123") == expected
    assert _resume_run_name("office-smoke", "ocb", "officecli", "job_123") == expected
    assert _resume_run_name("office-smoke", "ocb", "officecli", None) is None


@pytest.mark.asyncio
async def test_case_checkpoint_reuses_output_without_calling_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = {
        "input": {"prompt": "Create the requested document"},
        "metadata": {"case_id": "case-1"},
    }
    run_root = tmp_path / "run"
    workspace = run_root / "workspaces" / "case-1"
    workspace.mkdir(parents=True)
    _write_case_result(
        run_root=run_root,
        benchmark="ocb",
        skill="officecli",
        case_id="case-1",
        model_preset="gpt-5-6-luna",
        source=source,
        workspace=workspace,
        content="cached model response",
        tools_used=["shell"],
    )

    def fail_from_config(*_args, **_kwargs):
        raise AssertionError("model runtime must not be created for a valid checkpoint")

    monkeypatch.setattr("nanobot.nanobot.Nanobot.from_config", fail_from_config)

    result = await _run_agent_item(
        source=source,
        benchmark="ocb",
        skill="officecli",
        model_preset="gpt-5-6-luna",
        run_root=run_root,
    )

    checkpoint_path = next((run_root / "case-results").rglob("*.json"))
    assert result["content"] == "cached model response"
    assert result["checkpoint_reused"] is True
    assert stat.S_IMODE(checkpoint_path.stat().st_mode) == 0o600
    assert _load_case_result(
        run_root=run_root,
        benchmark="ocb",
        skill="officecli",
        case_id="case-1",
        model_preset="gpt-5-6-luna",
        source={**source, "input": {"prompt": "Changed prompt"}},
    ) is None
