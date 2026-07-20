"""Static, cache-friendly plan-mode tool with workspace persistence."""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from nanobot.agent.execution_mode import (
    EXECUTION_MODE_DEFAULT,
    EXECUTION_MODE_PLAN_ONLY,
    execution_mode_from_metadata,
)
from nanobot.agent.tools.base import Tool, ToolSuspensionResult, tool_parameters
from nanobot.agent.tools.context import ContextAware, RequestContext
from nanobot.bus.events import OUTBOUND_META_AGENT_UI, OutboundMessage
from nanobot.runtime.artifacts import ArtifactStore
from nanobot.runtime.interactions import (
    InteractionKind,
    InteractionManager,
    InteractionStatus,
    InteractionStrategy,
)
from nanobot.runtime.trace import emit_trace_event
from nanobot.session.plan_state import PLAN_STATE_KEY, parse_plan_state, plan_state_raw

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_STEP_STATUSES = ("pending", "in_progress", "done", "skipped")
_ACTIONS = ("create", "get", "confirm", "update_step", "complete")

_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 1, "maxLength": 80},
        "description": {"type": "string", "minLength": 1, "maxLength": 2000},
        "expected_artifacts": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
            "maxItems": 100,
        },
        "depends_on": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 80},
            "maxItems": 100,
        },
    },
    "required": ["id", "description"],
    "additionalProperties": False,
}

