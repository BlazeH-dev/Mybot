from __future__ import annotations

import json
from pathlib import Path

from nanobot.agent.context import ContextBuilder
from nanobot.agent.tools.context import RequestContext, ToolContext
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.plan import PlanTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.session.manager import SessionManager
from nanobot.session.plan_state import PLAN_STATE_KEY, plan_state_runtime_lines


def _make_tool(
    tmp_path: Path,
    metadata: dict | None = None,
) -> tuple[PlanTool, SessionManager]:
    sessions = SessionManager(tmp_path)
    tool = PlanTool(tmp_path, sessions)
    tool.set_context(
        RequestContext(
            channel="websocket",
            chat_id="chat-plan",
            message_id="msg-1",
            session_key="websocket:chat-plan",
            metadata=metadata or {},
        )
    )
    return tool, sessions


async def test_webui_default_mode_auto_activates_plan(tmp_path: Path) -> None:
    tool, _ = _make_tool(tmp_path, {"execution_mode": "default"})

    created = json.loads(
        await tool.execute(
            action="create",
            task_id="task_auto",
            goal="Implement the feature",
            steps=_steps(),
        )
    )

    assert created["plan"]["status"] == "active"
    assert created["plan"]["approved_plan_hash"] == created["plan"]["plan_hash"]
    assert created["plan"]["approval"]["mode"] == "automatic"
    assert created["next_action"].startswith("Begin execution")


async def test_plan_only_mode_waits_and_cannot_confirm_same_turn(tmp_path: Path) -> None:
    tool, _ = _make_tool(tmp_path, {"execution_mode": "plan_only"})

    created = json.loads(
        await tool.execute(
            action="create",
            task_id="task_plan_only",
            goal="Propose the implementation",
            steps=_steps(),
        )
    )

    assert created["plan"]["status"] == "awaiting_confirmation"
    blocked = await tool.execute(
        action="confirm",
        task_id="task_plan_only",
        expected_plan_hash=created["plan"]["plan_hash"],
    )
    assert blocked.startswith("Error: plan-only mode cannot confirm")


def _steps() -> list[dict]:
    return [
        {
            "id": "inspect",
            "description": "Inspect the workbook",
            "expected_artifacts": ["workbook_schema.json"],
            "depends_on": [],
        },
        {
            "id": "render",
            "description": "Render the Office packet",
            "expected_artifacts": ["weekly_report.docx"],
            "depends_on": ["inspect"],
        },
    ]


def test_plan_tool_is_statically_discovered_and_registered(tmp_path: Path) -> None:
    sessions = SessionManager(tmp_path)
    registry = ToolRegistry()
    ctx = ToolContext(config=None, workspace=str(tmp_path), sessions=sessions)

    registered = ToolLoader().load(ctx, registry)

    assert "plan" in registered
    assert registry.has("plan")
    definitions = registry.get_definitions()
    plan_schema = next(item for item in definitions if item["function"]["name"] == "plan")
    assert plan_schema["function"]["parameters"]["properties"]["action"]["enum"] == [
        "create",
        "get",
        "confirm",
        "update_step",
        "complete",
    ]
    assert registry.get_definitions() is definitions


