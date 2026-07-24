"""Materialize immutable, de-identified case manifests for public Office sets."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_digest(root: Path) -> str:
    """Digest a benchmark fixture without embedding its contents in Git."""
    entries: list[tuple[str, str]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        entries.append((str(path.relative_to(root)), _file_digest(path)))
    return _digest(entries)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def materialize_ocb(
    data_root: str | Path,
    output_path: str | Path,
    *,
    case_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Materialize fixed OCB row ids from the pinned Parquet dataset."""
    import pyarrow.parquet as parquet

    root = Path(data_root).resolve()
    table = parquet.read_table(root / "data" / "ocb_qna_data.parquet")
    rows = table.to_pylist()
    selected_ids = list(range(len(rows))) if case_ids is None else list(case_ids)
    invalid = [case_id for case_id in selected_ids if case_id < 0 or case_id >= len(rows)]
    if invalid:
        raise ValueError(f"OCB case ids are outside the pinned dataset: {invalid}")
    selected = [(case_id, rows[case_id]) for case_id in selected_ids]
    output: list[dict[str, Any]] = []
    for row_id, row in selected:
        references = [str(item) for item in row.get("reference_files") or []]
        reference_paths = [str(root / "reference_files" / item) for item in references]
        case_id = str(row_id)
        output.append({
            "input": {
                "case_id": case_id,
                "format": row.get("file_format"),
                "prompt": row.get("question", ""),
                "reference_paths": reference_paths,
                "reference_sha256": [
                    _file_digest(root / "reference_files" / path)
                    if (root / "reference_files" / path).is_file()
                    else None
                    for path in references
                ],
            },
            "expected_output": {
                "gold": row.get("expected_assertions", []),
                "weights": row.get("weights", []),
            },
            "metadata": {
                "benchmark": "ocb",
                "case_id": case_id,
                "source_row": row_id,
                "track": row.get("track"),
                "domain": row.get("domain"),
                "app_type": row.get("app_type"),
                "feature": row.get("feature"),
                "evaluation_source": "langfuse_terra",
                "content_upload_allowed": False,
            },
        })
    _write_jsonl(Path(output_path), output)
    return output


def materialize_officebench(
    source_root: str | Path,
    output_path: str | Path,
    *,
    cases: list[str] | None = None,
) -> list[dict[str, Any]]:
    root = Path(source_root).resolve()
    if cases is None:
        fixed_cases = tuple(
            f"{task_path.parts[-3]}/{task_path.stem}"
            for task_path in sorted(root.glob("tasks/1-*/subtasks/*.json"))
        )
    else:
        fixed_cases = tuple(cases)
    output: list[dict[str, Any]] = []
    for case in fixed_cases:
        task_id, subtask = case.split("/", 1)
        path = root / "tasks" / task_id / "subtasks" / f"{subtask}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        output.append({
            "input": {
                "case_id": case,
                "prompt": payload["task"],
                "source_config": str(path),
                "fixture_sha256": _tree_digest(root / "tasks" / task_id / "testbed"),
                "reference_sha256": _tree_digest(root / "tasks" / task_id / "reference")
                if (root / "tasks" / task_id / "reference").is_dir()
                else None,
            },
            "expected_output": {
                "evaluation": payload.get("evaluation", []),
            },
            "metadata": {
                "benchmark": "officebench",
                "case_id": case,
                "evaluation_source": "officebench_official",
                "content_upload_allowed": True,
            },
        })
    _write_jsonl(Path(output_path), output)
    return output


def materialize_presentbench(
    data_root: str | Path,
    output_path: str | Path,
    *,
    cases: list[str] | None = None,
) -> list[dict[str, Any]]:
    root = Path(data_root).resolve()
    if cases is None:
        fixed_cases = tuple(
            str(path.parent.parent.relative_to(root))
            for path in sorted(root.glob("*/**/generation_task/instructions.md"))
        )
    else:
        fixed_cases = tuple(cases)
    output: list[dict[str, Any]] = []
    for case in fixed_cases:
        case_root = root / case
        instructions_path = case_root / "generation_task" / "instructions.md"
        judge_path = case_root / "generation_task" / "judge_prompt.json"
        instructions = instructions_path.read_text(encoding="utf-8")
        judge_prompt = json.loads(judge_path.read_text(encoding="utf-8"))
        materials = sorted(
            str(path)
            for path in case_root.rglob("*")
            if path.is_file() and path.name not in {"README.md", "instructions.md", "judge_prompt.json"}
        )
        case_id = case.replace("/", "__")
        output.append({
            "input": {
                "case_id": case_id,
                "prompt": instructions,
                "material_paths": materials,
                "prompt_sha256": _digest(instructions),
                "materials_sha256": _digest(
                    [(str(Path(path).relative_to(case_root)), _file_digest(Path(path))) for path in materials]
                ),
            },
            "expected_output": {
                "rubric_sha256": _digest(judge_prompt),
                "rubric": judge_prompt,
            },
            "metadata": {
                "benchmark": "presentbench",
                "case_id": case_id,
                "domain": case.split("/", 1)[0],
                "evaluation_source": "langfuse_terra",
                "visual_score_fallback": "unscored_until_media_spike",
                "content_upload_allowed": False,
            },
        })
    _write_jsonl(Path(output_path), output)
    return output


def evaluate_officebench(source_root: str | Path, case_id: str, output_dir: str | Path) -> bool:
    """Run the pinned upstream OfficeBench evaluator without changing its rules."""
    source = Path(source_root).resolve()
    task_id, subtask_id = case_id.split("/", 1)
    previous_cwd = Path.cwd()
    try:
        os.chdir(source)
        sys.path.insert(0, str(source))
        from evaluation import evaluate_output

        return bool(evaluate_output(task_id, subtask_id, str(Path(output_dir).resolve())))
    finally:
        if sys.path and sys.path[0] == str(source):
            sys.path.pop(0)
        os.chdir(previous_cwd)
