"""Read one WebUI turn's redacted trace for the embedded observability panel."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from nanobot.config.loader import load_config

_MAX_TRACE_FILES = 64
_MAX_TRACE_FILE_BYTES = 16 * 1024 * 1024
_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[._-])(secret|password|passwd|authorization|api[_-]?key|access[_-]?token)(?:$|[._-])",
    re.IGNORECASE,
)
_CONTENT_KEY_RE = re.compile(
    r"(?:^|[._-])(content|prompt|completion|message|input|output|result)(?:$|[._-])",
    re.IGNORECASE,
)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value:
        return value
    return None


def _safe_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if _SENSITIVE_KEY_RE.search(key):
        return "[redacted]"
    if depth >= 5:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(child_key): _safe_value(child_value, key=str(child_key), depth=depth + 1)
            for child_key, child_value in list(value.items())[:100]
            if not _SENSITIVE_KEY_RE.search(str(child_key))
        }
    if isinstance(value, list):
        return [_safe_value(item, key=key, depth=depth + 1) for item in value[:100]]
    if _CONTENT_KEY_RE.search(key) and isinstance(value, str):
        return f"[redacted content: {len(value)} chars]"
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:500]


def _span_payload(
    *,
    span_id: str,
    parent_span_id: str | None,
    actor: str | None,
    name: str,
    started_at: str | None,
    ended_at: str | None,
    duration_ms: int | None,
    status: str,
    attributes: dict[str, Any] | None,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "actor": actor,
        "name": name,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "status": status,
        "attributes": _safe_value(attributes or {}),
        "events": events or [],
    }


def _local_trace(workspace: Path, session_key: str, turn_id: str) -> dict[str, Any] | None:
    trace_dir = workspace / ".nanobot-runtime" / "trace"
    if not trace_dir.is_dir():
        return None
    candidates = sorted(
        (path for path in trace_dir.glob("*.jsonl") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:_MAX_TRACE_FILES]
    matched_rows: list[dict[str, Any]] = []
    for path in candidates:
        try:
            if path.stat().st_size > _MAX_TRACE_FILE_BYTES:
                continue
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if (
                        row.get("mybot.webui.turn.id") == turn_id
                        and row.get("mybot.session.id") == session_key
                    ):
                        matched_rows.append(row)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    if not matched_rows:
        return None

    newest_row = max(matched_rows, key=lambda row: str(row.get("timestamp") or ""))
    trace_id = str(newest_row.get("trace_id") or "")
    rows = [row for row in matched_rows if str(row.get("trace_id") or "") == trace_id]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("span_id") or "unknown")].append(row)

    spans: list[dict[str, Any]] = []
    total_input = 0
    total_output = 0
    for span_id, span_rows in grouped.items():
        span_rows.sort(key=lambda row: str(row.get("timestamp") or ""))
        first = span_rows[0]
        last = span_rows[-1]
        events = []
        duration_ms: int | None = None
        stop_reason: str | None = None
        error: str | None = None
        span_input = 0
        span_output = 0
        for row in span_rows:
            attributes = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
            span_input = max(span_input, int(attributes.get("gen_ai.usage.input_tokens") or 0))
            span_output = max(span_output, int(attributes.get("gen_ai.usage.output_tokens") or 0))
            if attributes.get("mybot.duration_ms") is not None:
                duration_ms = int(attributes["mybot.duration_ms"])
            if attributes.get("mybot.stop_reason"):
                stop_reason = str(attributes["mybot.stop_reason"])
            if attributes.get("error.type"):
                error = str(attributes["error.type"])
            events.append({
                "timestamp": _iso(row.get("timestamp")),
                "name": str(row.get("event.name") or "event"),
                "attributes": _safe_value(attributes),
            })
        total_input += span_input
        total_output += span_output
        status = "error" if error else "completed" if stop_reason else "running"
        spans.append(_span_payload(
            span_id=span_id,
            parent_span_id=first.get("parent_span_id"),
            actor=first.get("mybot.actor"),
            name="mybot.agent.run",
            started_at=_iso(first.get("timestamp")),
            ended_at=_iso(last.get("timestamp")) if stop_reason or error else None,
            duration_ms=duration_ms,
            status=status,
            attributes={
                "model": first.get("gen_ai.request.model"),
                "task_id": first.get("mybot.task.id"),
                "stop_reason": stop_reason,
                "error": error,
            },
            events=events,
        ))
    spans.sort(key=lambda span: span.get("started_at") or "")
    return {
        "available": True,
        "source": "local",
        "session_key": session_key,
        "turn_id": turn_id,
        "trace_id": trace_id,
        "trace_url": None,
        "usage": {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "total_tokens": total_input + total_output,
        },
        "spans": spans,
    }


def _remote_trace(session_key: str, turn_id: str) -> dict[str, Any] | None:
    from nanobot.runtime.langfuse import LangfuseRuntime

    config = load_config().observability.langfuse
    if not config.enabled:
        return None
    runtime = LangfuseRuntime.acquire(config)
    try:
        traces = runtime.client.api.trace.list(
            session_id=session_key,
            limit=100,
            order_by="timestamp.desc",
        )
        trace = next(
            (
                item
                for item in traces.data
                if isinstance(_field(item, "metadata"), dict)
                and _field(item, "metadata").get("mybot.webui.turn.id") == turn_id
            ),
            None,
        )
        if trace is None:
            return None
        trace_id = str(_field(trace, "id") or "")
        trace_details = runtime.client.api.trace.get(trace_id)
        spans: list[dict[str, Any]] = []
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        for observation in (_field(trace_details, "observations") or [])[:1000]:
            details = _field(observation, "usage_details") or {}
            input_tokens = int(details.get("input") or details.get("input_tokens") or 0)
            output_tokens = int(details.get("output") or details.get("output_tokens") or 0)
            total_tokens = int(details.get("total") or details.get("total_tokens") or 0)
            usage["input_tokens"] += input_tokens
            usage["output_tokens"] += output_tokens
            usage["total_tokens"] += total_tokens or input_tokens + output_tokens
            started_at = _iso(_field(observation, "start_time"))
            ended_at = _iso(_field(observation, "end_time"))
            latency = _field(observation, "latency")
            level = str(_field(observation, "level") or "DEFAULT")
            spans.append(_span_payload(
                span_id=str(_field(observation, "id") or ""),
                parent_span_id=_field(observation, "parent_observation_id"),
                actor=(_field(observation, "metadata") or {}).get("mybot.actor")
                if isinstance(_field(observation, "metadata"), dict)
                else None,
                name=str(_field(observation, "name") or _field(observation, "type") or "span"),
                started_at=started_at,
                ended_at=ended_at,
                duration_ms=int(float(latency) * 1000) if latency is not None else None,
                status="error" if level.endswith("ERROR") else "completed" if ended_at else "running",
                attributes={
                    "type": _field(observation, "type"),
                    "model": _field(observation, "provided_model_name"),
                    "usage": details,
                    "status_message": _field(observation, "status_message"),
                    "metadata": _field(observation, "metadata") or {},
                },
            ))
        spans.sort(key=lambda span: span.get("started_at") or "")
        html_path = str(_field(trace, "html_path") or "")
        trace_url = html_path if html_path.startswith("http") else f"{runtime.base_url}{html_path}"
        return {
            "available": True,
            "source": "langfuse",
            "session_key": session_key,
            "turn_id": turn_id,
            "trace_id": trace_id,
            "trace_url": trace_url or None,
            "usage": usage,
            "spans": spans,
        }
    finally:
        runtime.release()


def read_turn_trace(workspace: Path, session_key: str, turn_id: str) -> dict[str, Any]:
    """Return a redacted trace payload, preferring the configured truth source."""
    config = load_config().observability.langfuse
    trace = _remote_trace(session_key, turn_id) if config.enabled else _local_trace(
        workspace,
        session_key,
        turn_id,
    )
    return trace or {
        "available": False,
        "source": "langfuse" if config.enabled else "local",
        "session_key": session_key,
        "turn_id": turn_id,
        "trace_id": None,
        "trace_url": None,
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "spans": [],
    }
