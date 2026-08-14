from __future__ import annotations

import hashlib
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as parquet
import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from nanobot.benchmark_adapters import materialize_ocb
from nanobot.cli.benchmark import (
    BenchmarkError,
    _case_manifest_digests,
    _case_manifest_map,
    _clone_at_revision,
    _cloud_smoke,
    _dataset_row_payload,
    _deterministic_stratified_rows,
    _download_hf_snapshot,
    _download_ocb_references,
    _emit_evaluation_progress,
    _enqueue_review_items,
    _ensure_experiment_complete,
    _flush_benchmark_runtime,
    _get_annotation_queue,
    _manifest,
    _missing_ocb_references,
    _read_rows,
    _recover_run_experiment_media_timeout,
    _release_dataset_name,
    _release_usable_ocb_rows,
    _remote_item_has_required_scores,
    _sample_stratum,
    _select_values,
    _update_readme_benchmark_block,
    _validate_case_assets,
    _wait_for_local_scores,
    benchmark_app,
    estimate_payload,
    export_run,
)
from nanobot.runtime.langfuse import LangfuseFlushTimeoutError


def _response(data, **meta):
    return SimpleNamespace(data=data, meta=SimpleNamespace(**meta))


def test_evaluation_progress_serializes_pydantic_scores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Score(BaseModel):
        name: str
        value: float

    progress = tmp_path / "progress.jsonl"
    monkeypatch.setenv("NANOBOT_EVALUATION_PROGRESS_LOG", str(progress))

    _emit_evaluation_progress(
        "case_reconciled",
        scores={"mybot_score": Score(name="mybot_score", value=0.75)},
    )

    event = json.loads(progress.read_text(encoding="utf-8"))
    assert event["scores"]["mybot_score"] == {"name": "mybot_score", "value": 0.75}


class _FakeCloudRuntime:
    def __init__(self, failures: int):
        self.base_url = "https://jp.cloud.langfuse.com"
        self._remaining_failures = failures
        self.readback_calls = 0
        self.client = SimpleNamespace(
            api=SimpleNamespace(trace=SimpleNamespace(get=self._get_trace)),
            get_current_trace_id=lambda: "trace-id",
            _get_project_id=lambda: "project-id",
        )

    @contextmanager
    def observation(self, **kwargs):
        yield SimpleNamespace(observation=object())

    def flush(self, *, strict: bool = False):
        assert strict is True

    def _get_trace(self, trace_id: str):
        assert trace_id == "trace-id"
        self.readback_calls += 1
        if self._remaining_failures:
            self._remaining_failures -= 1
            raise RuntimeError("not ingested yet")
        return SimpleNamespace(id=trace_id)


def test_profiles_pin_public_revisions_and_estimate_tokens() -> None:
    manifest = _manifest()
    for name, spec in manifest["repositories"].items():
        assert len(spec["revision"]) == 40, name
        assert len(spec["license_sha256"]) == 64, name
    assert manifest["smoke_cases"]["ocb"] == ["602", "631", "15", "121"]
    assert "pricing_usd_per_million_tokens" not in manifest
    estimate = estimate_payload("office-smoke", manifest)
    assert estimate["skill_runs"] == 4
    assert estimate["judge_runs"] == 4
    assert estimate["estimated_tokens"] == {
        "agent_input": 72000,
        "agent_output": 20000,
        "judge_input": 48000,
        "judge_output": 6000,
        "total": 146000,
    }


def test_benchmark_constraints_include_ocb_manifest_reader() -> None:
    constraints = (Path(__file__).parents[2] / "benchmarks" / "office" / "constraints.txt").read_text(
        encoding="utf-8"
    )

    assert "pyarrow==23.0.1" in constraints
    assert "pandas==2.3.2" in constraints
    assert "curl_cffi==0.13.0" in constraints
    assert "pypdf==6.15.0" in constraints


def test_cloud_smoke_waits_for_delayed_trace_ingestion(monkeypatch) -> None:
    runtime = _FakeCloudRuntime(failures=12)
    monkeypatch.setattr("nanobot.cli.benchmark.time.sleep", lambda _seconds: None)

    result = _cloud_smoke(runtime)

    assert runtime.readback_calls == 13
    assert result == {
        "trace_id": "trace-id",
        "deep_link": "https://jp.cloud.langfuse.com/project/project-id/traces/trace-id",
    }


