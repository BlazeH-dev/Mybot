from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.hook import AgentRunHookContext
from nanobot.agent.runner import AgentRunner, AgentRunResult, AgentRunSpec
from nanobot.agent.subagent import SubagentManager, SubagentStatus
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.runtime.evals.subagent_compare import compare, markdown
from nanobot.runtime.trace import TraceHook


@tool_parameters({"type": "object", "properties": {}})
class NoopTool(Tool):
    @property
    def name(self) -> str:
        return "noop"

    @property
    def description(self) -> str:
        return "noop"

    async def execute(self) -> str:
        return "ok"


class RepeatingProvider(LLMProvider):
    def __init__(self, *, usage: int = 1) -> None:
        super().__init__()
        self.calls = 0
        self.usage = usage

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content=None,
            finish_reason="tool_calls",
            usage={"prompt_tokens": self.usage, "completion_tokens": self.usage},
            tool_calls=[ToolCallRequest(id=f"c-{self.calls}", name="noop", arguments={})],
        )

    def get_default_model(self) -> str:
        return "fake"


class ChildQuestionProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.seen_messages: list[list[dict]] = []

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        self.calls += 1
        self.seen_messages.append(list(messages))
        if self.calls == 1:
            return LLMResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[ToolCallRequest(
                    id="child-question",
                    name="request_user_input",
                    arguments={
                        "questions": [{
                            "id": "choice",
                            "question": "Which format?",
                            "header": "Format",
                        }],
                        "strategy": "required",
                    },
                )],
            )
        return LLMResponse(content="child done", finish_reason="stop")

    def get_default_model(self) -> str:
        return "fake"


class FinalProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return LLMResponse(content="done", finish_reason="stop")

    def get_default_model(self) -> str:
        return "fake"


@pytest.mark.asyncio
async def test_optional_runner_budgets_remain_available_as_low_level_guards() -> None:
    tools = ToolRegistry()
    tools.register(NoopTool())
    tool_result = await AgentRunner(RepeatingProvider()).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "go"}],
        tools=tools,
        model="fake",
        max_iterations=20,
        max_tool_result_chars=1000,
        max_tool_calls=2,
    ))
    assert tool_result.stop_reason == "budget_exceeded"
    assert "tool-call budget" in (tool_result.final_content or "")

    token_result = await AgentRunner(RepeatingProvider(usage=10)).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "go"}],
        tools=tools,
        model="fake",
        max_iterations=20,
        max_tool_result_chars=1000,
        total_token_budget=5,
    ))
    assert token_result.stop_reason == "budget_exceeded"
    assert "token budget" in (token_result.final_content or "")


def test_spawn_tool_does_not_expose_per_child_workload_quotas() -> None:
    properties = SpawnTool(MagicMock()).parameters["properties"]
    assert "token_budget" not in properties
    assert "timeout_seconds" not in properties
    assert "max_tool_calls" not in properties


@pytest.mark.asyncio
async def test_subagent_runner_receives_no_workload_quotas(tmp_path: Path) -> None:
    manager = SubagentManager(
        provider=FinalProvider(),
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=1000,
    )
    seen_specs: list[AgentRunSpec] = []

    async def complete(spec: AgentRunSpec) -> AgentRunResult:
        seen_specs.append(spec)
        return AgentRunResult(final_content="done", messages=[], stop_reason="completed")

    manager.runner.run = AsyncMock(side_effect=complete)
    await manager.spawn(
        task="long child",
        parent_task_id="parent",
        parent_plan_hash="plan",
        session_key="websocket:chat",
    )
    await asyncio.gather(*manager._running_tasks.values())
    assert len(seen_specs) == 1
    assert seen_specs[0].total_token_budget is None
    assert seen_specs[0].max_tool_calls is None


@pytest.mark.asyncio
async def test_sixth_direct_child_is_rejected(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "fake"
    manager = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=1000,
        max_concurrent_subagents=10,
    )
    manager._run_subagent = AsyncMock()
    for index in range(5):
        result = await manager.spawn(
            task=f"task {index}",
            parent_task_id="parent",
            session_key="websocket:chat",
        )
        assert "started" in result
    rejected = await manager.spawn(
        task="sixth",
        parent_task_id="parent",
        session_key="websocket:chat",
    )
    assert "direct child limit reached" in rejected


@pytest.mark.asyncio
async def test_spawn_tool_requires_active_hash_bound_parent_plan(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "fake"
    manager = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=1000,
    )
    manager._run_subagent = AsyncMock()
    tool = SpawnTool(manager)
    tool.set_context(RequestContext(
        channel="websocket",
        chat_id="chat",
        session_key="websocket:chat",
    ))
    denied = await tool.execute(task="work")
    assert "requires an active parent plan" in denied

    tool.set_context(RequestContext(
        channel="websocket",
        chat_id="chat",
        session_key="websocket:chat",
        metadata={
            "_runtime_task_id": "parent",
            "_runtime_plan_hash": "hash-new",
            "_runtime_plan_status": "active",
            "_runtime_approved_plan_hash": "hash-old",
        },
    ))
    assert "requires an active parent plan" in await tool.execute(task="work")

    tool.set_context(RequestContext(
        channel="websocket",
        chat_id="chat",
        session_key="websocket:chat",
        metadata={
            "_runtime_task_id": "parent",
            "_runtime_plan_hash": "hash",
            "_runtime_plan_status": "active",
            "_runtime_approved_plan_hash": "hash",
        },
    ))
    assert "started" in await tool.execute(task="work")
    await asyncio.gather(*manager._running_tasks.values())


