"""Materialize immutable, de-identified case manifests for public Office sets."""

from __future__ import annotations

import hashlib
import json
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