def test_cloud_smoke_fails_after_bounded_readback_attempts(monkeypatch) -> None:
    runtime = _FakeCloudRuntime(failures=100)
    monkeypatch.setattr("nanobot.cli.benchmark.time.sleep", lambda _seconds: None)

    with pytest.raises(BenchmarkError, match="trace readback failed"):
        _cloud_smoke(runtime)

    assert runtime.readback_calls == 30


def test_ocb_adapter_uses_explicit_row_ids(tmp_path: Path) -> None:
    root = tmp_path / "ocb"
    (root / "data").mkdir(parents=True)
    (root / "reference_files").mkdir()
    rows = []
    for index, fmt in enumerate(("docx", "xlsx", "pptx")):
        filename = f"fixture-{index}.{fmt}"
        (root / "reference_files" / filename).write_bytes(f"fixture-{index}".encode())
        rows.append({
            "reference_files": [filename],
            "file_format": fmt,
            "version": "v1",
            "track": "test",
            "question": f"question-{index}",
            "expected_assertions": [f"answer-{index}"],
            "domain": "test",
            "feature": "test",
            "app_type": fmt,
            "weights": [1.0],
        })
    parquet.write_table(pa.Table.from_pylist(rows), root / "data" / "ocb_qna_data.parquet")
    output = tmp_path / "ocb.jsonl"

    materialize_ocb(root, output, case_ids=[2, 0])

    materialized = [json.loads(line) for line in output.read_text().splitlines()]
    assert [item["metadata"]["case_id"] for item in materialized] == ["2", "0"]
    assert materialized[0]["input"]["format"] == "pptx"
    assert materialized[0]["input"]["reference_sha256"][0]


def test_case_manifest_reader_preserves_unicode_line_separator(tmp_path: Path) -> None:
    path = tmp_path / "ocb.jsonl"
    rows = [
        {"input": {"prompt": "first\u2028paragraph"}, "metadata": {"case_id": "0"}},
        {"input": {"prompt": "second"}, "metadata": {"case_id": "1"}},
    ]
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )

    assert _read_rows(path) == rows


def test_hf_snapshot_download_retries_and_deduplicates_patterns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, str] | None, float | None]] = []

    def fake_run(command: list[str], *, cwd=None, env=None, timeout_seconds=None) -> str:
        calls.append((command, env, timeout_seconds))
        if len(calls) <= 3:
            raise BenchmarkError("connection interrupted")
        return str(tmp_path / "dataset")

    monkeypatch.setattr("nanobot.cli.benchmark._run", fake_run)
    monkeypatch.setattr("nanobot.cli.benchmark.time.sleep", lambda _seconds: None)

    result = _download_hf_snapshot(
        Path(sys.executable),
        {"dataset_id": "owner/dataset", "dataset_revision": "revision"},
        tmp_path / "dataset",
        ["reference_files/example.docx", "reference_files/example.docx"],
    )

    assert result == (tmp_path / "dataset").resolve()
    assert len(calls) == 4
    assert calls[0][0].count("reference_files/example.docx") == 1
    assert calls[0][1]["HF_HUB_DISABLE_XET"] == "1"
    assert calls[0][1]["HF_ENDPOINT"] == "https://huggingface.co"
    assert calls[3][1]["HF_ENDPOINT"] == "https://hf-mirror.com"
    assert calls[0][1]["HF_HUB_ETAG_TIMEOUT"] == "10"
    assert calls[0][1]["HF_HUB_DOWNLOAD_TIMEOUT"] == "120"
    assert calls[0][2] == 7_200
    assert "max_workers=1" in calls[0][0][2]


def test_pinned_clean_source_checkout_is_reused_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "a" * 40
    target = tmp_path / "sources" / "ocb"
    (target / ".git").mkdir(parents=True)
    license_path = target / "LICENSE"
    license_path.write_text("license", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, cwd=None, env=None, timeout_seconds=None) -> str:
        calls.append(command)
        if command[1:3] == ["rev-parse", "HEAD"]:
            return revision
        if command[1:3] == ["status", "--porcelain"]:
            return ""
        raise AssertionError(f"unexpected network or checkout command: {command}")

    monkeypatch.setattr("nanobot.cli.benchmark._run", fake_run)

    result = _clone_at_revision(
        "ocb",
        {
            "url": "https://github.com/example/ocb",
            "revision": revision,
            "license_sha256": hashlib.sha256(b"license").hexdigest(),
        },
        tmp_path,
    )

    assert result == target
    assert calls == [["git", "rev-parse", "HEAD"], ["git", "status", "--porcelain"]]