_PLAN_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(_ACTIONS),
            "description": "Plan operation. Use create before complex work, then confirm, update_step, and complete.",
        },
        "task_id": {
            "type": "string",
            "maxLength": 64,
            "description": "Stable task id. Required except create may omit it to generate one.",
        },
        "goal": {
            "type": "string",
            "maxLength": 8000,
            "description": "Required for create: concise user-visible objective.",
        },
        "constraints": {
            "type": "object",
            "description": "Optional JSON-safe plan constraints for create.",
        },
        "steps": {
            "type": "array",
            "items": _STEP_SCHEMA,
            "minItems": 1,
            "maxItems": 100,
            "description": "Required for create. Status is initialized to pending by the tool.",
        },
        "replace": {
            "type": "boolean",
            "description": "For create only: replace an existing plan and invalidate prior approval.",
        },
        "expected_plan_hash": {
            "type": "string",
            "maxLength": 64,
            "description": "Required for confirm; must equal the hash returned by create/get.",
        },
        "step_id": {
            "type": "string",
            "maxLength": 80,
            "description": "Required for update_step.",
        },
        "status": {
            "type": "string",
            "enum": list(_STEP_STATUSES),
            "description": "Required for update_step.",
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat()


def _canonical_contract(plan: dict[str, Any]) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    for step in plan.get("steps", []):
        if not isinstance(step, dict):
            continue
        steps.append(
            {
                "id": step.get("id"),
                "description": step.get("description"),
                "expected_artifacts": step.get("expected_artifacts", []),
                "depends_on": step.get("depends_on", []),
            }
        )
    return {
        "schema_version": 1,
        "task_id": plan.get("task_id"),
        "goal": plan.get("goal"),
        "constraints": plan.get("constraints", {}),
        "steps": steps,
    }


def _contract_hash(plan: dict[str, Any]) -> str:
    payload = json.dumps(
        _canonical_contract(plan),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@tool_parameters(_PLAN_PARAMETERS)
class PlanTool(Tool, ContextAware):
    """Create and maintain an explicit, persisted plan contract."""

    def __init__(self, workspace: Path, sessions: Any, bus: Any | None = None) -> None:
        self._workspace = workspace.expanduser().resolve()
        self._sessions = sessions
        self._bus = bus
        self._interactions = InteractionManager(self._workspace)
        self._artifacts = ArtifactStore(self._workspace)
        self._request_ctx: ContextVar[RequestContext | None] = ContextVar(
            "plan_tool_request_ctx",
            default=None,
        )

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "sessions", None) is not None

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        sessions = getattr(ctx, "sessions", None)
        assert sessions is not None
        return cls(Path(ctx.workspace), sessions, getattr(ctx, "bus", None))

    def set_context(self, ctx: RequestContext) -> None:
        self._request_ctx.set(ctx)

    @property
    def name(self) -> str:
        return "plan"

    @property
    def description(self) -> str:
        return (
            "Static plan tool for complex tasks (normally at least 3 steps or 2 requested "
            "artifacts). Use create before complex work. In normal WebUI execution mode, create "
            "activates the plan automatically so execution can start immediately; in plan-only "
            "mode it persists an awaiting-confirmation plan and you must stop after presenting "
            "it. Keep progress current with update_step; complete verifies every step and "
            "expected artifact."
        )

    @property
    def exclusive(self) -> bool:
        return True

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        action = params.get("action")
        if action == "create":
            if not str(params.get("goal") or "").strip():
                errors.append("goal is required when action='create'")
            if not isinstance(params.get("steps"), list) or not params.get("steps"):
                errors.append("steps is required when action='create'")
        elif action in {"get", "confirm", "update_step", "complete"}:
            if not str(params.get("task_id") or "").strip():
                errors.append(f"task_id is required when action={action!r}")
        if action == "confirm" and not str(params.get("expected_plan_hash") or "").strip():
            errors.append("expected_plan_hash is required when action='confirm'")
        if action == "update_step":
            if not str(params.get("step_id") or "").strip():
                errors.append("step_id is required when action='update_step'")
            if params.get("status") not in _STEP_STATUSES:
                errors.append("status is required when action='update_step'")
        return errors

    def _session(self):
        ctx = self._request_ctx.get()
        if ctx is None or not ctx.session_key:
            return None
        return self._sessions.get_or_create(ctx.session_key)

    @staticmethod
    def _normalize_task_id(task_id: str | None) -> str:
        candidate = str(task_id or f"task_{uuid4().hex[:10]}").strip()
        if not _TASK_ID_RE.fullmatch(candidate):
            raise ValueError(
                "task_id must start with an alphanumeric character and contain only "
                "letters, digits, '_' or '-' (maximum 64 characters)"
            )
        return candidate

    def _plan_path(self, task_id: str) -> Path:
        path = self._workspace / ".nanobot-runtime" / "artifacts" / task_id / "plan.json"
        try:
            path.parent.resolve().relative_to(self._workspace)
        except ValueError as exc:
            raise ValueError("plan artifact directory resolves outside the workspace") from exc
        return path

    @staticmethod
    def _read_plan(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _write_plan(path: Path, plan: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, path)

    def _save(self, session: Any, path: Path, plan: dict[str, Any]) -> None:
        self._write_plan(path, plan)
        session.metadata[PLAN_STATE_KEY] = plan
        self._sessions.save(session)
        task_id = plan.get("task_id")
        if isinstance(task_id, str) and task_id:
            self._artifacts.register(
                task_id=task_id,
                artifact_id="plan",
                path=path,
                type="plan",
                source_artifacts=list(plan.get("input_artifacts") or []),
                status=str(plan.get("status") or "created"),
            )

    @staticmethod
    def _result(path: Path, plan: dict[str, Any], **extra: Any) -> str:
        return json.dumps(
            {"path": str(path), "plan": plan, **extra},
            ensure_ascii=False,
            indent=2,
        )

    @staticmethod
    def _normalize_steps(steps: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        ids: set[str] = set()
        for raw in steps:
            if not isinstance(raw, dict):
                raise ValueError("each plan step must be an object")
            step_id = str(raw.get("id") or "").strip()
            description = str(raw.get("description") or "").strip()
            if not step_id or not description:
                raise ValueError("each plan step requires non-empty id and description")
            if step_id in ids:
                raise ValueError(f"duplicate plan step id: {step_id}")
            ids.add(step_id)
            expected = [str(item) for item in raw.get("expected_artifacts", [])]
            depends_on = [str(item) for item in raw.get("depends_on", [])]
            normalized.append(
                {
                    "id": step_id,
                    "description": description,
                    "expected_artifacts": expected,
                    "depends_on": depends_on,
                    "status": "pending",
                }
            )
        for step in normalized:
            missing = sorted(set(step["depends_on"]) - ids)
            if missing:
                raise ValueError(f"step {step['id']} depends on unknown step(s): {missing}")
            if step["id"] in step["depends_on"]:
                raise ValueError(f"step {step['id']} cannot depend on itself")
        return normalized

    async def execute(
        self,
        action: str,
        task_id: str | None = None,
        goal: str | None = None,
        constraints: dict[str, Any] | None = None,
        steps: list[dict[str, Any]] | None = None,
        replace: bool = False,
        expected_plan_hash: str | None = None,
        step_id: str | None = None,
        status: str | None = None,
        **kwargs: Any,
    ) -> str:
        session = self._session()
        if session is None:
            return "Error: plan requires an active chat session (missing routing context)."
        try:
            normalized_task_id = self._normalize_task_id(task_id)
        except ValueError as exc:
            return f"Error: {exc}"
        try:
            path = self._plan_path(normalized_task_id)
        except ValueError as exc:
            return f"Error: {exc}"

        if action == "create":
            if path.exists() and not replace:
                return f"Error: plan already exists for {normalized_task_id}; use get or replace=true."
            try:
                normalized_steps = self._normalize_steps(list(steps or []))
            except ValueError as exc:
                return f"Error: {exc}"
            now = _iso_now()
            ctx = self._request_ctx.get()
            execution_mode = execution_mode_from_metadata(ctx.metadata if ctx else None)
            auto_activate = execution_mode == EXECUTION_MODE_DEFAULT
            confirmation_request = None
            plan: dict[str, Any] = {
                "schema_version": 1,
                "task_id": normalized_task_id,
                "goal": str(goal or "").strip(),
                "constraints": constraints or {},
                "steps": normalized_steps,
                "status": "active" if auto_activate else "awaiting_confirmation",
                "approved_plan_hash": None,
                "approval": None,
                "created_at": now,
                "updated_at": now,
            }
            plan["plan_hash"] = _contract_hash(plan)
            if auto_activate:
                plan["approved_plan_hash"] = plan["plan_hash"]
                plan["approval"] = {
                    "confirmed_at": now,
                    "message_id": ctx.message_id if ctx else None,
                    "mode": "automatic",
                }
            elif ctx is not None and ctx.channel == "websocket" and self._bus is not None:
                request = self._interactions.create(
                    kind=InteractionKind.PLAN_CONFIRMATION,
                    strategy=InteractionStrategy.REQUIRED,
                    task_id=normalized_task_id,
                    turn_id=ctx.message_id,
                    plan_hash=plan["plan_hash"],
                    continuation={"tool_name": "plan", "action": "confirm"},
                    payload={
                        "chat_id": ctx.chat_id,
                        "goal": plan["goal"],
                        "plan_hash": plan["plan_hash"],
                    },
                )
                confirmation_request = request
                plan["interaction_request_id"] = request.request_id
                await self._bus.publish_outbound(OutboundMessage(
                    channel=ctx.channel,
                    chat_id=ctx.chat_id,
                    content="Plan confirmation is required.",
                    metadata={
                        "_progress": True,
                        OUTBOUND_META_AGENT_UI: {
                            "kind": "interaction_request",
                            "interaction": request.as_dict(),
                        },
                    },
                ))
            input_paths = (
                ctx.metadata.get("_runtime_input_paths", [])
                if ctx is not None and isinstance(ctx.metadata, dict)
                else []
            )
            if isinstance(input_paths, list) and input_paths:
                snapshots = self._artifacts.snapshot_inputs(
                    normalized_task_id,
                    [path for path in input_paths if isinstance(path, str)],
                )
                plan["input_artifacts"] = [item.artifact_id for item in snapshots]
            self._save(session, path, plan)
            emit_trace_event("mybot.plan.created", {
                "task_id": normalized_task_id,
                "plan_hash": plan["plan_hash"],
                "activation_mode": "automatic" if auto_activate else "explicit",
                "execution_mode": execution_mode,
            })
            result = self._result(
                path,
                plan,
                next_action=(
                    "Begin execution and keep step status current."
                    if auto_activate
                    else (
                        "Show this plan to the user and wait. After explicit confirmation, call "
                        "plan(action='confirm', task_id=..., expected_plan_hash=plan_hash)."
                    )
                ),
            )
            if confirmation_request is not None:
                return ToolSuspensionResult(
                    result,
                    stop_reason="awaiting_plan_confirmation",
                    payload={
                        "status": "awaiting_plan_confirmation",
                        "reason": "the plan requires explicit typed confirmation",
                        "interaction": confirmation_request.as_dict(),
                    },
                )
            return result

        plan = self._read_plan(path)
        if plan is None:
            session_plan = parse_plan_state(plan_state_raw(session.metadata))
            if isinstance(session_plan, dict) and session_plan.get("task_id") == normalized_task_id:
                plan = session_plan
            else:
                return f"Error: plan not found for task_id={normalized_task_id}"

        if action == "get":
            session.metadata[PLAN_STATE_KEY] = plan
            self._sessions.save(session)
            return self._result(path, plan)

        if action == "confirm":
            ctx = self._request_ctx.get()
            if (
                execution_mode_from_metadata(ctx.metadata if ctx else None)
                == EXECUTION_MODE_PLAN_ONLY
            ):
                return "Error: plan-only mode cannot confirm or execute a plan in the same turn."
            actual_hash = str(plan.get("plan_hash") or "")
            if expected_plan_hash != actual_hash:
                return (
                    "Error: plan hash mismatch; the plan changed or the confirmation is stale. "
                    f"Expected current hash {actual_hash}. Show the current plan and ask again."
                )
            request_id = plan.get("interaction_request_id")
            if isinstance(request_id, str):
                try:
                    request = self._interactions.get(request_id)
                except ValueError:
                    return "Error: plan confirmation interaction is missing or corrupt."
                if not (
                    request.status == InteractionStatus.ANSWERED
                    and request.response
                    and request.response.get("approved") is True
                ):
                    return "Error: plan still requires an explicit typed confirmation."
                self._interactions.consume(
                    request_id,
                    expected_revision=request.revision,
                    idempotency_key=f"plan-confirm:{actual_hash}",
                )
            plan["status"] = "active"
            plan["approved_plan_hash"] = actual_hash
            plan["approval"] = {
                "confirmed_at": _iso_now(),
                "message_id": ctx.message_id if ctx else None,
            }
            plan["updated_at"] = _iso_now()
            self._save(session, path, plan)
            emit_trace_event("mybot.plan.confirmed", {
                "task_id": normalized_task_id,
                "plan_hash": actual_hash,
                "activation_mode": "explicit",
            })
            return self._result(path, plan, next_action="Begin execution and keep step status current.")

        if plan.get("status") != "active":
            return "Error: plan is not active; wait for explicit user confirmation first."

        if action == "update_step":
            target = next(
                (item for item in plan.get("steps", []) if item.get("id") == step_id),
                None,
            )
            if not isinstance(target, dict):
                return f"Error: unknown plan step: {step_id}"
            by_id = {
                item.get("id"): item
                for item in plan.get("steps", [])
                if isinstance(item, dict)
            }
            if status in {"in_progress", "done"}:
                incomplete = [
                    dependency
                    for dependency in target.get("depends_on", [])
                    if by_id.get(dependency, {}).get("status") not in {"done", "skipped"}
                ]
                if incomplete:
                    return f"Error: step {step_id} has incomplete dependencies: {incomplete}"
            target["status"] = status
            plan["updated_at"] = _iso_now()
            self._save(session, path, plan)
            return self._result(path, plan)

        if action == "complete":
            incomplete_steps = [
                item.get("id")
                for item in plan.get("steps", [])
                if item.get("status") not in {"done", "skipped"}
            ]
            if incomplete_steps:
                return f"Error: plan has incomplete steps: {incomplete_steps}"
            missing_artifacts: list[str] = []
            for item in plan.get("steps", []):
                for artifact in item.get("expected_artifacts", []):
                    candidate = (path.parent / str(artifact)).resolve()
                    try:
                        candidate.relative_to(path.parent.resolve())
                    except ValueError:
                        missing_artifacts.append(str(artifact))
                        continue
                    if not candidate.exists():
                        missing_artifacts.append(str(artifact))
            if missing_artifacts:
                return f"Error: planned artifacts are missing or unsafe: {sorted(set(missing_artifacts))}"
            plan["status"] = "completed"
            plan["completed_at"] = _iso_now()
            plan["updated_at"] = _iso_now()
            self._save(session, path, plan)
            return self._result(path, plan, verification={"passed": True, "missing": []})

        return f"Error: unsupported plan action: {action}"
