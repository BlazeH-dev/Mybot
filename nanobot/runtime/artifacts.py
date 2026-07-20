"""Immutable input snapshots and task-scoped artifact lineage."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from nanobot.security.workspace_policy import is_path_within


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(slots=True)
class ArtifactRecord:
    artifact_id: str
    task_id: str
    type: str
    path: str
    checksum: str
    size: int
    created_at: str
    skill: str | None = None
    engine: str | None = None
    engine_version: str | None = None
    child_id: str | None = None
    source_artifacts: list[str] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    status: str = "created"
    replayable: bool = True
    source_path: str | None = None
    snapshot_status: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ArtifactRecord":
        return cls(**raw)


class ArtifactError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ArtifactStore:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve(strict=False)
        self.runtime_root = self.workspace / ".nanobot-runtime" / "artifacts"

    def task_root(self, task_id: str) -> Path:
        if not task_id or any(ch in task_id for ch in "/\\\0"):
            raise ArtifactError("invalid_task_id", task_id)
        root = (self.runtime_root / task_id).resolve(strict=False)
        if not is_path_within(root, self.runtime_root):
            raise ArtifactError("artifact_path_escape", str(root))
        return root

    def child_root(self, task_id: str, child_id: str) -> Path:
        if not child_id or any(ch in child_id for ch in "/\\\0"):
            raise ArtifactError("invalid_child_id", child_id)
        return self.task_root(task_id) / "children" / child_id

    def snapshot_input(
        self,
        task_id: str,
        source: str | Path,
        *,
        artifact_id: str | None = None,
    ) -> ArtifactRecord:
        original = Path(source).expanduser().resolve(strict=False)
        try:
            checksum = sha256_file(original)
            size = original.stat().st_size
        except OSError as exc:
            raise ArtifactError("input_unreadable", str(original)) from exc

        inputs = self.task_root(task_id) / "inputs"
        inputs.mkdir(parents=True, exist_ok=True)
        safe_name = original.name or "input"
        target = inputs / safe_name
        if target.exists() and target.resolve(strict=False) != original:
            target = inputs / f"{original.stem}-{checksum[:10]}{original.suffix}"
        snapshot_status = "copied"
        replayable = True
        stored_path = target
        try:
            temp = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            shutil.copy2(original, temp)
            if sha256_file(temp) != checksum:
                temp.unlink(missing_ok=True)
                raise ArtifactError("snapshot_checksum_mismatch", str(original))
            os.replace(temp, target)
        except (OSError, ArtifactError):
            stored_path = original
            snapshot_status = "reference_only"
            replayable = False

        record = ArtifactRecord(
            artifact_id=artifact_id or f"input_{uuid4().hex[:16]}",
            task_id=task_id,
            type=mimetypes.guess_type(original.name)[0] or original.suffix.lstrip(".") or "file",
            path=str(stored_path),
            checksum=checksum,
            size=size,
            created_at=datetime.now().astimezone().isoformat(),
            status="snapshotted" if replayable else "reference_only",
            replayable=replayable,
            source_path=str(original),
            snapshot_status=snapshot_status,
        )
        return self._upsert(record)

    def snapshot_inputs(self, task_id: str, paths: list[str | Path]) -> list[ArtifactRecord]:
        records: list[ArtifactRecord] = []
        seen: set[str] = set()
        for path in paths:
            resolved = str(Path(path).expanduser().resolve(strict=False))
            if resolved in seen:
                continue
            seen.add(resolved)
            records.append(self.snapshot_input(task_id, resolved))
        return records

    def register(
        self,
        *,
        task_id: str,
        path: str | Path,
        type: str | None = None,
        artifact_id: str | None = None,
        skill: str | None = None,
        engine: str | None = None,
        engine_version: str | None = None,
        child_id: str | None = None,
        source_artifacts: list[str] | None = None,
        tool_calls: list[str] | None = None,
        status: str = "created",
        replayable: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRecord:
        resolved = Path(path).expanduser().resolve(strict=False)
        task_root = self.task_root(task_id)
        allowed_roots = (self.workspace, task_root)
        if not any(is_path_within(resolved, root) for root in allowed_roots):
            raise ArtifactError("artifact_path_escape", str(resolved))
        if child_id is not None and not is_path_within(resolved, self.child_root(task_id, child_id)):
            raise ArtifactError("child_artifact_path_escape", str(resolved))
        if not resolved.is_file():
            raise ArtifactError("artifact_missing", str(resolved))
        record = ArtifactRecord(
            artifact_id=artifact_id or f"art_{uuid4().hex[:16]}",
            task_id=task_id,
            type=type or resolved.suffix.lstrip(".") or "file",
            path=str(resolved),
            checksum=sha256_file(resolved),
            size=resolved.stat().st_size,
            created_at=datetime.now().astimezone().isoformat(),
            skill=skill,
            engine=engine,
            engine_version=engine_version,
            child_id=child_id,
            source_artifacts=list(source_artifacts or []),
            tool_calls=list(tool_calls or []),
            status=status,
            replayable=replayable,
            metadata=dict(metadata or {}),
        )
        return self._upsert(record)

    def get(self, task_id: str, artifact_id: str) -> ArtifactRecord:
        for record in self.list(task_id):
            if record.artifact_id == artifact_id:
                return record
        raise ArtifactError("artifact_not_found", artifact_id)

    def list(self, task_id: str) -> list[ArtifactRecord]:
        path = self._index_path(task_id)
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactError("artifact_index_corrupt", str(path)) from exc
        rows = raw.get("artifacts") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            raise ArtifactError("artifact_index_corrupt", str(path))
        return [ArtifactRecord.from_dict(row) for row in rows if isinstance(row, dict)]

    def lineage(self, task_id: str, artifact_id: str) -> dict[str, Any]:
        records = {record.artifact_id: record for record in self.list(task_id)}
        if artifact_id not in records:
            raise ArtifactError("artifact_not_found", artifact_id)

        def visit(current: str, active: set[str]) -> dict[str, Any]:
            if current in active:
                raise ArtifactError("artifact_lineage_cycle", current)
            record = records.get(current)
            if record is None:
                return {"artifact_id": current, "missing": True, "sources": []}
            next_active = {*active, current}
            return {
                **record.as_dict(),
                "sources": [visit(source, next_active) for source in record.source_artifacts],
            }

        return visit(artifact_id, set())

    def verify(self, record: ArtifactRecord) -> bool:
        path = Path(record.path)
        return path.is_file() and sha256_file(path) == record.checksum

    def _upsert(self, record: ArtifactRecord) -> ArtifactRecord:
        rows = self.list(record.task_id)
        for index, existing in enumerate(rows):
            if existing.artifact_id == record.artifact_id:
                rows[index] = record
                break
        else:
            rows.append(record)
        self._write_index(record.task_id, rows)
        return record

    def _index_path(self, task_id: str) -> Path:
        return self.task_root(task_id) / "artifacts.json"

    def _write_index(self, task_id: str, rows: list[ArtifactRecord]) -> None:
        path = self._index_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(
                {"schema_version": 1, "task_id": task_id, "artifacts": [row.as_dict() for row in rows]},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