def test_ocb_reference_conversion_uses_bounded_adobe_client_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, str] | None, float | None]] = []
    output_root = tmp_path / "data" / "reference_files"
    output_root.mkdir(parents=True)
    rows = [{
        "input": {
            "reference_paths": ["/cache/first.pptx", "/cache/second.pptx"],
            "reference_sha256": [None, None],
        },
    }]

    def fake_run(command: list[str], *, cwd=None, env=None, timeout_seconds=None) -> str:
        calls.append((command, env, timeout_seconds))
        if len(calls) == 1:
            (output_root / "first.pptx").write_bytes(b"x" * 2048)
        else:
            (output_root / "second.pptx").write_bytes(b"y" * 2048)
        return ""

    monkeypatch.setattr("nanobot.cli.benchmark._run", fake_run)
    monkeypatch.setattr("nanobot.cli.benchmark.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "nanobot.cli.benchmark.adobe_pdf_services_env", lambda: {"SAFE": "1"}
    )

    _download_ocb_references(
        Path(sys.executable),
        tmp_path / "source",
        tmp_path / "data",
        rows,
    )

    assert len(calls) == 2
    compile(calls[0][0][2], "<ocb-downloader-wrapper>", "exec")
    assert "connect_timeout=30000" in calls[0][0][2]
    assert "read_timeout=120000" in calls[0][0][2]
    assert "item[0] != socket.AF_INET" in calls[0][0][2]
    assert "original_standard_get(*args, **kwargs)" in calls[0][0][2]
    assert "from curl_cffi import requests as browser_requests" in calls[0][0][2]
    assert "CurlHttpVersion.V1_1" in calls[0][0][2]
    assert '("safari", None)' in calls[0][0][2]
    assert '("firefox", None)' in calls[0][0][2]
    assert "writer.clone_document_from_reader(reader)" in calls[0][0][2]
    assert calls[0][1] == {"SAFE": "1"}
    assert calls[0][2] == 240.0
    assert calls[0][0][-3] == "first.pptx"
    assert calls[1][0][-3] == "second.pptx"


def test_ocb_reference_conversion_isolates_native_downloader_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    output_root = tmp_path / "data" / "reference_files"
    output_root.mkdir(parents=True)
    rows = [{
        "input": {
            "reference_paths": ["/cache/crash.pptx", "/cache/ready.pptx"],
            "reference_sha256": [None, None],
        },
    }]

    def fake_run(command: list[str], *, cwd=None, env=None, timeout_seconds=None) -> str:
        filename = command[-3]
        calls.append(filename)
        if filename == "crash.pptx":
            raise BenchmarkError("downloader exited with signal 11")
        (output_root / filename).write_bytes(b"x" * 2048)
        return ""

    monkeypatch.setattr("nanobot.cli.benchmark._run", fake_run)
    monkeypatch.setattr("nanobot.cli.benchmark.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "nanobot.cli.benchmark.adobe_pdf_services_env", lambda: {"SAFE": "1"}
    )

    _download_ocb_references(
        Path(sys.executable),
        tmp_path / "source",
        tmp_path / "data",
        rows,
    )

    assert "ready.pptx" in calls
    assert calls.count("crash.pptx") == 3
    assert (output_root / "ready.pptx").is_file()


def test_missing_ocb_references_are_reported_without_local_paths() -> None:
    rows = [{
        "input": {
            "reference_paths": ["/private/cache/missing.pptx", "/private/cache/ready.xlsx"],
            "reference_sha256": [None, "a" * 64],
        },
    }]

    assert _missing_ocb_references(rows) == ["missing.pptx"]


