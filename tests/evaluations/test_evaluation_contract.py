from __future__ import annotations

import json
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import nanobot.evaluations.catalog as evaluation_catalog
from nanobot.cli.benchmark import (
    BenchmarkError,
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
from nanobot.evaluations.failures import classify_evaluation_failure
from nanobot.evaluations.jobs import EvaluationJobService, EvaluationJobStore
from nanobot.evaluations.results import (
    LangfuseEvaluationReader,
    _aggregate_case_scores,
    _latest_experiment_items,
    _release_case_ids,
    _score_name,
    _score_value,
    _trace_metrics,
    _trace_scores,
)
from nanobot.evaluations.worker import _consume_progress


def test_office_suite_manifest_exposes_extension_contract() -> None:
    suite = EvaluationCatalog().payload([])["suites"][0]

    assert suite["id"] == "office"
    assert suite["version"] == "1.0.0"
    assert {item["id"] for item in suite["benchmarks"]} == {"ocb"}
    assert {item["id"] for item in suite["skills"]} == {"officecli"}
    assert {item["id"] for item in suite["model_presets"]} == {
        "gpt-5-6-luna",
        "deepseek-v4-flash",
    }
    assert suite["benchmark_samples"] == {"ocb": [211]}
    assert suite["extension"]["manifest"].endswith("manifest.yaml")
    assert suite["extension"]["adapter"].endswith("adapter.py")


def test_release_samples_apply_to_estimate_and_cli_command() -> None:
    catalog = EvaluationCatalog()
    request = EvaluationRequest.from_payload({
        "profile": "office-release",
        "benchmark_samples": {"ocb": 211},
    })

    estimate = catalog.adapter("office").estimate(request)  # type: ignore[attr-defined]
    command = catalog.command(request)

    assert estimate["case_counts"] == {"ocb": 211}
    assert estimate["skill_runs"] == 422
    assert estimate["judge_runs"] == 422
    assert command[command.index("--ocb-sample") + 1] == "211"


def test_ci_preflight_is_offline_and_estimate_is_zero() -> None:
    result = EvaluationCatalog().preflight(EvaluationRequest(profile="ci"))

    assert result.ready is True
    assert result.checks["offline"] is True
    assert result.estimate["estimated_tokens"]["total"] == 0


def test_office_run_preflight_distinguishes_missing_and_redacted_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeLangfuse:
        enabled = True
        capture_content = True

        def resolved_public_key(self) -> str:
            return "pk"

        def resolved_secret_key(self) -> str:
            return "sk"

        def resolved_base_url(self) -> str:
            return "https://jp.cloud.langfuse.com"

    config = SimpleNamespace(
        observability=SimpleNamespace(langfuse=FakeLangfuse()),
        providers=SimpleNamespace(
            openai=SimpleNamespace(api_key="openai-key", api_base="https://relay.example"),
            deepseek=SimpleNamespace(api_key="deepseek-key", api_base="https://api.deepseek.com"),
        ),
        resolve_preset=lambda name: SimpleNamespace(
            provider="deepseek" if name == "deepseek-v4-flash" else "openai"
        ),
    )
    monkeypatch.setenv("NANOBOT_BENCHMARK_CACHE", str(tmp_path))
    monkeypatch.setattr(evaluation_catalog, "load_config", lambda: config)
    monkeypatch.setattr(evaluation_catalog, "resolve_config_env_vars", lambda value: value)
    monkeypatch.setattr(
        evaluation_catalog,
        "_soffice_probe",
        lambda _prepared: {"available": True, "path": "/soffice", "version": "test"},
    )

    request = EvaluationRequest(profile="office-smoke", action="run")
    missing = EvaluationCatalog().preflight(request)

    assert "Profile has not been prepared" in missing.blockers
    assert "Prepared Dataset contains redacted content; run licensed prepare" not in missing.blockers

    (tmp_path / "office-smoke.prepared.json").write_text(
        json.dumps({"licensed_content_uploaded": False}),
        encoding="utf-8",
    )
    redacted = EvaluationCatalog().preflight(request)

    assert "Profile has not been prepared" not in redacted.blockers
    assert "Prepared Dataset contains redacted content; run licensed prepare" in redacted.blockers


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


def test_failure_classifier_reports_root_cause_and_concurrent_service_signals() -> None:
    failure = classify_evaluation_failure(
        [
            "Request timed out.",
            "Error code: 503 - Service temporarily unavailable",
            "Evaluator failed: callback got an unexpected keyword argument 'input'",
            "Langfuse score readback failed: missing official_score",
        ],
        "Langfuse score readback failed: missing official_score",
    )

    assert failure["category"] == "evaluator_error"
    assert failure["label"] == "Evaluator code error"
    assert {signal["category"] for signal in failure["signals"]} == {
        "model_relay_unavailable",
        "network_timeout",
        "langfuse_score_missing",
    }


def test_failure_classifier_prefers_missing_annotation_queue_over_old_timeout() -> None:
    failure = classify_evaluation_failure(
        [
            "Request timed out.",
            "NotFoundError: status_code: 404, body: {'message': 'Annotation queue not found'}",
        ],
        "LangfuseNotFoundError",
    )

    assert failure["category"] == "langfuse_queue_missing"
    assert failure["retryable"] is True
    assert {signal["category"] for signal in failure["signals"]} == {"network_timeout"}


def test_old_failed_job_is_enriched_with_failure_details_on_read(tmp_path: Path) -> None:
    store = EvaluationJobStore(tmp_path / "jobs")
    store.write({
        "job_id": "old-failure",
        "status": "failed",
        "phase": "failed",
        "error": "Error code: 503 - Service temporarily unavailable",
        "output_tail": ["Error code: 503 - Service temporarily unavailable"],
        "created_at": "2026-01-01T00:00:00+00:00",
    })
    service = EvaluationJobService(catalog=_FakeCatalog(), store=store)
    service._start_next = lambda: None

    job = service.get("old-failure")

    assert job is not None
    assert job["failure"]["category"] == "model_relay_unavailable"


def test_completed_legacy_score_flush_failure_is_projected_as_awaiting_review(
    tmp_path: Path,
) -> None:
    store = EvaluationJobStore(tmp_path / "jobs")
    store.write({
        "job_id": "score-lag",
        "status": "failed",
        "phase": "failed",
        "total_cases": 1,
        "completed_cases": 1,
        "cases": [{"case_id": "602", "status": "completed", "score_status": "pending"}],
        "error": (
            "LangfuseFlushTimeoutError: Langfuse score ingestion queue did not drain "
            "within 30 seconds (1 unfinished)"
        ),
        "failure": {"category": "langfuse_score_missing"},
        "created_at": "2026-01-01T00:00:00+00:00",
    })
    service = EvaluationJobService(catalog=_FakeCatalog(), store=store)
    service._start_next = lambda: None

    job = service.get("score-lag")

    assert job is not None
    assert job["status"] == "awaiting_review"
    assert job["phase"] == "awaiting_review"
    assert job["failure"] is None
    assert job["resumable"] is False


def test_score_flush_failure_stays_failed_when_any_case_failed(tmp_path: Path) -> None:
    store = EvaluationJobStore(tmp_path / "jobs")
    store.write({
        "job_id": "real-failure",
        "status": "failed",
        "phase": "failed",
        "total_cases": 1,
        "cases": [{"case_id": "602", "status": "failed"}],
        "error": "Langfuse score ingestion queue did not drain within 30 seconds",
        "created_at": "2026-01-01T00:00:00+00:00",
    })
    service = EvaluationJobService(catalog=_FakeCatalog(), store=store)
    service._start_next = lambda: None

    job = service.get("real-failure")

    assert job is not None
    assert job["status"] == "failed"
    assert job["failure"]["category"] == "langfuse_score_missing"


def test_delete_terminal_job_removes_local_history_and_private_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_root = tmp_path / "benchmarks"
    monkeypatch.setenv("NANOBOT_BENCHMARK_CACHE", str(cache_root))
    store = EvaluationJobStore(cache_root / "jobs")
    service = EvaluationJobService(catalog=_FakeCatalog(), store=store)
    service._start_next = lambda: None
    submitted = service.submit(EvaluationRequest(profile="office-smoke"))
    job_id = submitted["job_id"]
    store.update(job_id, status="failed", phase="failed")
    store.path(job_id).with_suffix(".log").write_text("private log", encoding="utf-8")
    store.path(job_id).with_suffix(".progress.jsonl").write_text("{}\n", encoding="utf-8")
    checkpoint_root = cache_root / "runs" / "office-smoke" / "jobs" / job_id
    checkpoint_root.mkdir(parents=True)
    (checkpoint_root / "case.json").write_text("{}\n", encoding="utf-8")

    assert service.delete(job_id) is True
    assert service.get(job_id) is None
    assert not store.path(job_id).with_suffix(".log").exists()
    assert not store.path(job_id).with_suffix(".progress.jsonl").exists()
    assert not checkpoint_root.exists()


def test_delete_rejects_active_job(tmp_path: Path) -> None:
    store = EvaluationJobStore(tmp_path / "jobs")
    service = EvaluationJobService(catalog=_FakeCatalog(), store=store)
    service._start_next = lambda: None
    submitted = service.submit(EvaluationRequest(profile="ci"))

    with pytest.raises(ValueError, match="only terminal"):
        service.delete(submitted["job_id"])

    assert service.get(submitted["job_id"]) is not None


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


def test_langfuse_known_run_projection_reuses_targeted_cache() -> None:
    reader = LangfuseEvaluationReader(cache_ttl_seconds=60)
    calls: list[str] = []

    def fake_get_run(run_id: str) -> dict[str, object]:
        calls.append(run_id)
        return {
            "available": True,
            "error": None,
            "run": {"dataset_run_id": run_id, "cases": [{"case_id": "1"}]},
        }

    reader._get_run_uncached = fake_get_run  # type: ignore[method-assign]
    reader._refresh_run_cache("run-1")

    first = reader.list_runs_by_ids({"run-1"})
    first["runs"].clear()
    second = reader.list_runs_by_ids({"run-1"})

    assert calls == ["run-1"]
    assert second == {
        "available": True,
        "refreshing": False,
        "runs": [{"dataset_run_id": "run-1", "cases": [{"case_id": "1"}]}],
    }


def test_langfuse_known_runs_refresh_serially() -> None:
    reader = LangfuseEvaluationReader(cache_ttl_seconds=60)
    calls: list[str] = []

    def fake_get_run(run_id: str) -> dict[str, object]:
        calls.append(run_id)
        return {"available": True, "error": None, "run": {"dataset_run_id": run_id}}

    reader._get_run_uncached = fake_get_run  # type: ignore[method-assign]
    reader._refresh_run_caches(["run-1", "run-2"])

    assert calls == ["run-1", "run-2"]
    assert set(reader._run_cache) == {"run-1", "run-2"}


def test_langfuse_history_keeps_only_latest_attempt_per_case() -> None:
    first_failed = SimpleNamespace(
        id="attempt-1",
        experiment_item_id="dataset-item-1",
        start_time=datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 7, 29, 1, 1, tzinfo=timezone.utc),
        level="ERROR",
    )
    retry_completed = SimpleNamespace(
        id="attempt-2",
        experiment_item_id="dataset-item-1",
        start_time=datetime(2026, 7, 29, 1, 5, tzinfo=timezone.utc),
        end_time=datetime(2026, 7, 29, 1, 6, tzinfo=timezone.utc),
        level="DEFAULT",
    )
    another_case = SimpleNamespace(
        id="attempt-3",
        experiment_item_id="dataset-item-2",
        start_time=datetime(2026, 7, 29, 1, 2, tzinfo=timezone.utc),
        end_time=datetime(2026, 7, 29, 1, 3, tzinfo=timezone.utc),
        level="DEFAULT",
    )

    latest = _latest_experiment_items([retry_completed, another_case, first_failed])

    assert latest == [retry_completed, another_case]


