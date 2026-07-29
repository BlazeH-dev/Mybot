from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as parquet
import pytest
from typer.testing import CliRunner

from nanobot.benchmark_adapters import (
    materialize_ocb,
    materialize_officebench,
    materialize_presentbench,
)
from nanobot.cli.benchmark import (
    BenchmarkError,
    _case_manifest_digests,
    _case_manifest_map,
    _cloud_smoke,
    _dataset_row_payload,
    _deterministic_stratified_rows,
    _manifest,
    _missing_ocb_references,
    _officebench_evaluator,
    _release_dataset_name,
    _sample_stratum,
    _select_values,
    _stage_case_workspace,
    _update_readme_benchmark_block,
    _validate_case_assets,
    benchmark_app,
    estimate_payload,
    export_run,
)


def _response(data, **meta):
    return SimpleNamespace(data=data, meta=SimpleNamespace(**meta))


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
    estimate = estimate_payload("office-smoke", 238, manifest)
    assert estimate["skill_runs"] == 24
    assert estimate["judge_runs"] == 16
    assert estimate["estimated_tokens"] == {
        "agent_input": 432000,
        "agent_output": 120000,
        "judge_input": 192000,
        "judge_output": 24000,
        "total": 768000,
    }


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


def test_missing_ocb_references_are_reported_without_local_paths() -> None:
    rows = [{
        "input": {
            "reference_paths": ["/private/cache/missing.pptx", "/private/cache/ready.xlsx"],
            "reference_sha256": [None, "a" * 64],
        },
    }]

    assert _missing_ocb_references(rows) == ["missing.pptx"]


def test_case_manifest_digests_and_assets_fail_closed(tmp_path: Path) -> None:
    for benchmark in ("ocb", "officebench", "presentbench"):
        (tmp_path / f"office-smoke-{benchmark}.jsonl").write_text(benchmark, encoding="utf-8")
    digests = _case_manifest_digests(tmp_path, "office-smoke")
    assert set(digests) == {"ocb", "officebench", "presentbench"}
    assert all(len(value) == 64 for value in digests.values())

    with pytest.raises(BenchmarkError, match="missing.pptx"):
        _validate_case_assets({
            "ocb": [{"input": {"reference_paths": [str(tmp_path / "missing.pptx")]}}],
        })


def test_run_filters_are_validated_and_deduplicated() -> None:
    assert _select_values(["ocb", "ocb"], ("ocb", "officebench"), "benchmark") == ("ocb",)
    with pytest.raises(BenchmarkError, match="unknown benchmark"):
        _select_values(["presentbench"], ("ocb", "officebench"), "benchmark")


def test_release_case_manifests_are_sampled_per_benchmark(tmp_path: Path) -> None:
    full_counts = {"ocb": 1018, "officebench": 93, "presentbench": 238}
    for benchmark, count in full_counts.items():
        rows = "".join(
            json.dumps({"metadata": {"case_id": f"{benchmark}-{index}"}}) + "\n"
            for index in range(count)
        )
        (tmp_path / f"office-release-{benchmark}.jsonl").write_text(rows, encoding="utf-8")

    sampled = _case_manifest_map(
        {"case_manifest_root": str(tmp_path)},
        "office-release",
        benchmark_samples={"ocb": 255, "officebench": 24, "presentbench": 60},
    )

    assert {benchmark: len(rows) for benchmark, rows in sampled.items()} == {
        "ocb": 255,
        "officebench": 24,
        "presentbench": 60,
    }


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
    assert _release_dataset_name("office-release-ocb", "ocb", 255) == (
        "office-release-ocb-strat-v1-n255"
    )
    assert _release_dataset_name("office-release-ocb", "ocb", 1018) == "office-release-ocb"


@pytest.mark.parametrize(
    ("benchmark", "row", "expected"),
    [
        (
            "ocb",
            {"input": {"format": "xlsx"}, "metadata": {"track": "edit"}},
            "xlsx|edit",
        ),
        (
            "officebench",
            {"input": {}, "metadata": {"case_id": "1-10/2"}},
            "1-10",
        ),
        (
            "presentbench",
            {"input": {}, "metadata": {"domain": "education"}},
            "education",
        ),
    ],
)
def test_release_sampling_strata(
    benchmark: str,
    row: dict[str, object],
    expected: str,
) -> None:
    assert _sample_stratum(benchmark, row) == expected