def test_case_manifest_digests_and_assets_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "office-smoke-ocb.jsonl").write_text("ocb", encoding="utf-8")
    digests = _case_manifest_digests(tmp_path, "office-smoke")
    assert set(digests) == {"ocb"}
    assert all(len(value) == 64 for value in digests.values())

    with pytest.raises(BenchmarkError, match="missing.pptx"):
        _validate_case_assets({
            "ocb": [{"input": {"reference_paths": [str(tmp_path / "missing.pptx")]}}],
        })


def test_run_filters_are_validated_and_deduplicated() -> None:
    assert _select_values(["ocb", "ocb"], ("ocb",), "benchmark") == ("ocb",)
    with pytest.raises(BenchmarkError, match="unknown benchmark"):
        _select_values(["removed"], ("ocb",), "benchmark")


def test_release_case_manifest_is_sampled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = "".join(
        json.dumps({
            "input": {"format": "docx"},
            "metadata": {"case_id": f"ocb-{index}", "track": "qa"},
        }) + "\n"
        for index in range(1018)
    )
    (tmp_path / "office-release-ocb.jsonl").write_text(rows, encoding="utf-8")
    monkeypatch.setattr(
        "nanobot.cli.benchmark._release_usable_ocb_rows",
        lambda values: values[:211],
    )

    sampled = _case_manifest_map(
        {"case_manifest_root": str(tmp_path)},
        "office-release",
        benchmark_samples={"ocb": 211},
    )

    assert {benchmark: len(rows) for benchmark, rows in sampled.items()} == {"ocb": 211}


def test_release_usable_subset_excludes_only_fixed_unavailable_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "input": {"format": "docx"},
            "metadata": {"case_id": str(index), "track": "qa"},
        }
        for index in range(255)
    ]
    excluded = frozenset(str(index) for index in range(44))
    monkeypatch.setattr(
        "nanobot.cli.benchmark._deterministic_stratified_rows",
        lambda _benchmark, values: values,
    )
    monkeypatch.setattr(
        "nanobot.cli.benchmark._RELEASE_EXCLUDED_OCB_CASE_IDS",
        excluded,
    )

    usable = _release_usable_ocb_rows(rows)

    assert len(usable) == 211
    assert {row["metadata"]["case_id"] for row in usable}.isdisjoint(excluded)


def test_release_sampling_is_reproducible_proportional_and_nested() -> None:
    rows = [
        {
            "input": {"case_id": f"docx-{index}", "format": "docx"},
            "metadata": {"case_id": f"docx-{index}", "track": "qa"},
        }
        for index in range(80)
    ] + [
        {
            "input": {"case_id": f"pptx-{index}", "format": "pptx"},
            "metadata": {"case_id": f"pptx-{index}", "track": "qa"},
        }
        for index in range(20)
    ]

    ordered = _deterministic_stratified_rows("ocb", rows)
    reordered_input = _deterministic_stratified_rows("ocb", list(reversed(rows)))
    ordered_ids = [row["metadata"]["case_id"] for row in ordered]
    reordered_ids = [row["metadata"]["case_id"] for row in reordered_input]
    quarter = ordered_ids[:25]
    half = ordered_ids[:50]

    assert ordered_ids == reordered_ids
    assert ordered_ids != [row["metadata"]["case_id"] for row in rows]
    assert set(quarter) < set(half) < set(ordered_ids)
    assert sum(case_id.startswith("pptx-") for case_id in quarter) == 5
    assert sum(case_id.startswith("pptx-") for case_id in half) == 10
    assert _release_dataset_name("office-release-ocb", "ocb", 211) == "office-release-ocb"


def test_release_sampling_stratum_is_format_and_track() -> None:
    row = {"input": {"format": "xlsx"}, "metadata": {"track": "edit"}}
    assert _sample_stratum("ocb", row) == "xlsx|edit"


def test_experiment_result_omission_fails_with_case_details() -> None:
    pending = [SimpleNamespace(id="item-1"), SimpleNamespace(id="item-2")]
    result = SimpleNamespace(
        item_results=[SimpleNamespace(item=SimpleNamespace(id="item-1"))]
    )

    with pytest.raises(BenchmarkError, match=r"1/2.*1-11/0"):
        _ensure_experiment_complete(
            result,
            pending,
            case_by_item_id={"item-1": "1-10/0", "item-2": "1-11/0"},
            task_failures=[("1-11/0", "workspace failed")],
        )


