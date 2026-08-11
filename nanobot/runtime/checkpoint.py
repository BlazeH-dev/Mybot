"""Durable checkpoints for activated, plan-hash-bound tasks."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from nanobot.runtime.artifacts import ArtifactStore
from nanobot.runtime.plan_scheduler import PlanScheduler


class CheckpointError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _state_hash(payload: dict[str, Any]) -> str:
    clean = {key: value for key, value in payload.items() if key != "state_hash"}
    raw = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    completed: tuple[str, ...]
    pending: tuple[str, ...]
    uncertain: tuple[str, ...]
    suspension: str | None
    interaction_request_id: str | None
    completed_nodes: tuple[str, ...] = ()
    pending_nodes: tuple[str, ...] = ()
    uncertain_nodes: tuple[str, ...] = ()


class CheckpointStore:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve(strict=False)
        self.artifacts = ArtifactStore(self.workspace)
        self.root = self.workspace / ".nanobot-runtime" / "checkpoints"

    @staticmethod
    def eligible(plan: dict[str, Any] | None) -> bool:
        if not isinstance(plan, dict):
            return False
        plan_hash = plan.get("plan_hash")
        return (
            plan.get("status") in {
                "active",
                "completed",
            }
            and isinstance(plan_hash, str)
            and plan.get("approved_plan_hash") == plan_hash
        )

    def write(
        self,
        *,
        plan: dict[str, Any],
        runner_payload: dict[str, Any],
        session_key: str,
        interactions: list[str] | None = None,
        children: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        if not self.eligible(plan):
            return None
        task_id = str(plan["task_id"])
        completed_results = runner_payload.get("completed_tool_results") or []
        pending_calls = runner_payload.get("pending_tool_calls") or []
        completed = [
            str(item.get("tool_call_id"))
            for item in completed_results
            if isinstance(item, dict) and item.get("tool_call_id")
        ]
        pending: list[str] = []
        uncertain: list[str] = []
        for call in pending_calls:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "")
            name = str((call.get("function") or {}).get("name") or "")
            if name in {"message", "cron"} or name.startswith("mcp_"):
                uncertain.append(call_id)
            else:
                pending.append(call_id)

        node_recovery = PlanScheduler.recovery_summary(plan)
        checkpoint: dict[str, Any] = {
            "schema_version": 2,
            "task_id": task_id,
            "session_key": session_key,
            "plan_hash": plan["plan_hash"],
            "approved_plan_hash": plan["approved_plan_hash"],
            "plan_status": plan["status"],
            "plan_revision": int(plan.get("revision") or 1),
            "created_at": datetime.now().astimezone().isoformat(),
            "runner": runner_payload,
            "completed": completed,
            "pending": pending,
            "uncertain": uncertain,
            "interaction_requests": list(interactions or []),
            "children": list(children or [
                {
                    "node_id": step.get("id"),
                    "child_id": step.get("child_id"),
                    "status": step.get("status"),
                    "executor": step.get("executor"),
                }
                for step in plan.get("steps", [])
                if isinstance(step, dict) and step.get("executor") == "child"
            ]),
            "completed_nodes": list(node_recovery.completed),
            "pending_nodes": list(node_recovery.pending),
            "uncertain_nodes": list(node_recovery.uncertain),
            "artifacts": [record.as_dict() for record in self.artifacts.list(task_id)],
        }
        checkpoint["state_hash"] = _state_hash(checkpoint)
        path = self.path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
        return checkpoint

    def load(self, task_id: str, *, expected_plan_hash: str | None = None) -> dict[str, Any]:
        path = self.path(task_id)
        try:
            checkpoint = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CheckpointError("checkpoint_not_found", task_id) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError("checkpoint_corrupt", str(path)) from exc
        if not isinstance(checkpoint, dict) or checkpoint.get("state_hash") != _state_hash(checkpoint):
            raise CheckpointError("checkpoint_hash_mismatch", str(path))
        if expected_plan_hash and checkpoint.get("plan_hash") != expected_plan_hash:
            raise CheckpointError("plan_hash_mismatch", task_id)
        for raw in checkpoint.get("artifacts", []):
            if not isinstance(raw, dict):
                raise CheckpointError("artifact_reference_corrupt", task_id)
            path_value = raw.get("path")
            checksum = raw.get("checksum")
            if not isinstance(path_value, str) or not isinstance(checksum, str):
                raise CheckpointError("artifact_reference_corrupt", task_id)
            path_obj = Path(path_value)
            if not path_obj.is_file():
                raise CheckpointError("artifact_missing", path_value)
            from nanobot.runtime.artifacts import sha256_file

            if sha256_file(path_obj) != checksum:
                raise CheckpointError("artifact_checksum_mismatch", path_value)
        return checkpoint

    def recovery_plan(self, checkpoint: dict[str, Any]) -> RecoveryPlan:
        runner = checkpoint.get("runner") if isinstance(checkpoint.get("runner"), dict) else {}
        phase = str(runner.get("phase") or "")
        interaction = runner.get("interaction") if isinstance(runner.get("interaction"), dict) else {}
        suspension = phase if phase.startswith("awaiting_") else None
        return RecoveryPlan(
            completed=tuple(str(item) for item in checkpoint.get("completed", [])),
            pending=tuple(str(item) for item in checkpoint.get("pending", [])),
            uncertain=tuple(str(item) for item in checkpoint.get("uncertain", [])),
            suspension=suspension,
            interaction_request_id=(
                str(interaction.get("request_id")) if interaction.get("request_id") else None
            ),
            completed_nodes=tuple(str(item) for item in checkpoint.get("completed_nodes", [])),
            pending_nodes=tuple(str(item) for item in checkpoint.get("pending_nodes", [])),
            uncertain_nodes=tuple(str(item) for item in checkpoint.get("uncertain_nodes", [])),
        )

    def delete(self, task_id: str) -> None:
        self.path(task_id).unlink(missing_ok=True)

    def path(self, task_id: str) -> Path:
        return self.root / f"{task_id}.json"
