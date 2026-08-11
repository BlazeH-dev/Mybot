"""Static, cache-friendly plan-mode tool with workspace persistence."""

from __future__ import annotations

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
from nanobot.runtime.artifacts import ArtifactStore, sha256_file
from nanobot.runtime.checkpoint import CheckpointStore
from nanobot.runtime.interactions import (
    InteractionKind,
    InteractionManager,
    InteractionStatus,
    InteractionStrategy,
)
from nanobot.runtime.plan_scheduler import (
    NODE_STATUSES,
    PlanGraphError,
    PlanScheduler,
    contract_hash,
    render_plan_markdown,
)
from nanobot.runtime.trace import emit_trace_event
from nanobot.session.plan_state import PLAN_STATE_KEY, parse_plan_state, plan_state_raw

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_STEP_STATUSES = (*NODE_STATUSES, "in_progress", "done", "skipped")
_ACTIONS = ("create", "get", "revise", "confirm", "resume", "update_step", "complete")

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
        "executor": {
            "type": "string",
            "enum": ["parent", "child"],
            "description": "Node executor. Parent runs in the main agent; child uses a subagent.",
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
            "description": (
                "Plan operation. Use create before complex work, confirm before execution, "
                "resume after an explicit user request to retry failed nodes, update_step while "
                "executing, and complete only after verification."
            ),
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
        "revision_reason": {
            "type": "string",
            "maxLength": 2000,
            "description": "For revise: why the active contract needs to change.",
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
        "result_summary": {
            "type": "string",
            "maxLength": 8000,
            "description": "Optional concise result/evidence for a terminal node transition.",
        },
        "error": {
            "type": "string",
            "maxLength": 8000,
            "description": "Optional failure detail for a failed or uncertain node.",
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat()


@tool_parameters(_PLAN_PARAMETERS)
class PlanTool(Tool, ContextAware):
    """Create and maintain an explicit, persisted plan contract."""

    def __init__(
        self,
        workspace: Path,
        sessions: Any,
        bus: Any | None = None,
        *,
        subagent_manager: Any | None = None,
    ) -> None:
        self._workspace = workspace.expanduser().resolve()
        self._sessions = sessions
        self._bus = bus
        self._interactions = InteractionManager(self._workspace)
        self._artifacts = ArtifactStore(self._workspace)
        self._checkpoints = CheckpointStore(self._workspace)
        self._subagents = subagent_manager
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
        return cls(
            Path(ctx.workspace),
            sessions,
            getattr(ctx, "bus", None),
            subagent_manager=getattr(ctx, "subagent_manager", None),
        )

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
            "it. DAG dependencies determine ready work; child nodes are dispatched through the "
            "governed subagent runtime. Use revise for contract changes. complete verifies every "
            "step and planned artifact before delivery. "
            "After the user explicitly asks to continue an interrupted or failed active plan, use "
            "resume with the current plan hash instead of guessing state transitions."
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
        elif action in {"get", "revise", "confirm", "resume", "update_step", "complete"}:
            if not str(params.get("task_id") or "").strip():
                errors.append(f"task_id is required when action={action!r}")
        if action in {"confirm", "resume"} and not str(
            params.get("expected_plan_hash") or ""
        ).strip():
            errors.append(f"expected_plan_hash is required when action={action!r}")
        if action == "revise":
            if not str(params.get("expected_plan_hash") or "").strip():
                errors.append("expected_plan_hash is required when action='revise'")
            if not isinstance(params.get("steps"), list) or not params.get("steps"):
                errors.append("steps is required when action='revise'")
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

    def _plan_markdown_path(self, plan: dict[str, Any]) -> Path:
        task_id = str(plan.get("task_id") or "")
        path = self._workspace / ".nanobot-runtime" / "artifacts" / task_id / "plan.md"
        try:
            path.parent.resolve().relative_to(self._workspace)
        except ValueError as exc:
            raise ValueError("plan Markdown directory resolves outside the workspace") from exc
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

    def _write_plan_markdown(self, plan: dict[str, Any]) -> dict[str, Any]:
        path = self._plan_markdown_path(plan)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = render_plan_markdown(plan)
        temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)
        record = self._artifacts.register(
            task_id=str(plan["task_id"]),
            artifact_id="plan_markdown",
            path=path,
            type="plan_markdown",
            source_artifacts=["plan"],
            status="generated",
            metadata={
                "revision": int(plan.get("revision") or 1),
                "plan_hash": str(plan.get("plan_hash") or ""),
            },
        )
        metadata = {
            "artifact_id": record.artifact_id,
            "path": str(path),
            "revision": int(plan.get("revision") or 1),
            "plan_hash": str(plan.get("plan_hash") or ""),
            "checksum": record.checksum,
        }
        plan["plan_markdown"] = metadata
        if not self._artifacts.verify(record):
            raise PlanGraphError(
                "plan_markdown_checksum_mismatch",
                f"plan Markdown checksum failed after registration: {path}",
            )
        self._prune_legacy_plan_markdown(str(plan["task_id"]))
        return metadata

    def _prune_legacy_plan_markdown(self, task_id: str) -> None:
        legacy_records = {
            record.artifact_id
            for record in self._artifacts.list(task_id)
            if re.fullmatch(r"plan_md_r\d+", record.artifact_id)
        }
        self._artifacts.remove(task_id, legacy_records)
        task_root = self._artifacts.task_root(task_id)
        for candidate in task_root.iterdir() if task_root.is_dir() else ():
            if candidate.is_file() and re.fullmatch(r"plan-r\d+\.md", candidate.name):
                try:
                    candidate.unlink()
                except OSError as exc:
                    raise PlanGraphError(
                        "legacy_plan_markdown_cleanup_failed",
                        f"cannot remove legacy plan Markdown: {candidate}",
                    ) from exc

    def _is_legacy_plan_markdown_binding(
        self,
        plan: dict[str, Any],
        current: dict[str, Any],
    ) -> bool:
        revision = int(plan.get("revision") or 1)
        path = current.get("path")
        if not isinstance(path, str) or not Path(path).is_absolute():
            return False
        expected = self._plan_markdown_path(plan).with_name(f"plan-r{revision}.md")
        return (
            current.get("artifact_id") == f"plan_md_r{revision}"
            and current.get("revision") == revision
            and current.get("plan_hash") == plan.get("plan_hash")
            and Path(path).resolve(strict=False) == expected.resolve(strict=False)
        )

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
            runner_payload = session.metadata.get("runtime_checkpoint")
            if not isinstance(runner_payload, dict):
                runner_payload = {
                    "phase": "plan_runtime",
                    "assistant_message": None,
                    "completed_tool_results": [],
                    "pending_tool_calls": [],
                }
            self._checkpoints.write(
                plan=plan,
                runner_payload=runner_payload,
                session_key=str(getattr(session, "key", "") or "unknown"),
                interactions=[
                    request.request_id
                    for request in self._interactions.list_pending(task_id=task_id)
                ],
            )

    def _migrate_legacy_reflection_plan(
        self,
        session: Any,
        path: Path,
        plan: dict[str, Any],
    ) -> bool:
        if plan.get("status") not in {"reviewing", "awaiting_reflection_decision"}:
            return False
        for request in self._interactions.list_pending(task_id=str(plan.get("task_id") or "")):
            if request.kind != InteractionKind.REFLECTION_DECISION:
                continue
            self._interactions.cancel(
                request.request_id,
                expected_revision=request.revision,
                idempotency_key=f"reflection-removed:{request.request_id}",
            )
        now = _iso_now()
        plan["status"] = "completed"
        plan["completed_at"] = str(plan.get("completed_at") or now)
        plan["updated_at"] = now
        for key in (
            "interaction_request_id",
            "reflection",
            "reflection_attempts",
            "reflection_findings",
        ):
            plan.pop(key, None)
        self._save(session, path, plan)
        return True

    def _ensure_plan_markdown(self, plan: dict[str, Any], *, allow_create: bool = False) -> bool:
        current = plan.get("plan_markdown")
        if isinstance(current, dict):
            markdown_path = current.get("path")
            expected_path = self._plan_markdown_path(plan)
            if self._is_legacy_plan_markdown_binding(plan, current):
                legacy_path = Path(str(markdown_path))
                checksum = str(current.get("checksum") or "")
                if (
                    not legacy_path.is_file()
                    or not checksum
                    or sha256_file(legacy_path) != checksum
                    or legacy_path.read_text(encoding="utf-8") != render_plan_markdown(plan)
                ):
                    raise PlanGraphError(
                        "plan_markdown_checksum_mismatch",
                        f"legacy plan Markdown failed migration validation: {legacy_path}",
                    )
                self._write_plan_markdown(plan)
                return True
            if not isinstance(markdown_path, str) or Path(markdown_path).resolve() != expected_path.resolve():
                raise PlanGraphError(
                    "plan_markdown_binding_mismatch",
                    "plan Markdown path is not bound to the active task",
                )
            if current.get("artifact_id") != "plan_markdown":
                raise PlanGraphError("plan_markdown_binding_mismatch", "plan Markdown artifact id is stale")
            if current.get("revision") != int(plan.get("revision") or 1):
                raise PlanGraphError("plan_markdown_binding_mismatch", "plan Markdown revision is stale")
            if current.get("plan_hash") != plan.get("plan_hash"):
                raise PlanGraphError("plan_markdown_binding_mismatch", "plan Markdown hash is stale")
            if not expected_path.is_file():
                raise PlanGraphError("plan_markdown_missing", str(expected_path))
            checksum = str(current.get("checksum") or "")
            if not checksum or sha256_file(expected_path) != checksum:
                raise PlanGraphError(
                    "plan_markdown_checksum_mismatch",
                    f"plan Markdown checksum failed: {expected_path}",
                )
            if expected_path.read_text(encoding="utf-8") != render_plan_markdown(plan):
                raise PlanGraphError(
                    "plan_markdown_content_mismatch",
                    f"plan Markdown does not match the structured plan: {expected_path}",
                )
            return False
        if not allow_create:
            raise PlanGraphError(
                "plan_markdown_missing",
                "schema v2 plan is missing its Markdown artifact binding",
            )
        self._write_plan_markdown(plan)
        return True

    @staticmethod
    def _verify_plan_hash(plan: dict[str, Any]) -> None:
        actual = str(plan.get("plan_hash") or "")
        expected = contract_hash(plan)
        if not actual or actual != expected:
            raise PlanGraphError(
                "plan_hash_mismatch",
                "stored plan contract does not match its plan_hash",
            )

    @staticmethod
    def _result(path: Path, plan: dict[str, Any], **extra: Any) -> str:
        payload: dict[str, Any] = {"path": str(path), "plan": plan, **extra}
        if isinstance(plan.get("plan_markdown"), dict):
            payload["plan_markdown"] = plan["plan_markdown"]
        return json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        )

    async def _request_plan_confirmation(
        self,
        plan: dict[str, Any],
        *,
        ctx: RequestContext | None,
    ) -> Any | None:
        if not (ctx is not None and ctx.channel == "websocket" and self._bus is not None):
            return None
        existing_id = plan.get("interaction_request_id")
        if isinstance(existing_id, str):
            try:
                existing = self._interactions.get(existing_id)
            except Exception:
                existing = None
            if existing is not None and existing.status == InteractionStatus.PENDING:
                return existing
        request = self._interactions.create(
            kind=InteractionKind.PLAN_CONFIRMATION,
            strategy=InteractionStrategy.REQUIRED,
            task_id=str(plan["task_id"]),
            turn_id=ctx.message_id,
            plan_hash=str(plan["plan_hash"]),
            continuation={"tool_name": "plan", "action": "confirm"},
            payload={
                "chat_id": ctx.chat_id,
                "goal": str(plan.get("goal") or ""),
                "plan_hash": str(plan["plan_hash"]),
                "revision": int(plan.get("revision") or 1),
                "plan_markdown": plan.get("plan_markdown"),
            },
        )
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
        return request

    async def _dispatch_ready_children(
        self,
        session: Any,
        path: Path,
        plan: dict[str, Any],
    ) -> list[str]:
        if self._subagents is None or plan.get("status") != "active":
            return []
        ctx = self._request_ctx.get()
        session_key = ctx.session_key if ctx is not None else getattr(session, "key", None)
        available = max(
            0,
            int(self._subagents.max_concurrent_subagents)
            - int(self._subagents.get_running_count()),
        )
        if available <= 0:
            return []
        ready = PlanScheduler.ready_steps(plan, executor="child")[:available]
        started: list[str] = []
        for step in ready:
            node_id = str(step["id"])
            captured_hash = str(plan["plan_hash"])
            captured_task_id = str(plan["task_id"])

            async def _completed(
                status_obj: Any,
                *,
                expected_node: str = node_id,
                expected_hash: str = captured_hash,
                expected_task: str = captured_task_id,
            ) -> None:
                callback_session = self._sessions.get_or_create(session_key) if session_key else session
                current = self._read_plan(path)
                if not isinstance(current, dict) or current.get("plan_hash") != expected_hash:
                    emit_trace_event("mybot.plan.node.stale_completion", {
                        "task_id": expected_task,
                        "node_id": expected_node,
                        "child_id": getattr(status_obj, "task_id", None),
                    })
                    return
                target = next(
                    (
                        item
                        for item in current.get("steps", [])
                        if isinstance(item, dict) and item.get("id") == expected_node
                    ),
                    None,
                )
                if not isinstance(target, dict):
                    return
                child_id = str(getattr(status_obj, "task_id", "") or "")
                artifact_root = getattr(status_obj, "artifact_root", None)
                if isinstance(artifact_root, str):
                    target["artifact_root"] = artifact_root
                    root = Path(artifact_root)
                    if root.is_dir():
                        for index, artifact_path in enumerate(
                            sorted(item for item in root.rglob("*") if item.is_file())
                        ):
                            try:
                                self._artifacts.register(
                                    task_id=expected_task,
                                    artifact_id=(
                                        f"child_{expected_node}_{index}_{sha256_file(artifact_path)[:8]}"
                                    ),
                                    path=artifact_path,
                                    child_id=child_id,
                                    source_artifacts=list(current.get("input_artifacts") or []),
                                    status="created",
                                    metadata={"node_id": expected_node},
                                )
                            except Exception:
                                emit_trace_event("mybot.plan.child_artifact_registration_failed", {
                                    "task_id": expected_task,
                                    "node_id": expected_node,
                                    "path": str(artifact_path),
                                })
                phase = str(getattr(status_obj, "phase", "error") or "error")
                stop_reason = str(getattr(status_obj, "stop_reason", "") or "")
                result_text = getattr(status_obj, "final_result", None)
                interrupted = stop_reason == "cancelled"
                terminal = (
                    "ready"
                    if interrupted
                    else (
                        "succeeded"
                        if phase == "done" and stop_reason not in {
                            "error", "tool_error", "max_iterations", "budget_exceeded"
                        }
                        else "failed"
                    )
                )
                try:
                    PlanScheduler.transition(
                        current,
                        expected_node,
                        terminal,
                        child_id=None if interrupted else child_id,
                        result=None if interrupted else result_text,
                        error=(
                            None
                            if terminal in {"ready", "succeeded"}
                            else result_text or stop_reason
                        ),
                    )
                except PlanGraphError:
                    return
                if interrupted:
                    target["interrupted_at"] = _iso_now()
                    target["interruption_reason"] = "child_cancelled"
                current["updated_at"] = _iso_now()
                self._save(callback_session, path, current)
                emit_trace_event(
                    "mybot.plan.node.interrupted" if interrupted else "mybot.plan.node.completed",
                    {
                        "task_id": expected_task,
                        "node_id": expected_node,
                        "child_id": child_id,
                        "status": terminal,
                    },
                )
                if not interrupted:
                    await self._dispatch_ready_children(callback_session, path, current)

            task_text = json.dumps({
                "goal": plan.get("goal"),
                "constraints": plan.get("constraints", {}),
                "node": {
                    "id": node_id,
                    "description": step.get("description"),
                    "depends_on": step.get("depends_on", []),
                    "expected_artifacts": step.get("expected_artifacts", []),
                },
                "delivery": (
                    "Work only on this node. Write outputs inside the provided child workspace. "
                    "Return a concise result with artifact filenames and validation evidence."
                ),
            }, ensure_ascii=False, indent=2)
            try:
                PlanScheduler.transition(plan, node_id, "running")
                plan["updated_at"] = _iso_now()
                self._save(session, path, plan)
                child_id = await self._subagents.spawn(
                    task=task_text,
                    label=f"Plan node: {node_id}",
                    origin_channel=ctx.channel if ctx is not None else "websocket",
                    origin_chat_id=ctx.chat_id if ctx is not None else "direct",
                    session_key=session_key,
                    origin_message_id=ctx.message_id if ctx is not None else None,
                    parent_task_id=captured_task_id,
                    parent_plan_hash=captured_hash,
                    node_id=node_id,
                    completion_callback=_completed,
                    return_task_id=True,
                )
                if not re.fullmatch(r"[0-9a-f]{8}", child_id):
                    raise RuntimeError(child_id)
                step["child_id"] = child_id
                started.append(node_id)
                self._save(session, path, plan)
                emit_trace_event("mybot.plan.node.dispatched", {
                    "task_id": captured_task_id,
                    "node_id": node_id,
                    "child_id": child_id,
                })
            except Exception as exc:
                try:
                    PlanScheduler.transition(plan, node_id, "failed", error=str(exc))
                except PlanGraphError:
                    pass
                self._save(session, path, plan)
        return started

    @staticmethod
    def _normalize_steps(steps: list[Any]) -> list[dict[str, Any]]:
        return PlanScheduler.normalize_steps(steps)

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
        revision_reason: str | None = None,
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
            previous = self._read_plan(path) if path.exists() else None
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
                "schema_version": 2,
                "task_id": normalized_task_id,
                "revision": int((previous or {}).get("revision") or 0) + 1,
                "goal": str(goal or "").strip(),
                "constraints": constraints or {},
                "steps": normalized_steps,
                "status": "active" if auto_activate else "awaiting_confirmation",
                "approved_plan_hash": None,
                "approval": None,
                "created_at": now,
                "updated_at": now,
            }
            if previous is not None:
                plan["previous_plan_hash"] = previous.get("plan_hash")
                plan["revision_reason"] = "Plan replaced by an explicit create(replace=true) call."
            plan = PlanScheduler.refresh(plan)
            plan["plan_hash"] = contract_hash(plan)
            if auto_activate:
                plan["approved_plan_hash"] = plan["plan_hash"]
                plan["approval"] = {
                    "confirmed_at": now,
                    "message_id": ctx.message_id if ctx else None,
                    "mode": "automatic",
                }
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
            self._write_plan_markdown(plan)
            self._save(session, path, plan)
            if not auto_activate:
                confirmation_request = await self._request_plan_confirmation(plan, ctx=ctx)
                self._save(session, path, plan)
            dispatched = await self._dispatch_ready_children(session, path, plan) if auto_activate else []
            emit_trace_event("mybot.plan.created", {
                "task_id": normalized_task_id,
                "plan_hash": plan["plan_hash"],
                "activation_mode": "automatic" if auto_activate else "explicit",
                "execution_mode": execution_mode,
            })
            result = self._result(
                path,
                plan,
                dispatched_steps=dispatched,
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

        self._migrate_legacy_reflection_plan(session, path, plan)

        if int(plan.get("schema_version") or 1) < 2:
            plan["schema_version"] = 2
            plan["revision"] = int(plan.get("revision") or 1)
            try:
                plan["steps"] = PlanScheduler.normalize_steps(plan.get("steps") or [])
                plan["plan_hash"] = contract_hash(plan)
            except PlanGraphError as exc:
                return f"Error: stored plan is invalid ({exc.code}): {exc.message}"
            self._ensure_plan_markdown(plan, allow_create=True)
            self._save(session, path, plan)

        try:
            self._verify_plan_hash(plan)
            markdown_migrated = self._ensure_plan_markdown(plan)
        except (OSError, PlanGraphError) as exc:
            code = getattr(exc, "code", "plan_integrity_error")
            message = getattr(exc, "message", str(exc))
            return f"Error: stored plan integrity check failed ({code}): {message}"
        if markdown_migrated:
            self._save(session, path, plan)

        if action == "get":
            PlanScheduler.refresh(plan)
            session.metadata[PLAN_STATE_KEY] = plan
            self._sessions.save(session)
            dispatched = await self._dispatch_ready_children(session, path, plan)
            return self._result(path, plan, dispatched_steps=dispatched)

        if action == "revise":
            actual_hash = str(plan.get("plan_hash") or "")
            if expected_plan_hash != actual_hash:
                return (
                    "Error: plan hash mismatch; the plan changed or the revision is stale. "
                    f"Expected current hash {actual_hash}."
                )
            if plan.get("status") not in {
                "awaiting_confirmation",
                "awaiting_revision_confirmation",
                "active",
            }:
                return "Error: only an unconfirmed or active plan can be revised."
            try:
                plan = PlanScheduler.revise(
                    plan,
                    steps=list(steps or []),
                    goal=goal,
                    constraints=constraints,
                    reason=revision_reason,
                )
            except PlanGraphError as exc:
                return f"Error: cannot revise plan ({exc.code}): {exc.message}"
            self._save(session, path, plan)
            self._write_plan_markdown(plan)
            self._save(session, path, plan)
            request_result = await self._request_plan_confirmation(plan, ctx=self._request_ctx.get())
            self._save(session, path, plan)
            result = self._result(
                path,
                plan,
                next_action="Confirm this revision before resuming execution.",
            )
            if request_result is not None:
                return ToolSuspensionResult(
                    result,
                    stop_reason="awaiting_plan_confirmation",
                    payload={
                        "status": "awaiting_plan_confirmation",
                        "reason": "the revised plan requires explicit typed confirmation",
                        "interaction": request_result.as_dict(),
                    },
                )
            return result

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
            plan.pop("interaction_request_id", None)
            PlanScheduler.refresh(plan)
            plan["updated_at"] = _iso_now()
            self._save(session, path, plan)
            emit_trace_event("mybot.plan.confirmed", {
                "task_id": normalized_task_id,
                "plan_hash": actual_hash,
                "activation_mode": "explicit",
            })
            ready = [str(step.get("id")) for step in PlanScheduler.ready_steps(plan)]
            dispatched = await self._dispatch_ready_children(session, path, plan)
            ready = [str(step.get("id")) for step in PlanScheduler.ready_steps(plan)]
            return self._result(
                path,
                plan,
                next_action="Execute the ready DAG nodes and keep their state current.",
                ready_steps=ready,
                dispatched_steps=dispatched,
            )

        if action == "resume":
            actual_hash = str(plan.get("plan_hash") or "")
            if expected_plan_hash != actual_hash:
                return (
                    "Error: plan hash mismatch; the plan changed or the resume request is stale. "
                    f"Expected current hash {actual_hash}."
                )
            if plan.get("status") != "active":
                return "Error: only an active confirmed plan can resume execution."
            before_recovery = {
                str(step.get("id")): str(step.get("status") or "pending")
                for step in plan.get("steps", [])
                if isinstance(step, dict)
            }
            PlanScheduler.recover_running(
                plan,
                live_child_ids=(
                    self._subagents.get_running_task_ids()
                    if self._subagents is not None
                    else set()
                ),
            )
            recovered = [
                str(step.get("id"))
                for step in plan.get("steps", [])
                if isinstance(step, dict)
                and before_recovery.get(str(step.get("id"))) != str(step.get("status"))
            ]
            retried = PlanScheduler.retry_failed(plan)
            plan["updated_at"] = _iso_now()
            self._save(session, path, plan)
            dispatched = await self._dispatch_ready_children(session, path, plan)
            ready = [str(step.get("id")) for step in PlanScheduler.ready_steps(plan)]
            return self._result(
                path,
                plan,
                next_action=(
                    "Continue the ready parent nodes and keep their state current. Child nodes "
                    "listed in dispatched_steps are already running."
                ),
                retried_steps=retried,
                recovered_steps=recovered,
                ready_steps=ready,
                dispatched_steps=dispatched,
            )

        if plan.get("status") != "active":
            return "Error: plan is not active; wait for explicit user confirmation first."

        if action == "update_step":
            if plan.get("status") != "active":
                return "Error: plan step state can only change while the confirmed plan is active."
            target = next((
                item
                for item in plan.get("steps", [])
                if isinstance(item, dict) and str(item.get("id")) == str(step_id)
            ), None)
            if (
                isinstance(target, dict)
                and status in {"running", "in_progress"}
                and target.get("status") == "pending"
            ):
                by_id = {
                    str(item.get("id")): item
                    for item in plan.get("steps", [])
                    if isinstance(item, dict)
                }
                incomplete = [
                    dependency
                    for dependency in target.get("depends_on", [])
                    if by_id.get(str(dependency), {}).get("status")
                    not in {"succeeded", "cancelled"}
                ]
                if incomplete:
                    return f"Error: incomplete dependencies for {step_id}: {incomplete}"
            requested_status = str(status)
            if (
                isinstance(target, dict)
                and target.get("status") == "failed"
                and requested_status in {"running", "in_progress"}
            ):
                PlanScheduler.retry_failed(plan, [str(step_id)])
                if target.get("executor") == "child":
                    requested_status = "ready"
            if (
                isinstance(target, dict)
                and target.get("executor") == "child"
                and requested_status in {"running", "in_progress"}
                and not (
                    target.get("status") == "running"
                    and isinstance(target.get("child_id"), str)
                    and target.get("child_id")
                )
            ):
                requested_status = "ready"
            try:
                PlanScheduler.transition(
                    plan,
                    str(step_id),
                    requested_status,
                    result=(
                        None if requested_status == "ready" else kwargs.get("result_summary")
                    ),
                    error=None if requested_status == "ready" else kwargs.get("error"),
                )
            except PlanGraphError as exc:
                return f"Error: cannot update plan step ({exc.code}): {exc.message}"
            plan["updated_at"] = _iso_now()
            self._save(session, path, plan)
            dispatched = await self._dispatch_ready_children(session, path, plan)
            ready = [str(step.get("id")) for step in PlanScheduler.ready_steps(plan)]
            return self._result(path, plan, ready_steps=ready, dispatched_steps=dispatched)

        if action == "complete":
            PlanScheduler.refresh(plan)
            incomplete_steps = [
                item.get("id")
                for item in plan.get("steps", [])
                if item.get("status") not in {"succeeded", "cancelled"}
            ]
            if incomplete_steps:
                return f"Error: plan has incomplete steps: {incomplete_steps}"
            missing_artifacts: list[str] = []
            for item in plan.get("steps", []):
                for artifact in item.get("expected_artifacts", []):
                    roots = [path.parent]
                    if isinstance(item.get("artifact_root"), str):
                        roots.insert(0, Path(item["artifact_root"]))
                    found = False
                    for root in roots:
                        candidate = (root / str(artifact)).resolve()
                        try:
                            candidate.relative_to(root.resolve())
                        except ValueError:
                            continue
                        if candidate.exists():
                            found = True
                            break
                    if not found:
                        missing_artifacts.append(str(artifact))
            if missing_artifacts:
                return f"Error: planned artifacts are missing or unsafe: {sorted(set(missing_artifacts))}"

            plan["status"] = "completed"
            plan["completed_at"] = _iso_now()
            plan["updated_at"] = _iso_now()
            self._save(session, path, plan)
            return self._result(
                path,
                plan,
                verification={"passed": True, "missing": []},
            )

        return f"Error: unsupported plan action: {action}"
