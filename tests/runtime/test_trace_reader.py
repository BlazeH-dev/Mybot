from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.agent.hook import AgentHookContext, AgentRunHookContext
from nanobot.providers.base import ToolCallRequest
from nanobot.runtime.trace import TraceHook
from nanobot.runtime.trace_reader import read_turn_trace


@pytest.mark.asyncio
async def test_local_turn_trace_is_correlated_and_redacted(tmp_path: Path, monkeypatch) -> None:
    config = SimpleNamespace(observability=SimpleNamespace(langfuse=SimpleNamespace(enabled=False)))
    monkeypatch.setattr("nanobot.runtime.trace_reader.load_config", lambda: config)
    path = tmp_path / ".nanobot-runtime" / "trace" / "websocket_chat.jsonl"
    hook = TraceHook(
        path,
        task_id="websocket_chat",
        actor="main",
        model="fake",
        session_id="websocket:chat",
        turn_id="turn-1",
    )
    run = AgentRunHookContext(messages=[{"role": "user", "content": "private prompt"}])
    await hook.before_run(run)
    await hook.after_iteration(AgentHookContext(
        iteration=1,
        messages=[],
        tool_calls=[ToolCallRequest(id="call-1", name="read_file", arguments={"path": "/secret"})],
        tool_events=[],
        usage={"prompt_tokens": 10, "completion_tokens": 4},
    ))
    run.stop_reason = "completed"
    run.final_content = "private response"
    run.usage = {"prompt_tokens": 10, "completion_tokens": 4}
    await hook.after_run(run)
    await hook.on_finally(run)

    payload = read_turn_trace(tmp_path, "websocket:chat", "turn-1")

    assert payload["available"] is True
    assert payload["source"] == "local"
    assert payload["turn_id"] == "turn-1"
    assert payload["spans"][0]["status"] == "completed"
    assert payload["spans"][0]["events"][0]["name"] == "gen_ai.agent.run.start"
    serialized = json.dumps(payload)
    assert "private prompt" not in serialized
    assert "private response" not in serialized


def test_local_turn_trace_does_not_cross_session_or_turn(tmp_path: Path, monkeypatch) -> None:
    config = SimpleNamespace(observability=SimpleNamespace(langfuse=SimpleNamespace(enabled=False)))
    monkeypatch.setattr("nanobot.runtime.trace_reader.load_config", lambda: config)
    trace_dir = tmp_path / ".nanobot-runtime" / "trace"
    trace_dir.mkdir(parents=True)
    (trace_dir / "trace.jsonl").write_text(json.dumps({
        "timestamp": "2026-08-11T00:00:00+00:00",
        "trace_id": "trace-1",
        "span_id": "span-1",
        "event.name": "gen_ai.agent.run.start",
        "mybot.session.id": "websocket:other",
        "mybot.webui.turn.id": "turn-1",
        "attributes": {},
    }) + "\n", encoding="utf-8")

    payload = read_turn_trace(tmp_path, "websocket:chat", "turn-1")

    assert payload["available"] is False
    assert payload["spans"] == []


def test_langfuse_turn_trace_requests_complete_records(tmp_path: Path, monkeypatch) -> None:
    """Langfuse field groups are alternatives; comma-joining them drops metadata."""
    langfuse = SimpleNamespace(enabled=True)
    config = SimpleNamespace(observability=SimpleNamespace(langfuse=langfuse))
    monkeypatch.setattr("nanobot.runtime.trace_reader.load_config", lambda: config)
    calls: list[tuple[str, dict]] = []

    def list_traces(**kwargs):
        calls.append(("traces", kwargs))
        return SimpleNamespace(data=[SimpleNamespace(
            id="trace-1",
            metadata={"mybot.webui.turn.id": "turn-1"},
            html_path="/project/example/traces/trace-1",
        )])

    def get_trace(trace_id):
        calls.append(("trace", {"trace_id": trace_id}))
        return SimpleNamespace(observations=[SimpleNamespace(
            id="span-1",
            parent_observation_id=None,
            metadata={"mybot.actor": "main"},
            name="mybot.agent.run",
            type="AGENT",
            start_time="2026-08-11T00:00:00+00:00",
            end_time="2026-08-11T00:00:01+00:00",
            latency=1.0,
            level="DEFAULT",
            provided_model_name="fake",
            usage_details={"input": 10, "output": 4, "total": 14},
            status_message=None,
        )])

    runtime = SimpleNamespace(
        base_url="https://example.langfuse.test",
        client=SimpleNamespace(api=SimpleNamespace(
            trace=SimpleNamespace(list=list_traces, get=get_trace),
        )),
        release=lambda: None,
    )
    monkeypatch.setattr(
        "nanobot.runtime.langfuse.LangfuseRuntime.acquire",
        lambda _config: runtime,
    )

    payload = read_turn_trace(tmp_path, "websocket:chat", "turn-1")

    assert payload["available"] is True
    assert payload["source"] == "langfuse"
    assert payload["usage"]["total_tokens"] == 14
    assert payload["spans"][0]["actor"] == "main"
    assert calls == [
        (
            "traces",
            {
                "session_id": "websocket:chat",
                "limit": 100,
                "order_by": "timestamp.desc",
            },
        ),
        ("trace", {"trace_id": "trace-1"}),
    ]
