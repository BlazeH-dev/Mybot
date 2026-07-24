from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as parquet
import pytest
from typer.testing import CliRunner

from nanobot.benchmark_adapters import materialize_ocb, materialize_officebench
from nanobot.cli.benchmark import (
    BenchmarkError,
    _dataset_row_payload,
    _manifest,
    _officebench_evaluator,
    _stage_case_workspace,
    _update_readme_benchmark_block,
    benchmark_app,
    estimate_payload,
    export_run,
)


def _response(data, **meta):
    return SimpleNamespace(data=data, meta=SimpleNamespace(**meta))


def test_profiles_pin_public_revisions_and_require_all_prices() -> None:
    manifest = _manifest()
    for name, spec in manifest["repositories"].items():
        assert len(spec["revision"]) == 40, name
        assert len(spec["license_sha256"]) == 64, name
    assert manifest["smoke_cases"]["ocb"] == ["602", "631", "15", "121"]
    assert estimate_payload("office-smoke", 238, manifest)["pricing_configured"] is False
    priced = json.loads(json.dumps(manifest))
    priced["pricing_usd_per_million_tokens"] = {
        "input": 1,
        "output": 2,
        "judge_input": 3,
        "judge_output": 4,
    }
    estimate = estimate_payload("office-smoke", 238, priced)
    assert estimate["skill_runs"] == 24
    assert estimate["pricing_configured"] is True


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
        "expected_output": {"gold": ["answer"]},
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
    serialized = json.dumps(uploaded_input)
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
    assert '"estimated_usd": 0' in result.stdout


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
