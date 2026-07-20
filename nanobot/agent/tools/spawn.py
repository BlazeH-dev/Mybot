"""Spawn tool for creating background subagents."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import ContextAware, RequestContext
from nanobot.agent.tools.schema import (
    NumberSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.security.workspace_access import current_workspace_scope

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager


@tool_parameters(
    tool_parameters_schema(
        task=StringSchema("The task for the subagent to complete"),
        label=StringSchema("Optional short label for the task (for display)"),
        temperature=NumberSchema(
            description=(
                "Optional sampling temperature for the subagent "
                "(0.0 = deterministic, higher = more creative). "
                "Defaults to the provider's configured temperature."
            ),
            minimum=0.0,
            maximum=2.0,
        ),
        required=["task"],
    )
)
class SpawnTool(Tool, ContextAware):
    """Tool to spawn a subagent for background task execution."""

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._origin_channel: ContextVar[str] = ContextVar("spawn_origin_channel", default="cli")
        self._origin_chat_id: ContextVar[str] = ContextVar("spawn_origin_chat_id", default="direct")
        self._session_key: ContextVar[str] = ContextVar("spawn_session_key", default="cli:direct")
        self._origin_message_id: ContextVar[str | None] = ContextVar(
            "spawn_origin_message_id",
            default=None,
        )
        self._parent_task_id: ContextVar[str | None] = ContextVar(
            "spawn_parent_task_id",
            default=None,
        )
        self._parent_plan_hash: ContextVar[str | None] = ContextVar(
            "spawn_parent_plan_hash",
            default=None,
        )
        self._parent_plan_status: ContextVar[str | None] = ContextVar(
            "spawn_parent_plan_status",
            default=None,
        )
        self._approved_plan_hash: ContextVar[str | None] = ContextVar(
            "spawn_approved_plan_hash",
            default=None,
        )

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(manager=ctx.subagent_manager)

    def set_context(self, ctx: RequestContext) -> None:
        """Set the origin context for subagent announcements."""
        self._origin_channel.set(ctx.channel)
        self._origin_chat_id.set(ctx.chat_id)
        self._session_key.set(ctx.session_key or f"{ctx.channel}:{ctx.chat_id}")
        self._origin_message_id.set(ctx.message_id)
        raw_task_id = ctx.metadata.get("_runtime_task_id") if isinstance(ctx.metadata, dict) else None
        self._parent_task_id.set(str(raw_task_id) if raw_task_id else None)
        raw_plan_hash = (
            ctx.metadata.get("_runtime_plan_hash") if isinstance(ctx.metadata, dict) else None
        )
        raw_plan_status = (
            ctx.metadata.get("_runtime_plan_status") if isinstance(ctx.metadata, dict) else None
        )
        raw_approved_hash = (
            ctx.metadata.get("_runtime_approved_plan_hash")
            if isinstance(ctx.metadata, dict)
            else None
        )
        self._parent_plan_hash.set(str(raw_plan_hash) if raw_plan_hash else None)
        self._parent_plan_status.set(str(raw_plan_status) if raw_plan_status else None)
        self._approved_plan_hash.set(str(raw_approved_hash) if raw_approved_hash else None)

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        return (
            "Spawn a subagent to handle a task in the background. "
            "Use this for complex or time-consuming tasks that can run independently. "
            "The subagent will complete the task and report back when done. "
            "For deliverables or existing projects, inspect the workspace first "
            "and use a dedicated subdirectory when helpful."
        )

    async def execute(
        self,
        task: str,
        label: str | None = None,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> str:
        """Spawn a subagent to execute the given task."""
        parent_task_id = self._parent_task_id.get()
        plan_hash = self._parent_plan_hash.get()
        if (
            not parent_task_id
            or self._parent_plan_status.get() != "active"
            or not plan_hash
            or self._approved_plan_hash.get() != plan_hash
        ):
            return (
                "Error: policy_denied: spawn requires an active parent plan whose "
                "approved_plan_hash matches the current plan_hash."
            )
        running = self._manager.get_running_count()
        limit = self._manager.max_concurrent_subagents
        if running >= limit:
            return (
                f"Cannot spawn subagent: concurrency limit reached "
                f"({running}/{limit} running). Wait for a running subagent "
                f"to complete before spawning a new one."
            )
        return await self._manager.spawn(
            task=task,
            label=label,
            origin_channel=self._origin_channel.get(),
            origin_chat_id=self._origin_chat_id.get(),
            session_key=self._session_key.get(),
            origin_message_id=self._origin_message_id.get(),
            temperature=temperature,
            workspace_scope=current_workspace_scope(),
            parent_task_id=parent_task_id,
            parent_plan_hash=plan_hash,
        )