def test_remote_resume_requires_output_present() -> None:
    complete = {
        "score_names": ["output_present", "mybot_score"],
        "score_values": {"output_present": True, "mybot_score": 0.0},
    }
    missing = {"score_names": ["mybot_score"], "score_values": {"mybot_score": 1.0}}
    assert _remote_item_has_required_scores("ocb", complete) is True
    assert _remote_item_has_required_scores("ocb", missing) is False


def test_score_readback_lag_keeps_completed_case_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nanobot.cli.benchmark._SCORE_READBACK_ATTEMPTS", 1)
    monkeypatch.setattr("nanobot.cli.benchmark._remote_trace_scores", lambda *_args, **_kwargs: {})

    assert _wait_for_local_scores(
        SimpleNamespace(),
        trace_id="trace-late",
        benchmark="ocb",
    ) == {}


def test_benchmark_flush_tolerates_async_queue_timeouts() -> None:
    def raise_score_timeout(**_kwargs) -> None:
        raise LangfuseFlushTimeoutError(
            "Langfuse score ingestion queue did not drain within 30 seconds (1 unfinished)"
        )

    _flush_benchmark_runtime(SimpleNamespace(flush=raise_score_timeout))

    def raise_media_timeout(**_kwargs) -> None:
        raise LangfuseFlushTimeoutError(
            "Langfuse media upload queue did not drain within 30 seconds (1 unfinished)"
        )

    _flush_benchmark_runtime(SimpleNamespace(flush=raise_media_timeout))

    def raise_otel_timeout(**_kwargs) -> None:
        raise LangfuseFlushTimeoutError(
            "Langfuse OTEL flush did not finish within 30 seconds"
        )

    with pytest.raises(LangfuseFlushTimeoutError, match="OTEL flush"):
        _flush_benchmark_runtime(SimpleNamespace(flush=raise_otel_timeout))


def test_run_experiment_media_timeout_requires_complete_remote_readback() -> None:
    pending = [SimpleNamespace(id="item-1"), SimpleNamespace(id="item-2")]
    complete = {
        "status": "completed",
        "score_names": ["output_present"],
        "score_values": {"output_present": True},
    }
    media_timeout = LangfuseFlushTimeoutError(
        "Langfuse media upload queue did not drain within 30 seconds (2 unfinished)"
    )
    recovered = {
        "run_id": "run-1",
        "items": {"item-1": complete, "item-2": complete},
    }

    assert _recover_run_experiment_media_timeout(
        media_timeout,
        benchmark="ocb",
        pending_items=pending,
        recovered=recovered,
        task_failures=[],
    )

    for exception in (
        RuntimeError("network failed"),
        LangfuseFlushTimeoutError("Langfuse OTEL flush did not finish within 30 seconds"),
    ):
        assert not _recover_run_experiment_media_timeout(
            exception,
            benchmark="ocb",
            pending_items=pending,
            recovered=recovered,
            task_failures=[],
        )

    assert not _recover_run_experiment_media_timeout(
        media_timeout,
        benchmark="ocb",
        pending_items=pending,
        recovered={"run_id": "run-1", "items": {"item-1": complete}},
        task_failures=[],
    )
    assert not _recover_run_experiment_media_timeout(
        media_timeout,
        benchmark="ocb",
        pending_items=pending,
        recovered=recovered,
        task_failures=[("case-2", "task failed")],
    )


def test_dataset_payload_never_uploads_local_paths() -> None:
    row = {
        "input": {
            "case_id": "case",
            "prompt": "licensed prompt",
            "reference_paths": ["/private/reference.docx"],
            "material_paths": ["/private/material.pdf"],
            "source_config": "/private/config.json",
        },
        "expected_output": {
            "gold": ["answer"],
            "rubric": {"criteria": ["correct"]},
        },
    }
    withheld_input, withheld_expected = _dataset_row_payload(
        row,
        allow_licensed_content=False,
    )
    assert "prompt" not in withheld_input
    assert withheld_expected["content_withheld"] is True
    uploaded_input, uploaded_expected = _dataset_row_payload(
        row,
        allow_licensed_content=True,
    )
    assert uploaded_input["prompt"] == "licensed prompt"
    assert uploaded_expected == row["expected_output"]
    serialized = json.dumps({"input": uploaded_input, "expected_output": uploaded_expected})
    assert "/private/" not in serialized


