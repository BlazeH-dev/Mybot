from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.base import ToolSuspensionResult
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.plan import PlanTool
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.runtime.artifacts import ArtifactStore
from nanobot.runtime.checkpoint import CheckpointStore
from nanobot.runtime.plan_scheduler import PlanGraphError, PlanScheduler, contract_hash
from nanobot.session.manager import SessionManager


class StubProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return LLMResponse(content="done", finish_reason="stop")

    def get_default_model(self) -> str:
        return "fake"


class FakeSubagents:
    max_concurrent_subagents = 2

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get_running_count(self) -> int:
        return 0

    def get_running_task_ids(self) -> set[str]:
        return set()

    async def spawn(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return f"{len(self.calls):08x}"


def _context(*, mode: str = "default") -> RequestContext:
    return RequestContext(
        channel="websocket",
        chat_id="chat",
        message_id="turn",
        session_key="websocket:chat",
        metadata={"execution_mode": mode},
    )


def test_scheduler_rejects_cycles_and_builds_parallel_layers() -> None:
    steps = PlanScheduler.normalize_steps([
        {"id": "a", "description": "A"},
        {"id": "b", "description": "B"},
        {"id": "c", "description": "C", "depends_on": ["a", "b"]},
    ])
    assert PlanScheduler.topological_layers(steps) == [["a", "b"], ["c"]]

    with pytest.raises(PlanGraphError) as exc_info:
        PlanScheduler.normalize_steps([
            {"id": "a", "description": "A", "depends_on": ["b"]},
            {"id": "b", "description": "B", "depends_on": ["a"]},
        ])
    assert exc_info.value.code == "dependency_cycle"


def test_failed_branch_blocks_only_its_descendants() -> None:
    plan = {
        "status": "active",
        "steps": PlanScheduler.normalize_steps([
            {"id": "failed-root", "description": "Fails"},
            {"id": "independent", "description": "Keeps running"},
            {
                "id": "dependent",
                "description": "Blocked successor",
                "depends_on": ["failed-root"],
            },
        ]),
    }
    PlanScheduler.refresh(plan)
    PlanScheduler.transition(plan, "failed-root", "failed", error="boom")
    by_id = {step["id"]: step for step in plan["steps"]}
    assert by_id["dependent"]["status"] == "blocked"
    assert by_id["independent"]["status"] == "ready"


def test_retry_failed_clears_attempt_binding_and_unblocks_descendants() -> None:
    plan = {
        "status": "active",
        "steps": PlanScheduler.normalize_steps([
            {"id": "research", "description": "Research", "executor": "child"},
            {
                "id": "write",
                "description": "Write",
                "depends_on": ["research"],
            },
        ]),
    }
    PlanScheduler.refresh(plan)
    PlanScheduler.transition(plan, "research", "running", child_id="deadbeef")
    PlanScheduler.transition(plan, "research", "failed", error="transient failure")

    assert PlanScheduler.retry_failed(plan) == ["research"]

    by_id = {step["id"]: step for step in plan["steps"]}
    assert by_id["research"]["status"] == "ready"
    assert by_id["research"]["retry_count"] == 1
    assert by_id["research"]["attempt_history"][0]["child_id"] == "deadbeef"
    assert "child_id" not in by_id["research"]
    assert "error" not in by_id["research"]
    assert by_id["write"]["status"] == "pending"


def test_running_child_without_dispatch_evidence_returns_to_ready() -> None:
    plan = {
        "status": "active",
        "steps": PlanScheduler.normalize_steps([{
            "id": "child",
            "description": "Dispatch child",
            "executor": "child",
            "status": "running",
        }]),
    }
    recovery = PlanScheduler.recover_running(plan)
    assert recovery.uncertain == ()
    assert plan["steps"][0]["status"] == "ready"


@pytest.mark.asyncio
async def test_plan_create_generates_current_markdown_artifact(tmp_path: Path) -> None:
    tool = PlanTool(tmp_path, SessionManager(tmp_path), MessageBus())
    tool.set_context(_context(mode="plan_only"))
    result = await tool.execute(
        action="create",
        task_id="dag-plan",
        goal="Build and verify",
        steps=[
            {"id": "build", "description": "Build output", "executor": "child"},
            {
                "id": "verify",
                "description": "Verify output",
                "depends_on": ["build"],
            },
        ],
    )
    assert isinstance(result, ToolSuspensionResult)
    payload = json.loads(result)
    plan = payload["plan"]
    assert plan["schema_version"] == 2
    assert plan["revision"] == 1
    markdown = Path(plan["plan_markdown"]["path"])
    assert markdown.name == "plan.md"
    content = markdown.read_text(encoding="utf-8")
    assert "## Summary" in content
    assert "## 核心改造" in content
    assert "## 执行步骤" in content
    assert "## DAG 依赖与并行安排" in content
    assert "## 输入、产物与血缘" in content
    assert "## 风险、审批与恢复" in content
    assert "## 验收标准" in content
    assert "`child`" in content
    record = ArtifactStore(tmp_path).get("dag-plan", "plan_markdown")
    assert record.metadata["plan_hash"] == plan["plan_hash"]


@pytest.mark.asyncio
async def test_complete_finishes_after_deterministic_checks(tmp_path: Path) -> None:
    tool = PlanTool(tmp_path, SessionManager(tmp_path))
    tool.set_context(_context())
    await tool.execute(
        action="create",
        task_id="complete-plan",
        goal="Produce a checked result",
        steps=[{"id": "work", "description": "Do the work"}],
    )
    await tool.execute(
        action="update_step",
        task_id="complete-plan",
        step_id="work",
        status="succeeded",
        result_summary="done",
    )
    completed = json.loads(await tool.execute(action="complete", task_id="complete-plan"))
    assert completed["plan"]["status"] == "completed"
    assert completed["verification"] == {"passed": True, "missing": []}
    assert "reflection" not in completed["plan"]


@pytest.mark.asyncio
async def test_ready_child_nodes_are_dispatched_in_parallel(tmp_path: Path) -> None:
    subagents = FakeSubagents()
    tool = PlanTool(
        tmp_path,
        SessionManager(tmp_path),
        subagent_manager=subagents,
    )
    tool.set_context(_context())
    created = json.loads(await tool.execute(
        action="create",
        task_id="child-plan",
        goal="Run independent child work",
        steps=[
            {"id": "one", "description": "First", "executor": "child"},
            {"id": "two", "description": "Second", "executor": "child"},
        ],
    ))
    assert created["dispatched_steps"] == ["one", "two"]
    assert len(subagents.calls) == 2
    assert all(call["parent_plan_hash"] == created["plan"]["plan_hash"] for call in subagents.calls)
    assert {step["status"] for step in created["plan"]["steps"]} == {"running"}
    checkpoint = CheckpointStore(tmp_path).load(
        "child-plan",
        expected_plan_hash=created["plan"]["plan_hash"],
    )
    assert checkpoint["schema_version"] == 2
    assert set(checkpoint["uncertain_nodes"]) == {"one", "two"}


@pytest.mark.asyncio
async def test_resume_retries_failed_child_without_plan_revision(tmp_path: Path) -> None:
    subagents = FakeSubagents()
    tool = PlanTool(
        tmp_path,
        SessionManager(tmp_path),
        subagent_manager=subagents,
    )
    tool.set_context(_context())
    created = json.loads(await tool.execute(
        action="create",
        task_id="resume-child-plan",
        goal="Retry interrupted research",
        steps=[{"id": "research", "description": "Research", "executor": "child"}],
    ))
    plan_hash = created["plan"]["plan_hash"]
    await tool.execute(
        action="update_step",
        task_id="resume-child-plan",
        step_id="research",
        status="failed",
        error="temporary search failure",
    )

    resumed = json.loads(await tool.execute(
        action="resume",
        task_id="resume-child-plan",
        expected_plan_hash=plan_hash,
    ))

    assert resumed["plan"]["revision"] == 1
    assert resumed["plan"]["plan_hash"] == plan_hash
    assert resumed["retried_steps"] == ["research"]
    assert resumed["dispatched_steps"] == ["research"]
    assert resumed["plan"]["steps"][0]["status"] == "running"
    assert resumed["plan"]["steps"][0]["retry_count"] == 1
    assert len(subagents.calls) == 2


@pytest.mark.asyncio
async def test_failed_child_in_progress_shorthand_retries_and_dispatches(tmp_path: Path) -> None:
    subagents = FakeSubagents()
    tool = PlanTool(
        tmp_path,
        SessionManager(tmp_path),
        subagent_manager=subagents,
    )
    tool.set_context(_context())
    await tool.execute(
        action="create",
        task_id="retry-shorthand-plan",
        goal="Retry failed child",
        steps=[{"id": "research", "description": "Research", "executor": "child"}],
    )
    await tool.execute(
        action="update_step",
        task_id="retry-shorthand-plan",
        step_id="research",
        status="failed",
        error="temporary failure",
    )

    result = json.loads(await tool.execute(
        action="update_step",
        task_id="retry-shorthand-plan",
        step_id="research",
        status="in_progress",
        result_summary="Resume after interruption",
    ))

    assert result["dispatched_steps"] == ["research"]
    assert result["plan"]["steps"][0]["status"] == "running"
    assert result["plan"]["steps"][0]["retry_count"] == 1
    assert len(subagents.calls) == 2


@pytest.mark.asyncio
async def test_cancelled_child_returns_to_ready_without_automatic_redispatch(tmp_path: Path) -> None:
    subagents = FakeSubagents()
    sessions = SessionManager(tmp_path)
    tool = PlanTool(tmp_path, sessions, subagent_manager=subagents)
    tool.set_context(_context())
    created = json.loads(await tool.execute(
        action="create",
        task_id="cancelled-child-plan",
        goal="Pause child work",
        steps=[{"id": "research", "description": "Research", "executor": "child"}],
    ))
    callback = subagents.calls[0]["completion_callback"]

    await callback(SimpleNamespace(
        task_id="00000001",
        phase="error",
        stop_reason="cancelled",
        final_result="Subagent cancelled.",
        artifact_root=None,
    ))

    plan = json.loads(Path(created["path"]).read_text(encoding="utf-8"))
    assert plan["steps"][0]["status"] == "ready"
    assert plan["steps"][0]["interruption_reason"] == "child_cancelled"
    assert "child_id" not in plan["steps"][0]
    assert len(subagents.calls) == 1


@pytest.mark.asyncio
async def test_resume_recovers_child_running_without_binding_and_dispatches_it(tmp_path: Path) -> None:
    subagents = FakeSubagents()
    sessions = SessionManager(tmp_path)
    tool = PlanTool(tmp_path, sessions, subagent_manager=subagents)
    tool.set_context(_context())
    created = json.loads(await tool.execute(
        action="create",
        task_id="orphan-child-plan",
        goal="Recover interrupted child dispatch",
        steps=[{"id": "research", "description": "Research", "executor": "child"}],
    ))
    plan_hash = created["plan"]["plan_hash"]
    path = Path(created["path"])
    plan = json.loads(path.read_text(encoding="utf-8"))
    plan["steps"][0]["status"] = "running"
    plan["steps"][0].pop("child_id", None)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    session = sessions.get_or_create("websocket:chat")
    session.metadata["plan_state"] = plan
    sessions.save(session)

    resumed = json.loads(await tool.execute(
        action="resume",
        task_id="orphan-child-plan",
        expected_plan_hash=plan_hash,
    ))

    assert resumed["recovered_steps"] == ["research"]
    assert resumed["dispatched_steps"] == ["research"]
    assert resumed["plan"]["steps"][0]["status"] == "running"
    assert resumed["plan"]["steps"][0]["child_id"] == "00000002"


@pytest.mark.asyncio
async def test_child_running_update_is_idempotent_when_scheduler_binding_exists(tmp_path: Path) -> None:
    subagents = FakeSubagents()
    tool = PlanTool(tmp_path, SessionManager(tmp_path), subagent_manager=subagents)
    tool.set_context(_context())
    await tool.execute(
        action="create",
        task_id="managed-child-plan",
        goal="Keep child binding managed",
        steps=[{"id": "research", "description": "Research", "executor": "child"}],
    )

    result = json.loads(await tool.execute(
        action="update_step",
        task_id="managed-child-plan",
        step_id="research",
        status="running",
    ))

    assert result["dispatched_steps"] == []
    assert result["plan"]["steps"][0]["status"] == "running"
    assert result["plan"]["steps"][0]["child_id"] == "00000001"
    assert len(subagents.calls) == 1


@pytest.mark.asyncio
async def test_plan_markdown_is_replaced_and_only_current_revision_is_kept(tmp_path: Path) -> None:
    tool = PlanTool(tmp_path, SessionManager(tmp_path))
    tool.set_context(_context())
    first = json.loads(await tool.execute(
        action="create",
        task_id="immutable-plan",
        goal="First goal",
        steps=[{"id": "one", "description": "First"}],
    ))
    first_path = Path(first["plan"]["plan_markdown"]["path"])
    first_content = first_path.read_text(encoding="utf-8")

    second = json.loads(await tool.execute(
        action="create",
        task_id="immutable-plan",
        goal="Replacement goal",
        steps=[{"id": "two", "description": "Second"}],
        replace=True,
    ))
    assert second["plan"]["revision"] == 2
    second_path = Path(second["plan"]["plan_markdown"]["path"])
    assert second_path == first_path
    assert second_path.name == "plan.md"
    assert second_path.read_text(encoding="utf-8") != first_content
    assert "Replacement goal" in second_path.read_text(encoding="utf-8")
    records = ArtifactStore(tmp_path).list("immutable-plan")
    assert [record.artifact_id for record in records].count("plan_markdown") == 1
    assert not any(record.artifact_id.startswith("plan_md_r") for record in records)

    second_path.write_text("tampered", encoding="utf-8")
    result = await tool.execute(action="get", task_id="immutable-plan")
    assert "plan_markdown_checksum_mismatch" in result


@pytest.mark.asyncio
async def test_plan_markdown_migrates_legacy_revision_file_and_index(tmp_path: Path) -> None:
    sessions = SessionManager(tmp_path)
    tool = PlanTool(tmp_path, sessions)
    tool.set_context(_context())
    created = json.loads(await tool.execute(
        action="create",
        task_id="legacy-markdown",
        goal="Migrate current plan",
        steps=[{"id": "one", "description": "First"}],
    ))
    plan = created["plan"]
    current_path = Path(plan["plan_markdown"]["path"])
    legacy_path = current_path.with_name("plan-r1.md")
    current_path.replace(legacy_path)
    store = ArtifactStore(tmp_path)
    store.remove("legacy-markdown", {"plan_markdown"})
    legacy_record = store.register(
        task_id="legacy-markdown",
        artifact_id="plan_md_r1",
        path=legacy_path,
        type="plan_markdown",
        source_artifacts=["plan"],
        metadata={"revision": 1, "plan_hash": plan["plan_hash"]},
    )
    plan["plan_markdown"] = {
        "artifact_id": legacy_record.artifact_id,
        "path": str(legacy_path),
        "revision": 1,
        "plan_hash": plan["plan_hash"],
        "checksum": legacy_record.checksum,
    }
    plan_path = Path(created["path"])
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    session = sessions.get_or_create("websocket:chat")
    session.metadata["plan_state"] = plan
    sessions.save(session)

    migrated = json.loads(await tool.execute(action="get", task_id="legacy-markdown"))["plan"]

    assert Path(migrated["plan_markdown"]["path"]).name == "plan.md"
    assert Path(migrated["plan_markdown"]["path"]).is_file()
    assert not legacy_path.exists()
    artifact_ids = {record.artifact_id for record in store.list("legacy-markdown")}
    assert "plan_markdown" in artifact_ids
    assert "plan_md_r1" not in artifact_ids


@pytest.mark.asyncio
async def test_orphaned_child_node_requires_recovery_decision(tmp_path: Path) -> None:
    bus = MessageBus()
    sessions = SessionManager(tmp_path)
    loop = AgentLoop(
        bus=bus,
        provider=StubProvider(),
        workspace=tmp_path,
        session_manager=sessions,
    )
    session = sessions.get_or_create("websocket:chat")
    plan = {
        "schema_version": 2,
        "task_id": "recover-plan",
        "revision": 1,
        "goal": "Recover child work",
        "constraints": {},
        "steps": [{
            "id": "child",
            "description": "Child work",
            "depends_on": [],
            "expected_artifacts": [],
            "executor": "child",
            "status": "running",
            "child_id": "deadbeef",
        }],
        "status": "active",
    }
    plan["plan_hash"] = contract_hash(plan)
    plan["approved_plan_hash"] = plan["plan_hash"]
    session.metadata["plan_state"] = plan
    sessions.save(session)

    assert await loop._prepare_uncertain_recovery(
        session,
        channel="websocket",
        chat_id="chat",
        turn_id="turn",
    )
    outbound = await bus.consume_outbound()
    interaction = outbound.metadata["_agent_ui"]["interaction"]
    assert interaction["payload"]["uncertain_node_ids"] == ["child"]
    assert session.metadata["plan_state"]["steps"][0]["status"] == "uncertain"

    answered = loop.interactions.respond(
        interaction["request_id"],
        expected_revision=interaction["revision"],
        idempotency_key="retry-node",
        response={"answer": "Retry"},
    )
    assert answered.status.value == "answered"
    assert loop._materialize_interaction_response(session, interaction["request_id"])
    assert session.metadata["plan_state"]["steps"][0]["status"] == "ready"