def test_targeted_run_projection_aggregates_scores_and_release_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "benchmarks"
    cases = root / "cases"
    cases.mkdir(parents=True)
    (cases / "office-release-ocb.jsonl").write_text(
        '\n'.join([
            json.dumps({"metadata": {"case_id": "1"}}),
            json.dumps({"metadata": {"case_id": "2"}}),
        ]) + '\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("nanobot.evaluations.results._BENCHMARK_CACHE_ROOT", root)

    assert _release_case_ids("office-release", "ocb") == {"1", "2"}
    assert _aggregate_case_scores([
        {"scores": {"mybot_score": 0.5}},
        {"scores": {"mybot_score": 1.0}},
        {"scores": {}},
    ]) == {"mybot_score": 0.75}


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


def test_langfuse_v3_scores_are_read_and_judge_rule_name_is_normalized() -> None:
    calls: list[str | None] = []
    pages = iter([
        SimpleNamespace(
            data=[SimpleNamespace(name="mybot-ocb-judge-v1", value=0.75)],
            meta=SimpleNamespace(cursor="next"),
        ),
        SimpleNamespace(
            data=[SimpleNamespace(name="output_present", value=True)],
            meta=SimpleNamespace(cursor=None),
        ),
    ])
    runtime = SimpleNamespace(client=SimpleNamespace(api=SimpleNamespace(
        scores_v3=SimpleNamespace(get_many_v3=lambda **kwargs: (
            calls.append(kwargs.get("cursor")), next(pages)
        )[1]),
    )))

    scores = _trace_scores(runtime, "trace-1")

    assert calls == [None, "next"]
    assert [_score_name(score) for score in scores] == ["mybot_score", "output_present"]


def test_langfuse_text_correction_is_not_projected_as_a_history_score() -> None:
    correction = SimpleNamespace(name="output", value="full corrected model output", data_type="CORRECTION")

    assert _score_value(correction) is None


def test_langfuse_reader_deletes_requested_dataset_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeConfig:
        enabled = True

        def resolved_public_key(self) -> str:
            return "pk"

        def resolved_secret_key(self) -> str:
            return "sk"

    deleted: list[tuple[str, str]] = []

    class FakeClient:
        def auth_check(self) -> bool:
            return True

        class API:
            class Datasets:
                def list(self, **kwargs: object) -> SimpleNamespace:
                    return SimpleNamespace(data=[SimpleNamespace(name="mybot-office")])

            datasets = Datasets()

        api = API()

        def get_dataset_runs(self, *, dataset_name: str, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(data=[SimpleNamespace(id="run-1", name="smoke")])

        def delete_dataset_run(self, *, dataset_name: str, run_name: str) -> None:
            deleted.append((dataset_name, run_name))

    class FakeRuntime:
        client = FakeClient()

        def __init__(self, config: object) -> None:
            del config

        def shutdown(self) -> None:
            pass

    monkeypatch.setattr("nanobot.evaluations.results.load_config", lambda: SimpleNamespace(
        observability=SimpleNamespace(langfuse=FakeConfig()),
    ))
    monkeypatch.setattr("nanobot.evaluations.results.resolve_config_env_vars", lambda config: config)
    monkeypatch.setattr("nanobot.evaluations.results.LangfuseRuntime", FakeRuntime)

    reader = LangfuseEvaluationReader(cache_ttl_seconds=60)
    reader._cache[50] = (0.0, {
        "available": True,
        "runs": [
            {"dataset_run_id": "run-1"},
            {"dataset_run_id": "run-2"},
        ],
    })

    assert reader.delete_runs(["run-1"]) == {"deleted": 1, "missing": []}
    assert deleted == [("mybot-office", "smoke")]
    assert reader._cache[50][1]["runs"] == [{"dataset_run_id": "run-2"}]
    reader._refreshing.add(50)
    assert reader.list_runs(limit=50)["runs"] == [{"dataset_run_id": "run-2"}]


def test_langfuse_reader_does_not_restore_deleted_run_from_refresh() -> None:
    reader = LangfuseEvaluationReader(cache_ttl_seconds=60)
    reader._deleted_run_ids.add("run-1")
    reader._list_runs_uncached = lambda *, limit: {  # type: ignore[method-assign]
        "available": True,
        "runs": [{"dataset_run_id": "run-1"}, {"dataset_run_id": "run-2"}],
    }

    reader._refresh_cache(50)

    assert reader._cache[50][1]["runs"] == [{"dataset_run_id": "run-2"}]


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
        failure={"category": "network_timeout"},
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
    assert resumed["failure"] is None
    assert resumed["resume_history"][0]["from_status"] == "interrupted"


def test_case_rerun_requeues_only_selected_case_from_completed_job(tmp_path: Path) -> None:
    store = EvaluationJobStore(tmp_path / "jobs")
    service = EvaluationJobService(catalog=_FakeCatalog(), store=store)
    service._start_next = lambda: None
    submitted = service.submit(EvaluationRequest(profile="office-smoke"))
    store.update(
        submitted["job_id"],
        status="awaiting_review",
        phase="awaiting_review",
        cases=[
            {"benchmark": "ocb", "skill": "officecli", "case_id": "602", "status": "completed"},
            {"benchmark": "ocb", "skill": "officecli", "case_id": "603", "status": "completed"},
        ],
    )

    rerun = service.rerun_case(
        submitted["job_id"], benchmark="ocb", skill="officecli", case_id="602"
    )

    assert rerun["job_id"] == submitted["job_id"]
    assert rerun["status"] == "queued"
    assert rerun["case_rerun"] == {
        "benchmark": "ocb",
        "skill": "officecli",
        "model_preset": "gpt-5-6-luna",
        "case_id": "602",
    }
    assert rerun["cases"][0]["status"] == "queued"
    assert rerun["cases"][1]["status"] == "completed"
    assert rerun["case_rerun_history"][0]["previous_case_status"] == "completed"


def test_case_rerun_rejects_unknown_or_active_case(tmp_path: Path) -> None:
    store = EvaluationJobStore(tmp_path / "jobs")
    service = EvaluationJobService(catalog=_FakeCatalog(), store=store)
    service._start_next = lambda: None
    submitted = service.submit(EvaluationRequest(profile="office-smoke"))
    store.update(
        submitted["job_id"],
        status="failed",
        cases=[{"benchmark": "ocb", "skill": "officecli", "case_id": "602", "status": "failed"}],
    )

    with pytest.raises(ValueError, match="does not belong"):
        service.rerun_case(submitted["job_id"], benchmark="ocb", skill="officecli", case_id="999")

    store.update(submitted["job_id"], status="failed", cases=[{
        "benchmark": "ocb", "skill": "officecli", "case_id": "602", "status": "running",
    }])
    with pytest.raises(ValueError, match="only failed or completed"):
        service.rerun_case(submitted["job_id"], benchmark="ocb", skill="officecli", case_id="602")

    store.update(submitted["job_id"], status="running")
    with pytest.raises(ValueError, match="cannot rerun"):
        service.rerun_case(submitted["job_id"], benchmark="ocb", skill="officecli", case_id="602")


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


def test_prepare_progress_projects_current_stage(tmp_path: Path) -> None:
    progress = tmp_path / "job.progress.jsonl"
    progress.write_text(json.dumps({
        "event": "prepare_stage",
        "stage": "download_dataset",
        "label": "Download owner/dataset via mirror (1/3)",
    }) + "\n", encoding="utf-8")
    job: dict[str, object] = {"status": "preparing", "phase": "preparing", "cases": []}

    _consume_progress(progress, 0, job)

    assert job["status"] == "preparing"
    assert job["phase"] == "download_dataset"
    assert job["current_variant"] == "Download owner/dataset via mirror (1/3)"


def test_case_rerun_progress_preserves_parent_case_total(tmp_path: Path) -> None:
    progress = tmp_path / "job.progress.jsonl"
    progress.write_text(json.dumps({"event": "run_started", "total_cases": 1}) + "\n", encoding="utf-8")
    job: dict[str, object] = {
        "total_cases": 8,
        "case_rerun": {"benchmark": "ocb", "skill": "officecli", "case_id": "602"},
        "cases": [],
    }

    _consume_progress(progress, 0, job)

    assert job["total_cases"] == 8


def test_variant_run_is_persisted_before_variant_completion(tmp_path: Path) -> None:
    progress = tmp_path / "job.progress.jsonl"
    progress.write_text(json.dumps({
        "event": "variant_run_discovered",
        "benchmark": "ocb",
        "skill": "officecli",
        "dataset_run_id": "run-2",
        "dataset_run_url": "https://langfuse.test/run-2",
    }) + "\n", encoding="utf-8")
    job: dict[str, object] = {
        "total_cases": 1,
        "cases": [{"benchmark": "ocb", "skill": "officecli", "case_id": "602"}],
        "dataset_run_ids": [],
        "langfuse_links": [],
    }

    _consume_progress(progress, 0, job)

    assert job["dataset_run_ids"] == ["run-2"]
    assert job["langfuse_links"] == ["https://langfuse.test/run-2"]
    assert job["cases"][0]["langfuse_url"] == "https://langfuse.test/run-2"  # type: ignore[index]


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
        api=SimpleNamespace(
            experiments=SimpleNamespace(
                list=lambda **_kwargs: SimpleNamespace(data=[SimpleNamespace(start_time=first.end_time)]),
                list_items=lambda **_kwargs: SimpleNamespace(
                    data=[first, latest],
                    meta=SimpleNamespace(cursor=None),
                ),
            ),
            scores_v3=SimpleNamespace(get_many_v3=lambda **_kwargs: SimpleNamespace(
                data=[SimpleNamespace(name="output_present", value=True)],
                meta=SimpleNamespace(cursor=None),
            )),
        ),
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
                "score_names": ["output_present"],
                "score_values": {"output_present": True},
                "trace_url": "https://langfuse.test/project/project-1/traces/trace-completed",
        },
    }