@pytest.mark.asyncio
async def test_child_question_routes_to_parent_and_resumes_same_child(tmp_path: Path) -> None:
    provider = ChildQuestionProvider()
    bus = MessageBus()
    manager = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=1000,
    )
    started = await manager.spawn(
        task="ask then finish",
        origin_channel="websocket",
        origin_chat_id="chat",
        session_key="websocket:chat",
        parent_task_id="parent",
        parent_plan_hash="plan-hash",
    )
    assert "started" in started
    running = list(manager._running_tasks.values())
    outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=2)
    interaction = outbound.metadata["_agent_ui"]["interaction"]
    assert interaction["child_id"]
    assert interaction["task_id"] == "parent"
    assert interaction["plan_hash"] == "plan-hash"
    request = manager.interactions.respond(
        interaction["request_id"],
        expected_revision=interaction["revision"],
        idempotency_key="answer-child",
        response={"answer": "markdown"},
    )
    assert request.status.value == "answered"
    await asyncio.gather(*running)
    announcement = await asyncio.wait_for(bus.consume_inbound(), timeout=2)
    assert "child done" in announcement.content
    assert provider.calls == 2
    resumed_tool_results = [
        message
        for message in provider.seen_messages[1]
        if message.get("role") == "tool"
        and message.get("tool_call_id") == "child-question"
    ]
    assert resumed_tool_results
    assert json.loads(resumed_tool_results[-1]["content"])["status"] == "answered"


@pytest.mark.asyncio
async def test_child_has_no_lifecycle_timeout_and_remains_cancellable(tmp_path: Path) -> None:
    manager = SubagentManager(
        provider=FinalProvider(),
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=1000,
    )
    entered = asyncio.Event()

    async def never_finishes(spec: AgentRunSpec) -> AgentRunResult:
        del spec
        entered.set()
        await asyncio.Event().wait()

    manager.runner.run = AsyncMock(side_effect=never_finishes)
    await manager.spawn(
        task="slow child",
        parent_task_id="parent",
        parent_plan_hash="plan",
        session_key="websocket:chat",
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    running = list(manager._running_tasks.values())
    await asyncio.sleep(0.02)
    assert running and not running[0].done()
    assert await manager.cancel_by_session("websocket:chat") == 1
    assert running[0].cancelled()


@pytest.mark.asyncio
async def test_loop_guard_reports_partial_progress_as_failure(tmp_path: Path) -> None:
    manager = SubagentManager(
        provider=FinalProvider(),
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=1000,
    )
    manager.runner.run = AsyncMock(return_value=AgentRunResult(
        final_content="Stopped after the loop guard limit.",
        messages=[],
        stop_reason="max_iterations",
        tool_events=[{"name": "read_file", "status": "ok", "detail": "read input"}],
    ))
    manager._announce_result = AsyncMock()
    status = SubagentStatus(
        task_id="child",
        label="looping child",
        task_description="loop",
        started_at=0.0,
    )
    await manager._run_subagent(
        "child",
        "loop",
        "looping child",
        {"channel": "websocket", "chat_id": "chat"},
        status,
    )
    assert status.phase == "error"
    assert status.stop_reason == "max_iterations"
    result_text = manager._announce_result.await_args.args[3]
    assert "Completed steps:" in result_text
    assert manager._announce_result.await_args.args[5] == "error"


def test_subagent_tool_registry_has_no_nested_spawn_and_uses_isolated_state(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "fake"
    manager = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=1000,
    )
    tools = manager._build_tools(workspace=tmp_path / "child")
    assert "spawn" not in tools.tool_names


def test_child_artifact_root_is_scoped_under_parent_task(tmp_path: Path) -> None:
    from nanobot.runtime.artifacts import ArtifactStore

    root = ArtifactStore(tmp_path).child_root("parent", "child")
    assert root == tmp_path / ".nanobot-runtime" / "artifacts" / "parent" / "children" / "child"


def test_single_multi_comparison_records_cost_latency_and_context() -> None:
    result = compare(
        {
            "success_rate": 1.0,
            "wall_clock_ms": 120,
            "input_tokens": 100,
            "output_tokens": 20,
            "parent_context_tokens": 100,
            "failures": 0,
        },
        {
            "success_rate": 1.0,
            "wall_clock_ms": 90,
            "input_tokens": 140,
            "output_tokens": 28,
            "parent_context_tokens": 60,
            "failures": 0,
        },
    )
    assert result["delta"]["wall_clock_ms"] == -30
    assert result["delta"]["input_tokens"] == 40
    assert "deterministic fake-provider harness" in markdown(result)


@pytest.mark.asyncio
async def test_parent_child_trace_linkage_is_complete(tmp_path: Path) -> None:
    trace_path = tmp_path / ".nanobot-runtime" / "trace" / "parent.jsonl"
    parent_hook = TraceHook(
        trace_path,
        task_id="parent",
        actor="main",
        model="fake",
    )
    parent_run = AgentRunHookContext(messages=[{"role": "user", "content": "delegate"}])
    await parent_hook.before_run(parent_run)
    manager = SubagentManager(
        provider=FinalProvider(),
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=1000,
    )
    await manager.spawn(
        task="child work",
        parent_task_id="parent",
        parent_plan_hash="plan",
        session_key="websocket:chat",
    )
    running = list(manager._running_tasks.values())
    await asyncio.gather(*running)
    parent_run.stop_reason = "completed"
    await parent_hook.after_run(parent_run)
    await parent_hook.on_finally(parent_run)

    rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    child_start = next(
        row
        for row in rows
        if row.get("event.name") == "gen_ai.agent.run.start"
        and str(row.get("mybot.actor", "")).startswith("child:")
    )
    assert child_start["trace_id"] == parent_hook.trace_id
    assert child_start["parent_span_id"] == parent_hook.span_id
