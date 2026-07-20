"""OTel-shaped JSONL tracing through the existing AgentHook surface."""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from nanobot.agent.hook import AgentHook, AgentHookContext, AgentRunHookContext


@dataclass(frozen=True, slots=True)
class TraceContext:
    trace_id: str
    span_id: str
    task_id: str
    actor: str
    path: str


_CURRENT_TRACE: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar(
    "mybot_trace_context",
    default=None,
)


def current_trace_context() -> TraceContext | None:
    return _CURRENT_TRACE.get()


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _summary(value: Any) -> dict[str, Any]:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return {
        "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "chars": len(raw),
    }


def _append(path: str | Path, row: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, (json.dumps(row, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def emit_trace_event(name: str, attributes: dict[str, Any] | None = None) -> None:
    ctx = current_trace_context()
    if ctx is None:
        return
    _append(ctx.path, {
        "timestamp": _utc_iso(),
        "trace_id": ctx.trace_id,
        "span_id": ctx.span_id,
        "event.name": name,
        "mybot.task.id": ctx.task_id,
        "mybot.actor": ctx.actor,
        "attributes": attributes or {},
    })


class TraceHook(AgentHook):
    def __init__(
        self,
        path: str | Path,
        *,
        task_id: str,
        actor: str,
        model: str,
        parent: TraceContext | None = None,
        initial_events: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(reraise=True)
        self.path = str(path)
        self.task_id = task_id
        self.actor = actor
        self.model = model
        self.trace_id = parent.trace_id if parent is not None else uuid4().hex
        self.span_id = uuid4().hex[:16]
        self.parent_span_id = parent.span_id if parent is not None else None
        self.initial_events = list(initial_events or [])
        self._started = 0.0
        self._token: contextvars.Token[TraceContext | None] | None = None

    def _row(self, name: str, attributes: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "timestamp": _utc_iso(),
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "event.name": name,
            "gen_ai.system": "mybot",
            "gen_ai.request.model": self.model,
            "mybot.task.id": self.task_id,
            "mybot.actor": self.actor,
            "attributes": attributes or {},
        }

    async def before_run(self, context: AgentRunHookContext) -> None:
        self._started = time.monotonic()
        trace_context = TraceContext(
            trace_id=self.trace_id,
            span_id=self.span_id,
            task_id=self.task_id,
            actor=self.actor,
            path=self.path,
        )
        self._token = _CURRENT_TRACE.set(trace_context)
        _append(self.path, self._row("gen_ai.agent.run.start", {
            "mybot.input.summary": _summary(context.messages),
        }))
        for event in self.initial_events:
            name = str(event.get("name") or "mybot.runtime.event")
            attributes = event.get("attributes")
            emit_trace_event(name, attributes if isinstance(attributes, dict) else {})

    async def after_iteration(self, context: AgentHookContext) -> None:
        attributes: dict[str, Any] = {
            "gen_ai.usage.input_tokens": context.usage.get("prompt_tokens", 0),
            "gen_ai.usage.output_tokens": context.usage.get("completion_tokens", 0),
            "mybot.iteration": context.iteration,
            "mybot.tool.calls": [
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": _summary(call.arguments),
                }
                for call in context.tool_calls
            ],
            "mybot.tool.events": context.tool_events,
        }
        if context.error:
            attributes["error.type"] = context.error
        _append(self.path, self._row("gen_ai.agent.iteration", attributes))

    async def after_run(self, context: AgentRunHookContext) -> None:
        duration_ms = max(0, int((time.monotonic() - self._started) * 1000))
        _append(self.path, self._row("gen_ai.agent.run.end", {
            "mybot.stop_reason": context.stop_reason,
            "mybot.duration_ms": duration_ms,
            "gen_ai.usage.input_tokens": context.usage.get("prompt_tokens", 0),
            "gen_ai.usage.output_tokens": context.usage.get("completion_tokens", 0),
            "mybot.output.summary": _summary(context.final_content),
            "error.type": context.error,
        }))

    async def on_error(self, context: AgentRunHookContext) -> None:
        _append(self.path, self._row("gen_ai.agent.run.error", {
            "error.type": context.error or type(context.exception).__name__,
        }))

    async def on_finally(self, context: AgentRunHookContext) -> None:
        del context
        if self._token is not None:
            _CURRENT_TRACE.reset(self._token)
            self._token = None


def export_jsonl_to_otlp(input_path: str | Path, output_path: str | Path) -> int:
    """Write a minimal OTLP/JSON envelope consumable by standard collectors."""
    events = [
        json.loads(line)
        for line in Path(input_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    spans: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        key = (str(event.get("trace_id")), str(event.get("span_id")))
        span = spans.setdefault(key, {
            "traceId": key[0],
            "spanId": key[1],
            "parentSpanId": event.get("parent_span_id"),
            "name": "mybot.agent.run",
            "events": [],
            "attributes": {
                "gen_ai.system": event.get("gen_ai.system", "mybot"),
                "mybot.task.id": event.get("mybot.task.id"),
                "mybot.actor": event.get("mybot.actor"),
            },
        })
        span["events"].append({
            "timeUnixNano": int(datetime.fromisoformat(event["timestamp"]).timestamp() * 1e9),
            "name": event.get("event.name"),
            "attributes": event.get("attributes", {}),
        })
    output = {"resourceSpans": [{"scopeSpans": [{"spans": list(spans.values())}]}]}
    Path(output_path).write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(spans)