def test_resume_run_name_is_stable_for_job() -> None:
    expected = "mybot-office-smoke-ocb-officecli-job-job_123"

    assert _resume_run_name("office-smoke", "ocb", "officecli", "job_123") == expected
    assert _resume_run_name("office-smoke", "ocb", "officecli", "job_123") == expected
    assert _resume_run_name("office-smoke", "ocb", "officecli", None) is None
    assert _resume_run_name(
        "office-smoke", "ocb", "officecli", "job_123", "deepseek-v4-flash"
    ) == "mybot-office-smoke-ocb-officecli-deepseek-v4-flash-job-job_123"


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


@pytest.mark.asyncio
@pytest.mark.parametrize("skill", ["officecli"])
async def test_benchmark_agent_receives_authoritative_skill_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skill: str,
) -> None:
    captured: dict[str, object] = {}

    class CapturingBot:
        _loop = SimpleNamespace(set_model_preset=lambda *_args, **_kwargs: None)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def run(self, prompt: str, **_kwargs: object) -> SimpleNamespace:
            captured["prompt"] = prompt
            return SimpleNamespace(
                content="done",
                tools_used=[],
                stop_reason="completed",
                error=None,
            )

    monkeypatch.setattr("nanobot.nanobot.Nanobot.from_config", lambda **_kwargs: CapturingBot())
    source = {
        "input": {"prompt": "Inspect the workbook"},
        "metadata": {"case_id": f"case-{skill}"},
    }

    await _run_agent_item(
        source=source,
        benchmark="ocb",
        skill=skill,
        model_preset="gpt-5-6-luna",
        run_root=tmp_path / "run",
    )

    prompt = str(captured["prompt"])
    skill_root = Path(__file__).resolve().parents[2] / "nanobot" / "skills" / skill
    assert f"Selected Skill root: {skill_root}" in prompt
    assert "Do not search /Users, $HOME, /, or parent directories" in prompt
    scripts_dir = "Scripts" if sys.platform == "win32" else "bin"
    launcher = (Path(sys.prefix) / scripts_dir / "officecli").resolve()
    assert f"OfficeCLI launcher: {launcher}" in prompt


