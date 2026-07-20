from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.filesystem import WriteFileTool
from nanobot.agent.tools.interaction import RequestUserInputTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.runtime.approvals import ApprovalBinding, ApprovalManager, normalized_params_hash
from nanobot.runtime.interactions import (
    InteractionError,
    InteractionKind,
    InteractionManager,
    InteractionStatus,
    InteractionStrategy,
)
from nanobot.runtime.policy import PermissionDecision, PolicyGateOutcome
from nanobot.session.manager import SessionManager


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 18, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


class ApprovalProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[ToolCallRequest(
                id="call-write",
                name="write_file",
                arguments={"path": "existing.txt", "content": "changed"},
            )],
        )

    def get_default_model(self) -> str:
        return "fake"


def test_request_user_input_schema_is_json_serializable() -> None:
    registry = ToolRegistry()
    registry.register(RequestUserInputTool())

    definitions = registry.get_definitions()
    serialized = json.dumps(definitions, ensure_ascii=False)
    option_schema = definitions[0]["function"]["parameters"]["properties"]["questions"][
        "items"
    ]["properties"]["options"]["items"]

    assert '"request_user_input"' in serialized
    assert option_schema["properties"]["description"]["type"] == "string"
    assert "description" not in option_schema


def test_required_waits_until_explicit_response_and_consumes_once(tmp_path: Path) -> None:
    clock = Clock()
    manager = InteractionManager(tmp_path, now=clock)
    request = manager.create(
        kind=InteractionKind.QUESTION,
        strategy=InteractionStrategy.REQUIRED,
        questions=[{"id": "q", "question": "Continue?", "header": "Choice"}],
    )
    clock.value += timedelta(days=1)
    assert manager.expire_due() == []
    answered = manager.respond(
        request.request_id,
        expected_revision=1,
        idempotency_key="answer-1",
        response={"answer": "yes"},
    )
    assert answered.status == InteractionStatus.ANSWERED
    consumed = manager.consume(
        request.request_id,
        expected_revision=answered.revision,
        idempotency_key="consume-1",
    )
    assert consumed.status == InteractionStatus.CONSUMED
    assert manager.consume(
        request.request_id,
        expected_revision=1,
        idempotency_key="consume-1",
    ).status == InteractionStatus.CONSUMED


def test_auto_resolve_uses_deterministic_default(tmp_path: Path) -> None:
    clock = Clock()
    manager = InteractionManager(tmp_path, now=clock)
    request = manager.create(
        kind=InteractionKind.QUESTION,
        strategy=InteractionStrategy.AUTO_RESOLVE,
        payload={"default": "concise"},
        expires_at=(clock.value + timedelta(seconds=60)).isoformat(),
    )
    clock.value += timedelta(seconds=61)
    [resolved] = manager.expire_due()
    assert resolved.request_id == request.request_id
    assert resolved.status == InteractionStatus.TIMED_OUT
    assert resolved.resolution == {"source": "deterministic_default", "value": "concise"}


def test_expire_and_deny_never_approves_and_revision_race_is_rejected(tmp_path: Path) -> None:
    clock = Clock()
    manager = InteractionManager(tmp_path, now=clock)
    request = manager.create(
        kind=InteractionKind.APPROVAL,
        strategy=InteractionStrategy.EXPIRE_AND_DENY,
        expires_at=(clock.value + timedelta(seconds=1)).isoformat(),
    )
    clock.value += timedelta(seconds=2)
    expired = manager.respond(
        request.request_id,
        expected_revision=1,
        idempotency_key="late",
        response={"approved": True},
    )
    assert expired.status == InteractionStatus.EXPIRED
    assert expired.resolution == {"approved": False, "reason": "deadline_expired"}
    with pytest.raises(InteractionError, match=request.request_id):
        manager.respond(
            request.request_id,
            expected_revision=1,
            idempotency_key="other",
            response={"approved": True},
        )


def test_approval_is_bound_to_params_plan_and_one_shot(tmp_path: Path) -> None:
    manager = InteractionManager(tmp_path)
    approvals = ApprovalManager(manager)
    binding = ApprovalBinding(
        tool_name="exec",
        normalized_params_hash=normalized_params_hash({"command": "git commit"}),
        task_id="task-1",
        plan_hash="plan-1",
        step_id="commit",
        child_id=None,
        target="git commit",
        risk="high",
        reason="high risk",
        sandbox_mode="workspace_write",
        chat_id="chat-1",
        command_hash="cmd-1",
    )
    request = approvals.request(binding, tool_call_id="call-1", turn_id="turn-1")
    approved = manager.respond(
        request.request_id,
        expected_revision=request.revision,
        idempotency_key="approve",
        response={"approved": True},
    )
    assert approvals.matches(approved, binding)
    changed = ApprovalBinding(**{
        **binding.as_dict(),
        "normalized_params_hash": normalized_params_hash({"command": "git push"}),
        "writable_roots": (),
        "network_domains": (),
        "ports": (),
        "network_addresses": (),
    })
    assert not approvals.matches(approved, changed)
    manager.consume(
        approved.request_id,
        expected_revision=approved.revision,
        idempotency_key="call-1",
    )
    assert approvals.find_approved(binding) is None