class _FakeExperiments:
    def __init__(self, experiment, items):
        self.experiment = experiment
        self.items = items

    def list(self, **kwargs):
        return _response([self.experiment], cursor=None)

    def list_items(self, **kwargs):
        return _response(self.items, cursor=None)


class _FakeQueues:
    def __init__(self, queue, queue_items):
        self.queue = queue
        self.queue_items = queue_items

    def list_queues(self, **kwargs):
        return _response([self.queue], page=1, limit=100, total_items=1, total_pages=1)

    def list_queue_items(self, queue_id, **kwargs):
        assert queue_id == self.queue.id
        return _response(
            self.queue_items,
            page=1,
            limit=100,
            total_items=len(self.queue_items),
            total_pages=1,
        )


class _FakeClient:
    def __init__(self, experiment, items, queue, queue_items):
        self.api = SimpleNamespace(
            experiments=_FakeExperiments(experiment, items),
            annotation_queues=_FakeQueues(queue, queue_items),
            scores_v3=SimpleNamespace(get_many_v3=lambda **_kwargs: _response([], cursor=None)),
        )
        self._base_url = "https://jp.cloud.langfuse.com"

    def _get_project_id(self):
        return "project-id"


def _score(name: str, value):
    return SimpleNamespace(name=name, value=value)


def test_annotation_queue_capacity_reuses_existing_mybot_queue() -> None:
    existing = SimpleNamespace(id="existing-queue", name="mybot-office-smoke-ocb-review")

    class CapacityError(RuntimeError):
        status_code = 405
        body = {"message": "Maximum number of annotation queues reached on Hobby plan."}

    queues = SimpleNamespace(
        list_queues=lambda **_kwargs: _response([existing]),
        create_queue=lambda **_kwargs: (_ for _ in ()).throw(CapacityError()),
    )
    score_configs = SimpleNamespace(
        get=lambda **_kwargs: _response(
            [SimpleNamespace(id="score-config", name="mybot-human-review", is_archived=False)]
        )
    )
    client = SimpleNamespace(
        api=SimpleNamespace(annotation_queues=queues, score_configs=score_configs)
    )

    queue = _get_annotation_queue(client, "mybot-office-smoke-review")

    assert queue is existing


def test_enqueue_review_items_recreates_deleted_annotation_queue() -> None:
    stale = SimpleNamespace(id="deleted-queue", name="mybot-office-smoke-review")
    recreated = SimpleNamespace(id="recreated-queue", name=stale.name)

    class MissingQueueError(RuntimeError):
        status_code = 404
        body = {"message": "Annotation queue not found"}

    created_items = []

    class Queues:
        def list_queue_items(self, queue_id, **_kwargs):
            if queue_id == stale.id:
                raise MissingQueueError()
            assert queue_id == recreated.id
            return _response([])

        def list_queues(self, **_kwargs):
            return _response([])

        def create_queue(self, **_kwargs):
            return recreated

        def create_queue_item(self, queue_id, **kwargs):
            created_items.append((queue_id, kwargs["object_id"]))

    client = SimpleNamespace(
        api=SimpleNamespace(
            annotation_queues=Queues(),
            score_configs=SimpleNamespace(
                get=lambda **_kwargs: _response([
                    SimpleNamespace(
                        id="score-config",
                        name="mybot-human-review",
                        is_archived=False,
                    )
                ])
            ),
        )
    )

    queue_id, added = _enqueue_review_items(
        SimpleNamespace(client=client),
        queue=stale,
        trace_ids=["trace-1", "trace-2"],
        profile="office-smoke",
    )

    assert queue_id == recreated.id
    assert added == 2
    assert created_items == [
        (recreated.id, "trace-1"),
        (recreated.id, "trace-2"),
    ]


