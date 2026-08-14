from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec, ToolSuspensionError
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import AgentDefaults, PtcConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest


class _Tool(Tool):
    def __init__(self, name: str, events: list[str], *, read_only: bool, delay: float = 0) -> None:
        self._name = name
        self.events = events
        self._read_only = read_only
        self.delay = delay

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"value": {"type": "integer"}}}

    @property
    def read_only(self) -> bool:
        return self._read_only

    async def execute(self, **kwargs):
        self.events.append(f"start:{self.name}")
        await asyncio.sleep(self.delay)
        self.events.append(f"end:{self.name}")
        return {"tool": self.name, **kwargs}


class _NonJsonTool(_Tool):
    async def execute(self, **kwargs):
        return (self.name, kwargs)


def _spec(tools: ToolRegistry, tmp_path: Path, *, mode: str) -> AgentRunSpec:
    return AgentRunSpec(
        initial_messages=[{"role": "system", "content": "base"}],
        tools=tools,
        model="test",
        max_iterations=2,
        max_tool_result_chars=AgentDefaults().max_tool_result_chars,
        workspace=tmp_path,
        tool_mode=mode,
        ptc_config=PtcConfig(sandbox="none", wall_timeout_seconds=5),
        concurrent_tools=True,
    )


def test_runner_projects_native_code_and_both(tmp_path: Path) -> None:
    tools = ToolRegistry()
    tools.register(_Tool("read_a", [], read_only=True))
    runner = AgentRunner(MagicMock())
    assert [item["function"]["name"] for item in runner._tool_definitions(_spec(tools, tmp_path, mode="native"))] == ["read_a"]
    assert [item["function"]["name"] for item in runner._tool_definitions(_spec(tools, tmp_path, mode="code"))] == ["run_code"]
    assert [item["function"]["name"] for item in runner._tool_definitions(_spec(tools, tmp_path, mode="both"))] == ["read_a", "run_code"]


@pytest.mark.asyncio
async def test_code_mode_runs_program_with_nested_events(tmp_path: Path) -> None:
    calls = {"count": 0}
    captured_requests: list[dict] = []
    events: list[str] = []
    provider = MagicMock()

    async def chat_with_retry(**kwargs):
        captured_requests.append(kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id="outer",
                    name="run_code",
                    arguments={
                        "description": "Read values concurrently",
                        "code": (
                            'values = await asyncio.gather('
                            'tools.read_a({"value": 1}), tools.read_b({"value": 2}))\n'
                            'return {"names": [item["tool"] for item in values]}'
                        ),
                    },
                )],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="done")

    provider.chat_with_retry = chat_with_retry
    tools = ToolRegistry()
    tools.register(_Tool("read_a", events, read_only=True, delay=0.05))
    tools.register(_Tool("read_b", events, read_only=True, delay=0.05))
    result = await AgentRunner(provider).run(_spec(tools, tmp_path, mode="code"))

    assert result.final_content == "done"
    assert [item["function"]["name"] for item in captured_requests[0]["tools"]] == ["run_code"]
    assert "# Programmatic Tool Calling" in captured_requests[0]["messages"][0]["content"]
    tool_messages = [message for message in result.messages if message.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["name"] == "run_code"
    assert "read_a" in tool_messages[0]["content"]
    outer_event = result.tool_events[0]
    assert len(outer_event["ptc_subcalls"]) == 2
    assert outer_event["ptc_metrics"]["peak_parallel"] == 2
    assert events[:2] == ["start:read_a", "start:read_b"]


@pytest.mark.asyncio
async def test_ptc_subcalls_emit_content_free_trace_events(tmp_path: Path, monkeypatch) -> None:
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "nanobot.agent.runner.emit_trace_event",
        lambda name, attributes: emitted.append((name, attributes)),
    )
    tools = ToolRegistry()
    tools.register(_Tool("read_a", [], read_only=True))

    result, event, error = await AgentRunner(MagicMock())._run_ptc_tool(
        _spec(tools, tmp_path, mode="code"),
        ToolCallRequest(
            id="outer",
            name="run_code",
            arguments={
                "description": "Read one value",
                "code": "return await tools.read_a({'value': 7})",
            },
        ),
        {},
        {},
    )

    assert error is None
    assert event["status"] == "ok"
    assert "read_a" in result
    start = next(item for item in emitted if item[0] == "mybot.ptc.subcall.start")
    finish = next(item for item in emitted if item[0] == "mybot.ptc.subcall.finish")
    assert start[1]["sub_call_id"] == "outer:ptc:1"
    assert start[1]["arguments_summary"]["chars"] > 0
    assert "value" not in start[1]
    assert finish[1]["status"] == "ok"