async def test_plan_tool_requires_confirmation_and_tracks_dependencies(tmp_path: Path) -> None:
    tool, sessions = _make_tool(tmp_path)

    created = json.loads(
        await tool.execute(
            action="create",
            task_id="task_weekly",
            goal="Generate the weekly report and deck",
            constraints={"pptx_max_pages": 6},
            steps=_steps(),
        )
    )
    plan = created["plan"]
    plan_path = Path(created["path"])
    assert plan["status"] == "awaiting_confirmation"
    assert plan["plan_hash"]
    assert plan_path.exists()

    blocked = await tool.execute(
        action="update_step",
        task_id="task_weekly",
        step_id="inspect",
        status="in_progress",
    )
    assert blocked.startswith("Error: plan is not active")

    stale = await tool.execute(
        action="confirm",
        task_id="task_weekly",
        expected_plan_hash="stale",
    )
    assert stale.startswith("Error: plan hash mismatch")

    confirmed = json.loads(
        await tool.execute(
            action="confirm",
            task_id="task_weekly",
            expected_plan_hash=plan["plan_hash"],
        )
    )
    assert confirmed["plan"]["status"] == "active"
    assert confirmed["plan"]["approval"]["message_id"] == "msg-1"

    dependency_blocked = await tool.execute(
        action="update_step",
        task_id="task_weekly",
        step_id="render",
        status="in_progress",
    )
    assert "incomplete dependencies" in dependency_blocked

    for step_id, status in [
        ("inspect", "in_progress"),
        ("inspect", "done"),
        ("render", "in_progress"),
        ("render", "done"),
    ]:
        result = await tool.execute(
            action="update_step",
            task_id="task_weekly",
            step_id=step_id,
            status=status,
        )
        assert not result.startswith("Error:")

    missing = await tool.execute(action="complete", task_id="task_weekly")
    assert "planned artifacts are missing" in missing

    artifact_root = plan_path.parent
    (artifact_root / "workbook_schema.json").write_text("{}\n", encoding="utf-8")
    (artifact_root / "weekly_report.docx").write_bytes(b"docx")
    completed = json.loads(await tool.execute(action="complete", task_id="task_weekly"))
    assert completed["plan"]["status"] == "completed"
    assert completed["verification"]["passed"] is True

    session = sessions.get_or_create("websocket:chat-plan")
    assert session.metadata[PLAN_STATE_KEY]["status"] == "completed"


async def test_plan_tool_runtime_context_is_compact_and_dynamic(tmp_path: Path) -> None:
    tool, sessions = _make_tool(tmp_path)
    created = json.loads(
        await tool.execute(
            action="create",
            task_id="task_context",
            goal="Build a grounded Office packet",
            steps=_steps(),
        )
    )
    session = sessions.get_or_create("websocket:chat-plan")

    lines = plan_state_runtime_lines(session.metadata)

    assert lines[0].startswith("Plan: task_context (awaiting_confirmation")
    assert any("Plan goal: Build a grounded Office packet" in line for line in lines)
    assert any("wait" in line.lower() for line in lines)
    assert created["plan"]["plan_hash"][:16] in lines[0]

    messages = ContextBuilder(tmp_path).build_messages(
        history=[],
        current_message="Continue the task",
        channel="websocket",
        chat_id="chat-plan",
        session_metadata=session.metadata,
    )
    assert "task_context" not in messages[0]["content"]
    assert "Plan: task_context" in messages[-1]["content"]


async def test_plan_tool_rejects_unsafe_artifact_paths(tmp_path: Path) -> None:
    tool, _sessions = _make_tool(tmp_path)
    created = json.loads(
        await tool.execute(
            action="create",
            task_id="task_unsafe",
            goal="Unsafe artifact test",
            steps=[
                {
                    "id": "one",
                    "description": "Try unsafe artifact",
                    "expected_artifacts": ["../../outside.txt"],
                    "depends_on": [],
                }
            ],
        )
    )
    await tool.execute(
        action="confirm",
        task_id="task_unsafe",
        expected_plan_hash=created["plan"]["plan_hash"],
    )
    await tool.execute(
        action="update_step",
        task_id="task_unsafe",
        step_id="one",
        status="done",
    )

    result = await tool.execute(action="complete", task_id="task_unsafe")

    assert "missing or unsafe" in result


async def test_plan_tool_rejects_symlinked_task_directory(tmp_path: Path) -> None:
    tool, _sessions = _make_tool(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    task_link = tmp_path / ".nanobot-runtime" / "artifacts" / "task_link"
    task_link.parent.mkdir(parents=True)
    task_link.symlink_to(outside, target_is_directory=True)

    result = await tool.execute(
        action="create",
        task_id="task_link",
        goal="Do not escape",
        steps=_steps(),
    )

    assert result == "Error: plan artifact directory resolves outside the workspace"
