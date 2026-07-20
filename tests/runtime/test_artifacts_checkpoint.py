from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.runtime.artifacts import ArtifactError, ArtifactStore
from nanobot.runtime.checkpoint import CheckpointError, CheckpointStore
from nanobot.runtime.interactions import InteractionStatus
from nanobot.session.manager import SessionManager


class NoCallProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        self.calls += 1
        raise AssertionError("recovery preparation must not call the provider")

    def get_default_model(self) -> str:
        return "fake"


def active_plan(task_id: str = "task-1") -> dict:
    return {
        "task_id": task_id,
        "status": "active",
        "plan_hash": "hash-1",
        "approved_plan_hash": "hash-1",
    }


def test_snapshot_is_immutable_and_lineage_is_recursive(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("v1")
    store = ArtifactStore(tmp_path)
    snapshot = store.snapshot_input("task-1", source, artifact_id="input")
    source.write_text("v2")
    assert Path(snapshot.path).read_text() == "v1"
    facts_path = store.task_root("task-1") / "verified_facts.json"
    facts_path.write_text("{}")
    facts = store.register(
        task_id="task-1",
        artifact_id="facts",
        path=facts_path,
        source_artifacts=["input"],
        status="validated",
    )
    report_path = store.task_root("task-1") / "report.docx"
    report_path.write_bytes(b"report")
    report = store.register(
        task_id="task-1",
        artifact_id="report",
        path=report_path,
        source_artifacts=[facts.artifact_id],
    )
    lineage = store.lineage("task-1", report.artifact_id)
    assert lineage["sources"][0]["artifact_id"] == "facts"
    assert lineage["sources"][0]["sources"][0]["artifact_id"] == "input"


def test_reference_only_is_honest_when_copy_fails(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.txt"
    source.write_text("v1")
    monkeypatch.setattr("nanobot.runtime.artifacts.shutil.copy2", lambda *_: (_ for _ in ()).throw(OSError()))
    record = ArtifactStore(tmp_path).snapshot_input("task-1", source)
    assert record.snapshot_status == "reference_only"
    assert record.replayable is False


def test_child_artifacts_cannot_escape_child_root(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    outside = tmp_path / "formal.txt"
    outside.write_text("x")
    with pytest.raises(ArtifactError) as exc:
        store.register(task_id="task-1", child_id="child-1", path=outside)
    assert exc.value.code == "child_artifact_path_escape"


def test_checkpoint_gates_on_plan_and_classifies_uncertain(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path)
    assert store.write(
        plan={"task_id": "x", "status": "awaiting_confirmation", "plan_hash": "h"},
        runner_payload={},
        session_key="s",
    ) is None
    checkpoint = store.write(
        plan=active_plan(),
        session_key="websocket:chat",
        runner_payload={
            "phase": "awaiting_tools",
            "completed_tool_results": [{"tool_call_id": "done"}],
            "pending_tool_calls": [
                {"id": "safe", "function": {"name": "read_file"}},
                {"id": "uncertain", "function": {"name": "message"}},
            ],
        },
    )
    assert checkpoint is not None
    loaded = store.load("task-1", expected_plan_hash="hash-1")
    recovery = store.recovery_plan(loaded)
    assert recovery.completed == ("done",)
    assert recovery.pending == ("safe",)
    assert recovery.uncertain == ("uncertain",)


def test_checkpoint_corruption_and_plan_change_fail_loud(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path)
    store.write(plan=active_plan(), runner_payload={}, session_key="s")
    with pytest.raises(CheckpointError) as mismatch:
        store.load("task-1", expected_plan_hash="changed")
    assert mismatch.value.code == "plan_hash_mismatch"
    path = store.path("task-1")
    path.write_text("{}")
    with pytest.raises(CheckpointError) as corrupt:
        store.load("task-1")
    assert corrupt.value.code == "checkpoint_hash_mismatch"


def test_kill_resume_loads_durable_pending_work_without_interrupted_error(tmp_path: Path) -> None:
    sessions = SessionManager(tmp_path)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=NoCallProvider(),
        workspace=tmp_path,
        session_manager=sessions,
    )
    session = sessions.get_or_create("websocket:chat")
    session.metadata["plan_state"] = active_plan()
    loop._set_runtime_checkpoint(session, {
        "phase": "awaiting_tools",
        "assistant_message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "safe",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
        "completed_tool_results": [],
        "pending_tool_calls": [{
            "id": "safe",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }],
    })
    session.metadata.pop(loop._RUNTIME_CHECKPOINT_KEY)
    sessions.save(session)

    resumed_sessions = SessionManager(tmp_path)
    resumed_loop = AgentLoop(
        bus=MessageBus(),
        provider=NoCallProvider(),
        workspace=tmp_path,
        session_manager=resumed_sessions,
    )
    resumed = resumed_sessions.get_or_create("websocket:chat")
    assert resumed_loop._restore_runtime_checkpoint(resumed)
    tool_result = next(message for message in resumed.messages if message.get("tool_call_id") == "safe")
    assert "pending_recovery" in tool_result["content"]
    assert "interrupted before this tool finished" not in tool_result["content"]


@pytest.mark.asyncio
async def test_uncertain_side_effect_requires_recovery_decision_before_resume(tmp_path: Path) -> None:
    provider = NoCallProvider()
    bus = MessageBus()
    sessions = SessionManager(tmp_path)
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        session_manager=sessions,
    )
    session = sessions.get_or_create("websocket:chat")
    session.metadata["plan_state"] = active_plan()
    loop._set_runtime_checkpoint(session, {
        "phase": "awaiting_tools",
        "assistant_message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "external",
                "type": "function",
                "function": {"name": "message", "arguments": "{}"},
            }],
        },
        "completed_tool_results": [],
        "pending_tool_calls": [{
            "id": "external",
            "type": "function",
            "function": {"name": "message", "arguments": "{}"},
        }],
    })
    assert await loop._prepare_uncertain_recovery(
        session,
        channel="websocket",
        chat_id="chat",
        turn_id="turn-2",
    )
    outbound = await bus.consume_outbound()
    interaction = outbound.metadata["_agent_ui"]["interaction"]
    assert interaction["kind"] == "recovery_decision"
    assert interaction["strategy"] == "required"
    assert provider.calls == 0

    answered = loop.interactions.respond(
        interaction["request_id"],
        expected_revision=interaction["revision"],
        idempotency_key="recover",
        response={"answer": "mark completed"},
    )
    assert answered.status == InteractionStatus.ANSWERED
    assert loop._materialize_interaction_response(session, interaction["request_id"])
    assert loop._restore_runtime_checkpoint(session)
    tool_result = next(
        message for message in session.messages
        if message.get("tool_call_id") == "external"
    )
    assert "mark completed" in tool_result["content"]
    assert provider.calls == 0