@pytest.mark.asyncio
async def test_ptc_marks_non_json_tool_results_as_subcall_errors(tmp_path: Path) -> None:
    tools = ToolRegistry()
    tools.register(_NonJsonTool("tuple_result", [], read_only=True))

    result, event, error = await AgentRunner(MagicMock())._run_ptc_tool(
        _spec(tools, tmp_path, mode="code"),
        ToolCallRequest(
            id="outer",
            name="run_code",
            arguments={
                "description": "Reject non JSON result",
                "code": "return await tools.tuple_result({'value': 1})",
            },
        ),
        {},
        {},
    )

    assert error is None
    assert event["status"] == "error"
    assert event["ptc_subcalls"][0]["phase"] == "error"
    assert "invalid_json" in result


@pytest.mark.asyncio
async def test_code_mode_rejects_direct_native_tool_call(tmp_path: Path) -> None:
    tools = ToolRegistry()
    events: list[str] = []
    tools.register(_Tool("read_a", events, read_only=True))
    runner = AgentRunner(MagicMock())
    result, event, error = await runner._run_tool_unobserved(
        _spec(tools, tmp_path, mode="code"),
        ToolCallRequest(id="bad", name="read_a", arguments={}),
        {},
        {},
    )
    assert "not directly callable" in result
    assert event["status"] == "error"
    assert error is None
    assert events == []


@pytest.mark.asyncio
async def test_ptc_scheduler_places_write_barrier_between_reads(tmp_path: Path) -> None:
    events: list[str] = []
    tools = ToolRegistry()
    tools.register(_Tool("read_a", events, read_only=True, delay=0.03))
    tools.register(_Tool("write", events, read_only=False, delay=0.01))
    tools.register(_Tool("read_b", events, read_only=True, delay=0.01))
    runner = AgentRunner(MagicMock())
    result, event, error = await runner._run_ptc_tool(
        _spec(tools, tmp_path, mode="code"),
        ToolCallRequest(
            id="outer",
            name="run_code",
            arguments={
                "description": "Run ordered work",
                "code": (
                    'a = asyncio.gather(tools.read_a({"value": 1}))\n'
                    'w = tools.write({"value": 2})\n'
                    'b = tools.read_b({"value": 3})\n'
                    'await a\nawait w\nawait b\nreturn "ok"'
                ),
            },
        ),
        {},
        {},
    )
    assert error is None
    assert "ok" in result
    assert event["status"] == "ok"
    assert events.index("end:read_a") < events.index("start:write")
    assert events.index("end:write") < events.index("start:read_b")