def test_export_requires_scores_review_and_builds_real_deep_link() -> None:
    now = datetime.now(timezone.utc)
    experiment = SimpleNamespace(
        id="run-id",
        name="run",
        start_time=now,
        end_time=now,
        item_count=1,
        dataset_id="dataset-id",
        metadata={
            "profile": "office-smoke",
            "benchmark": "ocb",
            "evaluation_source": "langfuse_terra",
            "required_remote_score": "mybot_score",
            "annotation_queue_name": "review-queue",
        },
        scores=[],
    )
    item = SimpleNamespace(
        trace_id="trace-id",
        end_time=now,
        level="DEFAULT",
        scores=[
            _score("output_present", True),
            _score("mybot_score", 0.0),
            _score("mybot-human-review", 1.0),
        ],
    )
    queue = SimpleNamespace(id="queue-id", name="review-queue")
    queue_item = SimpleNamespace(object_id="trace-id", status="COMPLETED")

    payload = export_run(_FakeClient(experiment, [item], queue, [queue_item]), "run-id")

    assert payload["reviewed_items"] == 1
    assert payload["deep_link"].endswith("/datasets/dataset-id/runs/run-id")
    assert "{project-id}" not in payload["deep_link"]


def test_export_finds_reused_queue_when_old_run_metadata_is_stale() -> None:
    now = datetime.now(timezone.utc)
    experiment = SimpleNamespace(
        id="run-id",
        name="run",
        start_time=now,
        end_time=now,
        item_count=1,
        dataset_id="dataset-id",
        metadata={
            "profile": "office-smoke",
            "benchmark": "ocb",
            "required_remote_score": "mybot_score",
            "annotation_queue_name": "mybot-office-smoke-ocb-officecli-review",
        },
        scores=[],
    )
    item = SimpleNamespace(
        trace_id="trace-id",
        end_time=now,
        level="DEFAULT",
        scores=[
            _score("output_present", True),
            _score("mybot_score", 0.5),
            _score("mybot-human-review", 1.0),
        ],
    )
    queue = SimpleNamespace(id="queue-id", name="mybot-office-smoke-ocb-officecli-review")
    queue_item = SimpleNamespace(object_id="trace-id", status="COMPLETED")

    payload = export_run(_FakeClient(experiment, [item], queue, [queue_item]), "run-id")

    assert payload["annotation_queue_name"] == queue.name


def test_export_rejects_missing_remote_judge_score() -> None:
    now = datetime.now(timezone.utc)
    experiment = SimpleNamespace(
        id="run-id",
        name="run",
        start_time=now,
        end_time=now,
        item_count=1,
        dataset_id="dataset-id",
        metadata={
            "profile": "office-smoke",
            "benchmark": "ocb",
            "required_remote_score": "mybot_score",
            "annotation_queue_name": "review-queue",
        },
        scores=[],
    )
    item = SimpleNamespace(
        trace_id="trace-id",
        end_time=now,
        level="DEFAULT",
        scores=[_score("output_present", True)],
    )
    queue = SimpleNamespace(id="queue-id", name="review-queue")
    with pytest.raises(BenchmarkError, match="mybot_score"):
        export_run(_FakeClient(experiment, [item], queue, []), "run-id")


def test_ci_estimate_needs_no_keys_or_network() -> None:
    result = CliRunner().invoke(benchmark_app, ["estimate", "--profile", "ci"])
    assert result.exit_code == 0
    assert '"total": 0' in result.stdout


def test_run_has_no_price_confirmation_option() -> None:
    result = CliRunner().invoke(benchmark_app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--confirm-cost" not in result.stdout
    assert "--parent-run-id" in result.stdout
    assert "--benchmark" in result.stdout
    assert "--skill" in result.stdout
    assert "--ocb-sample" in result.stdout


def test_export_readme_block_is_controlled(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(
        "before\n<!-- benchmark-results:begin -->\nold\n<!-- benchmark-results:end -->\nafter\n",
        encoding="utf-8",
    )
    _update_readme_benchmark_block(
        {
            "benchmark": "ocb",
            "evaluation_source": "langfuse_terra",
            "required_score": "mybot_score",
            "item_count": 1,
            "reviewed_items": 1,
            "required_reviewed_items": 1,
            "deep_link": "https://jp.cloud.langfuse.com/project/p/datasets/d/r",
        },
        readme,
    )
    content = readme.read_text(encoding="utf-8")
    assert "old" not in content
    assert "mybot_score" in content
    assert content.count("benchmark-results:begin") == 1
