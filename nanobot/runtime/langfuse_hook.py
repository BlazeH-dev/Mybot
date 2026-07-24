"""Agent lifecycle hook backed directly by the Langfuse SDK."""

from __future__ import annotations

import time
from typing import Any

from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext, AgentRunHookContext
from nanobot.runtime.langfuse import LangfuseRuntime, value_summary


class LangfuseTraceHook(AgentHook):
    """Represent one main or child Agent run as a Langfuse agent observation."""

    def __init__(
        self,
        runtime: LangfuseRuntime,
        *,
        task_id: str,
        actor: str,
        model: str,
        session_id: str | None = None,
        plan_hash: str | None = None,
        sandbox_mode: str | None = None,
        initial_events: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self.runtime = runtime
        self.task_id = task_id
        self.actor = actor
        self.model = model
        self.session_id = session_id
        self.plan_hash = plan_hash
        self.sandbox_mode = sandbox_mode
        self.initial_events = list(initial_events or [])
        self._started = 0.0
        self._runtime_token = None
        self._propagation_cm = None
        self._observation_cm = None
        self._observation = None

    async def before_run(self, context: AgentRunHookContext) -> None:
        from langfuse import propagate_attributes

        self._started = time.monotonic()
        self._runtime_token = self.runtime.bind()
        metadata = {
            "mybot.task.id": self.task_id,
            "mybot.actor": self.actor,
            "mybot.model": self.model,
            "mybot.plan.hash": self.plan_hash,
            "mybot.sandbox.mode": self.sandbox_mode,
            "mybot.input.summary": value_summary(context.messages),
        }
        try:
            self._propagation_cm = propagate_attributes(
                session_id=self.session_id,
                trace_name="mybot.task",
                metadata=self.runtime.metadata(metadata),
            )
            self._propagation_cm.__enter__()
            self._observation_cm = self.runtime.observation(
                name="mybot.agent.run",
                as_type="agent",
                input=context.messages,
                model=self.model,
                metadata=metadata,
            )
            handle = self._observation_cm.__enter__()
            self._observation = handle.observation
            for event in self.initial_events:
                name = str(event.get("name") or "mybot.runtime.event")
                attributes = event.get("attributes")
                self.runtime.create_event(
                    name,
                    attributes if isinstance(attributes, dict) else {},
                )
        except Exception:
            logger.exception("Langfuse agent observation start failed")

    async def after_iteration(self, context: AgentHookContext) -> None:
        self.runtime.create_event("mybot.agent.iteration", {
            "mybot.iteration": context.iteration,
            "gen_ai.usage.input_tokens": context.usage.get("prompt_tokens", 0),
            "gen_ai.usage.output_tokens": context.usage.get("completion_tokens", 0),
            "mybot.tool.events": context.tool_events,
            "error.type": context.error,
        })

    async def after_run(self, context: AgentRunHookContext) -> None:
        if self._observation is None:
            return
        duration_ms = max(0, int((time.monotonic() - self._started) * 1000))
        self._observation.update(
            output=self.runtime.content(context.final_content),
            usage_details={
                "input": context.usage.get("prompt_tokens", 0),
                "output": context.usage.get("completion_tokens", 0),
                "total": context.usage.get("total_tokens", 0),
            },
            metadata=self.runtime.metadata({
                "mybot.stop_reason": context.stop_reason,
                "mybot.duration_ms": duration_ms,
                "mybot.output.summary": value_summary(context.final_content),
                "mybot.tools.used": context.tools_used,
            }),
            level="ERROR" if context.error else "DEFAULT",
            status_message=context.error,
        )

    async def on_error(self, context: AgentRunHookContext) -> None:
        if self._observation is not None:
            error = context.error or (
                type(context.exception).__name__ if context.exception is not None else "unknown"
            )
            self._observation.update(level="ERROR", status_message=error[:500])

    async def on_finally(self, context: AgentRunHookContext) -> None:
        del context
        if self._observation_cm is not None:
            try:
                self._observation_cm.__exit__(None, None, None)
            finally:
                self._observation_cm = None
        if self._propagation_cm is not None:
            try:
                self._propagation_cm.__exit__(None, None, None)
            finally:
                self._propagation_cm = None
        if self._runtime_token is not None:
            self.runtime.reset(self._runtime_token)
            self._runtime_token = None