@pytest.mark.asyncio
async def test_ptc_safe_reads_remain_concurrent_with_policy_checks(tmp_path: Path) -> None:
    events: list[str] = []
    policy_calls: list[str] = []
    tools = ToolRegistry()
    tools.register(_Tool("read_a", events, read_only=True, delay=0.05))
    tools.register(_Tool("read_b", events, read_only=True, delay=0.05))

    async def policy_gate(**kwargs):
        policy_calls.append(kwargs["tool_call"].name)
        decision = type("Decision", (), {"action": "allow", "reason": "allowed"})()
        return type("Outcome", (), {"decision": decision, "interaction": None})()

    spec = _spec(tools, tmp_path, mode="code")
    spec.policy_gate = policy_gate
    result, event, error = await AgentRunner(MagicMock())._run_ptc_tool(
        spec,
        ToolCallRequest(
            id="outer",
            name="run_code",
            arguments={
                "description": "Read values concurrently",
                "code": (
                    "return await asyncio.gather("
                    "tools.read_a({'value': 1}), tools.read_b({'value': 2}))"
                ),
            },
        ),
        {},
        {},
    )

    assert error is None
    assert "read_a" in result
    assert event["ptc_metrics"]["peak_parallel"] == 2
    assert policy_calls == ["read_a", "read_b"]
    assert events[:2] == ["start:read_a", "start:read_b"]


@pytest.mark.asyncio
async def test_ptc_program_end_cancels_calls_that_never_started(tmp_path: Path) -> None:
    events: list[str] = []
    tools = ToolRegistry()
    tools.register(_Tool("slow", events, read_only=True, delay=0.05))
    tools.register(_Tool("write", events, read_only=False))
    spec = _spec(tools, tmp_path, mode="code")
    spec.ptc_config = PtcConfig(
        sandbox="none",
        wall_timeout_seconds=5,
        max_parallel_sub_calls=1,
    )

    result, event, error = await AgentRunner(MagicMock())._run_ptc_tool(
        spec,
        ToolCallRequest(
            id="outer",
            name="run_code",
            arguments={
                "description": "Return before queued write",
                "code": (
                    "pending = asyncio.gather(\n"
                    "    tools.slow({'value': 1}),\n"
                    "    tools.write({'value': 2}),\n"
                    ")\n"
                    "await asyncio.sleep(0)\n"
                    "return 'done'"
                ),
            },
        ),
        {},
        {},
    )

    assert error is None
    assert event["status"] == "ok"
    assert "done" in result
    assert "start:write" not in events


@pytest.mark.asyncio
async def test_ptc_wall_timeout_cancels_active_host_tool(tmp_path: Path) -> None:
    events: list[str] = []
    tools = ToolRegistry()
    tools.register(_Tool("slow", events, read_only=False, delay=10))
    spec = _spec(tools, tmp_path, mode="code")
    spec.ptc_config = PtcConfig(sandbox="none", wall_timeout_seconds=1)

    started = asyncio.get_running_loop().time()
    result, event, error = await AgentRunner(MagicMock())._run_ptc_tool(
        spec,
        ToolCallRequest(
            id="outer",
            name="run_code",
            arguments={
                "description": "Time out slow tool",
                "code": "return await tools.slow({'value': 1})",
            },
        ),
        {},
        {},
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert error is None
    assert event["status"] == "error"
    assert "ptc_timeout" in result
    assert elapsed < 3
    assert events == ["start:slow"]


@pytest.mark.asyncio
async def test_ptc_approval_suspends_outer_run_and_stops_later_calls(tmp_path: Path) -> None:
    events: list[str] = []
    tools = ToolRegistry()
    tools.register(_Tool("write", events, read_only=False))
    tools.register(_Tool("later", events, read_only=False))

    async def policy_gate(**kwargs):
        call = kwargs["tool_call"]
        if call.name == "write":
            decision = type("Decision", (), {"action": "ask", "reason": "approval required"})()
            return type("Outcome", (), {
                "decision": decision,
                "interaction": {"kind": "approval", "request_id": "req-1"},
            })()
        decision = type("Decision", (), {"action": "allow", "reason": "allowed"})()
        return type("Outcome", (), {"decision": decision, "interaction": None})()

    spec = _spec(tools, tmp_path, mode="code")
    spec.policy_gate = policy_gate
    result, event, error = await AgentRunner(MagicMock())._run_ptc_tool(
        spec,
        ToolCallRequest(
            id="outer",
            name="run_code",
            arguments={
                "description": "Request guarded write",
                "code": (
                    'try:\n'
                    '    await tools.write({"value": 1})\n'
                    'except ToolCallError:\n'
                    '    pass\n'
                    'await tools.later({"value": 2})\n'
                    'return "unexpected"'
                ),
            },
        ),
        {},
        {},
    )
    assert isinstance(error, ToolSuspensionError)
    assert event["status"] == "error"
    assert "ptc_approval_required" in result
    assert events == []


@pytest.mark.asyncio
async def test_ptc_reuses_successful_exact_reads_across_program_retries(tmp_path: Path) -> None:
    requests = 0
    provider = MagicMock()

    async def chat_with_retry(**_kwargs):
        nonlocal requests
        requests += 1
        if requests == 1:
            code = "await tools.read_a({'value': 7})\nraise ValueError('retry compactly')"
        elif requests == 2:
            code = "return await tools.read_a({'value': 7})"
        else:
            return LLMResponse(content="done")
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(
                id=f"outer-{requests}",
                name="run_code",
                arguments={"description": "Read one value", "code": code},
            )],
            finish_reason="tool_calls",
        )

    provider.chat_with_retry = chat_with_retry
    events: list[str] = []
    tools = ToolRegistry()
    tools.register(_Tool("read_a", events, read_only=True))
    spec = _spec(tools, tmp_path, mode="code")
    spec.max_iterations = 3

    result = await AgentRunner(provider).run(spec)

    assert result.final_content == "done"
    assert events == ["start:read_a", "end:read_a"]
    assert result.tool_events[0]["ptc_metrics"]["cache_hits"] == 0
    assert result.tool_events[1]["ptc_metrics"]["cache_hits"] == 1
    assert result.tool_events[1]["ptc_metrics"]["executed_subcall_count"] == 0
    assert result.tool_events[1]["ptc_subcalls"][0]["cache_hit"] is True