@pytest.mark.asyncio
async def test_model_error_is_not_persisted_as_a_case_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailedBot:
        _loop = SimpleNamespace(set_model_preset=lambda *_args, **_kwargs: None)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def run(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                content="Error: service temporarily unavailable",
                tools_used=[],
                stop_reason="error",
                error="Service temporarily unavailable",
            )

    monkeypatch.setattr(
        "nanobot.nanobot.Nanobot.from_config",
        lambda **_kwargs: FailedBot(),
    )
    source = {
        "input": {"prompt": "Create the requested document"},
        "metadata": {"case_id": "case-error"},
    }
    run_root = tmp_path / "run"

    with pytest.raises(BenchmarkError, match="model execution failed.*Service temporarily"):
        await _run_agent_item(
            source=source,
            benchmark="ocb",
            skill="officecli",
            model_preset="gpt-5-6-luna",
            run_root=run_root,
        )

    assert not list((run_root / "case-results").rglob("*.json"))


def test_legacy_model_error_checkpoint_is_not_reused(tmp_path: Path) -> None:
    source = {
        "input": {"prompt": "Create the requested document"},
        "metadata": {"case_id": "case-error"},
    }
    run_root = tmp_path / "run"
    workspace = run_root / "workspace"
    workspace.mkdir(parents=True)
    _write_case_result(
        run_root=run_root,
        benchmark="ocb",
        skill="officecli",
        case_id="case-error",
        model_preset="gpt-5-6-luna",
        source=source,
        workspace=workspace,
        content="Error: {'message': 'Service temporarily unavailable', 'type': 'api_error'}",
        tools_used=[],
    )
    checkpoint = next((run_root / "case-results").rglob("*.json"))
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload["schema_version"] = 2
    payload.pop("stop_reason")
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    assert _load_case_result(
        run_root=run_root,
        benchmark="ocb",
        skill="officecli",
        case_id="case-error",
        model_preset="gpt-5-6-luna",
        source=source,
    ) is None
