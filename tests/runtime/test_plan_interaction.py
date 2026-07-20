import json
from pathlib import Path

import pytest

from nanobot.agent.progress_hook import AgentProgressHook
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.plan import PlanTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.runtime.artifacts import ArtifactStore
from nanobot.runtime.interactions import InteractionManager
from nanobot.session.manager import SessionManager


class PlanCreateProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return LLMResponse(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[ToolCallRequest(
                id="create-plan",
                name="plan",
                arguments={
                    "action": "create",
                    "task_id": "task-runner-plan",
                    "goal": "Do work",
                    "steps": [{"id": "one", "description": "Do it"}],
                },
            )],
        )

    def get_default_model(self) -> str:
        return "fake"


@pytest.mark.asyncio
async def test_plan_only_confirmation_is_a_durable_interaction(tmp_path: Path) -> None:
    bus = MessageBus()
    tool = PlanTool(tmp_path, SessionManager(tmp_path), bus)
    tool.set_context(RequestContext(
        channel="websocket",
        chat_id="chat-1",
        message_id="turn-1",
        session_key="websocket:chat-1",
        metadata={"execution_mode": "plan_only"},
    ))
    created = json.loads(await tool.execute(
        action="create",
        task_id="task-plan",
        goal="Do work",
        steps=[{"id": "one", "description": "Do it"}],
    ))
    request_id = created["plan"]["interaction_request_id"]
    request = InteractionManager(tmp_path).get(request_id)
    assert request.plan_hash == created["plan"]["plan_hash"]
    outbound = await bus.consume_outbound()
    assert outbound.metadata["_agent_ui"]["kind"] == "interaction_request"

    manager = InteractionManager(tmp_path)
    answered = manager.respond(
        request_id,
        expected_revision=request.revision,
        idempotency_key="confirm",
        response={"approved": True},
    )
    assert answered.status.value == "answered"
    tool.set_context(RequestContext(
        channel="websocket",
        chat_id="chat-1",
        message_id="turn-2",
        session_key="websocket:chat-1",
        metadata={"execution_mode": "default"},
    ))
    confirmed = json.loads(await tool.execute(
        action="confirm",
        task_id="task-plan",
        expected_plan_hash=created["plan"]["plan_hash"],
    ))
    assert confirmed["plan"]["status"] == "active"
    assert manager.get(request_id).status.value == "consumed"


@pytest.mark.asyncio
async def test_plan_only_create_suspends_runner_with_typed_state(tmp_path: Path) -> None:
    bus = MessageBus()
    tool = PlanTool(tmp_path, SessionManager(tmp_path), bus)
    tool.set_context(RequestContext(
        channel="websocket",
        chat_id="chat-1",
        message_id="turn-1",
        session_key="websocket:chat-1",
        metadata={"execution_mode": "plan_only"},
    ))
    tools = ToolRegistry()
    tools.register(tool)
    progress_events: list[dict] = []

    async def on_progress(
        content: str,
        *,
        tool_hint: bool = False,
        tool_events: list[dict] | None = None,
    ) -> None:
        del content, tool_hint
        progress_events.extend(tool_events or [])

    result = await AgentRunner(PlanCreateProvider()).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "plan it"}],
        tools=tools,
        model="fake",
        max_iterations=2,
        max_tool_result_chars=2000,
        progress_callback=on_progress,
        hook=AgentProgressHook(on_progress=on_progress),
    ))
    assert result.stop_reason == "awaiting_plan_confirmation"
    assert len([message for message in result.messages if message.get("role") == "tool"]) == 1
    finished = next(event for event in progress_events if event["phase"] == "end")
    progress_plan = json.loads(finished["result"])["plan"]
    assert progress_plan["status"] == "awaiting_confirmation"
    assert progress_plan["plan_hash"]
    interaction = (await bus.consume_outbound()).metadata["_agent_ui"]["interaction"]
    assert interaction["kind"] == "plan_confirmation"


@pytest.mark.asyncio
async def test_plan_creation_snapshots_attached_source_inputs(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("version one", encoding="utf-8")
    tool = PlanTool(tmp_path, SessionManager(tmp_path))
    tool.set_context(RequestContext(
        channel="websocket",
        chat_id="chat-1",
        message_id="turn-1",
        session_key="websocket:chat-1",
        metadata={
            "execution_mode": "default",
            "_runtime_input_paths": [str(source)],
        },
    ))
    created = json.loads(await tool.execute(
        action="create",
        task_id="task-input",
        goal="Use source",
        steps=[{"id": "one", "description": "Read snapshot"}],
    ))
    artifact_id = created["plan"]["input_artifacts"][0]
    record = ArtifactStore(tmp_path).get("task-input", artifact_id)
    source.write_text("version two", encoding="utf-8")
    assert Path(record.path).read_text(encoding="utf-8") == "version one"
    assert record.snapshot_status == "copied"
    assert record.replayable is True
    await tool.execute(
        action="update_step",
        task_id="task-input",
        step_id="one",
        status="in_progress",
    )
    plan_record = ArtifactStore(tmp_path).get("task-input", "plan")
    assert ArtifactStore(tmp_path).verify(plan_record)