@pytest.mark.asyncio
async def test_runner_suspends_without_executing_or_calling_provider_again(tmp_path: Path) -> None:
    target = tmp_path / "existing.txt"
    target.write_text("original")
    tools = ToolRegistry()
    tools.register(WriteFileTool(workspace=tmp_path, enforce_occ=True))
    provider = ApprovalProvider()
    checkpoints: list[dict] = []

    async def gate(**kwargs):
        return PolicyGateOutcome(
            decision=PermissionDecision(
                action="ask",
                reason="approval required",
                matched_rules=("test",),
                risk_level="high",
            ),
            interaction={"kind": "approval", "request_id": "ir_test"},
        )

    async def checkpoint(payload):
        checkpoints.append(payload)

    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "change it"}],
        tools=tools,
        model="fake",
        max_iterations=5,
        max_tool_result_chars=1000,
        policy_gate=gate,
        checkpoint_callback=checkpoint,
    ))
    assert result.stop_reason == "awaiting_approval"
    assert provider.calls == 1
    assert target.read_text() == "original"
    assert checkpoints[-1]["phase"] == "awaiting_approval"


def test_interaction_response_materializes_as_matching_tool_result(tmp_path: Path) -> None:
    sessions = SessionManager(tmp_path)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=ApprovalProvider(),
        workspace=tmp_path,
        session_manager=sessions,
    )
    session = sessions.get_or_create("websocket:chat")
    session.metadata["plan_state"] = {
        "task_id": "task-1",
        "status": "active",
        "plan_hash": "plan-1",
        "approved_plan_hash": "plan-1",
    }
    request = loop.interactions.create(
        kind=InteractionKind.QUESTION,
        strategy=InteractionStrategy.REQUIRED,
        task_id="task-1",
        plan_hash="plan-1",
        tool_call_id="call-question",
        payload={"chat_id": "chat"},
    )
    loop._set_runtime_checkpoint(session, {
        "phase": "awaiting_question",
        "assistant_message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call-question",
                "type": "function",
                "function": {"name": "request_user_input", "arguments": "{}"},
            }],
        },
        "completed_tool_results": [{
            "role": "tool",
            "tool_call_id": "call-question",
            "name": "request_user_input",
            "content": "awaiting",
        }],
        "pending_tool_calls": [],
        "interaction": request.as_dict(),
    })
    answered = loop.interactions.respond(
        request.request_id,
        expected_revision=request.revision,
        idempotency_key="answer",
        response={"answer": "yes"},
    )
    assert answered.status == InteractionStatus.ANSWERED
    assert loop._materialize_interaction_response(session, request.request_id)
    assert loop._restore_runtime_checkpoint(session)
    tool_result = next(
        message
        for message in session.messages
        if message.get("tool_call_id") == "call-question"
    )
    assert json.loads(tool_result["content"])["status"] == "answered"
    assert loop.interactions.get(request.request_id).status == InteractionStatus.CONSUMED


@pytest.mark.asyncio
async def test_ordinary_chat_cannot_bypass_required_interaction(tmp_path: Path) -> None:
    provider = ApprovalProvider()
    sessions = SessionManager(tmp_path)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        session_manager=sessions,
    )
    session = sessions.get_or_create("websocket:chat")
    session.metadata["plan_state"] = {
        "task_id": "task-1",
        "status": "active",
        "plan_hash": "plan-1",
        "approved_plan_hash": "plan-1",
    }
    request = loop.interactions.create(
        kind=InteractionKind.QUESTION,
        strategy=InteractionStrategy.REQUIRED,
        task_id="task-1",
        plan_hash="plan-1",
        tool_call_id="call-question",
        payload={"chat_id": "chat"},
    )
    loop._set_runtime_checkpoint(session, {
        "phase": "awaiting_question",
        "assistant_message": None,
        "completed_tool_results": [],
        "pending_tool_calls": [],
        "interaction": request.as_dict(),
    })
    response = await loop._process_message(InboundMessage(
        channel="websocket",
        sender_id="user",
        chat_id="chat",
        content="just continue",
    ))
    assert response is not None
    assert "ordinary chat cannot consume or bypass" in response.content
    assert provider.calls == 0
    assert loop.interactions.get(request.request_id).status == InteractionStatus.PENDING
    assert session.metadata.get(loop._RUNTIME_CHECKPOINT_KEY) is not None