def test_officebench_staging_and_pinned_evaluator_contract(tmp_path: Path) -> None:
    source_root = tmp_path / "OfficeBench"
    task_root = source_root / "tasks" / "1-1"
    (task_root / "subtasks").mkdir(parents=True)
    (task_root / "testbed" / "data").mkdir(parents=True)
    (task_root / "reference").mkdir()
    (task_root / "testbed" / "data" / "input.txt").write_text("input")
    (task_root / "reference" / "expected.txt").write_text("expected")
    (task_root / "subtasks" / "0.json").write_text(json.dumps({
        "task": "create data/result.txt",
        "evaluation": [{"function": "synthetic", "args": {}}],
    }))
    (source_root / "evaluation.py").write_text(
        "from pathlib import Path\n"
        "def evaluate_output(task_id, subtask_id, output_dir):\n"
        "    return (Path(output_dir) / 'data' / 'result.txt').read_text() == 'ok'\n"
    )
    manifest_path = tmp_path / "officebench.jsonl"
    rows = materialize_officebench(source_root, manifest_path, cases=["1-1/0"])
    workspace = _stage_case_workspace(
        benchmark="officebench",
        source=rows[0],
        run_root=tmp_path / "run",
        skill="office-python",
    )
    (workspace / "data" / "result.txt").write_text("ok")

    from nanobot.cli import benchmark as benchmark_module

    benchmark_module._WORKSPACES["workspace-token"] = workspace
    evaluations = _officebench_evaluator(
        output={"workspace_token": "workspace-token", "case_id": "1-1/0"},
        benchmark_python=Path(sys.executable),
        source_root=source_root,
    )

    values = {evaluation.name: evaluation.value for evaluation in evaluations}
    assert values == {"official_score": 1.0, "official_evaluator_ok": True}
    assert (workspace.parents[3] / "reference" / "expected.txt").is_file()


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


def test_presentbench_adapter_embeds_rubric_without_local_path(tmp_path: Path) -> None:
    case_root = tmp_path / "advertising" / "example"
    task_root = case_root / "generation_task"
    task_root.mkdir(parents=True)
    (task_root / "instructions.md").write_text("Create a deck", encoding="utf-8")
    rubric = {"criteria": [{"name": "content", "weight": 1.0}]}
    (task_root / "judge_prompt.json").write_text(json.dumps(rubric), encoding="utf-8")
    (case_root / "material.pdf").write_bytes(b"fixture")

    rows = materialize_presentbench(
        tmp_path,
        tmp_path / "presentbench.jsonl",
        cases=["advertising/example"],
    )

    expected = rows[0]["expected_output"]
    assert expected["rubric"] == rubric
    assert "judge_prompt_path" not in expected
    assert "/Users/" not in json.dumps(expected)


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
        )
        self._base_url = "https://jp.cloud.langfuse.com"

    def _get_project_id(self):
        return "project-id"


def _score(name: str, value):
    return SimpleNamespace(name=name, value=value)


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
            "benchmark": "officebench",
            "evaluation_source": "officebench_official",
            "required_remote_score": "official_score",
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
            _score("official_score", 0.0),
            _score("official_evaluator_ok", True),
            _score("mybot-human-review", 1.0),
        ],
    )
    queue = SimpleNamespace(id="queue-id", name="review-queue")
    queue_item = SimpleNamespace(object_id="trace-id", status="COMPLETED")

    payload = export_run(_FakeClient(experiment, [item], queue, [queue_item]), "run-id")

    assert payload["reviewed_items"] == 1
    assert payload["deep_link"].endswith("/datasets/dataset-id/runs/run-id")
    assert "{project-id}" not in payload["deep_link"]


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
    assert "--officebench-sample" in result.stdout
    assert "--presentbench-sample" in result.stdout


def test_export_readme_block_is_controlled(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(
        "before\n<!-- benchmark-results:begin -->\nold\n<!-- benchmark-results:end -->\nafter\n",
        encoding="utf-8",
    )
    _update_readme_benchmark_block(
        {
            "benchmark": "officebench",
            "evaluation_source": "officebench_official",
            "required_score": "official_score",
            "item_count": 1,
            "reviewed_items": 1,
            "required_reviewed_items": 1,
            "deep_link": "https://jp.cloud.langfuse.com/project/p/datasets/d/r",
        },
        readme,
    )
    content = readme.read_text(encoding="utf-8")
    assert "old" not in content
    assert "official_score" in content
    assert content.count("benchmark-results:begin") == 1