@pytest.mark.asyncio
async def test_ptc_never_caches_writes_and_invalidates_prior_reads(tmp_path: Path) -> None:
    events: list[str] = []
    tools = ToolRegistry()
    tools.register(_Tool("read_a", events, read_only=True))
    tools.register(_Tool("write", events, read_only=False))
    spec = _spec(tools, tmp_path, mode="code")
    spec.ptc_read_cache = {}
    runner = AgentRunner(MagicMock())

    for index in range(2):
        result, event, error = await runner._run_ptc_tool(
            spec,
            ToolCallRequest(
                id=f"outer-{index}",
                name="run_code",
                arguments={
                    "description": "Read then write",
                    "code": (
                        "results = await asyncio.gather(\n"
                        "    tools.read_a({'value': 1}),\n"
                        "    tools.write({'value': 2}),\n"
                        ")\n"
                        "return results[1]"
                    ),
                },
            ),
            {},
            {},
        )
        assert error is None
        assert "write" in result
        assert event["ptc_metrics"]["cache_hits"] == 0

    assert events.count("start:read_a") == 2
    assert events.count("start:write") == 2


@pytest.mark.asyncio
async def test_ptc_read_cache_is_not_shared_between_agent_runs(tmp_path: Path) -> None:
    events: list[str] = []
    tools = ToolRegistry()
    tools.register(_Tool("read_a", events, read_only=True))

    async def run_once() -> None:
        requests = 0
        provider = MagicMock()

        async def chat_with_retry(**_kwargs):
            nonlocal requests
            requests += 1
            if requests == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCallRequest(
                        id="outer",
                        name="run_code",
                        arguments={
                            "description": "Read once",
                            "code": "return await tools.read_a({'value': 1})",
                        },
                    )],
                    finish_reason="tool_calls",
                )
            return LLMResponse(content="done")

        provider.chat_with_retry = chat_with_retry
        await AgentRunner(provider).run(_spec(tools, tmp_path, mode="code"))

    await run_once()
    await run_once()

    assert events.count("start:read_a") == 2
