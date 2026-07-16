"""Regression coverage for explicit per-turn Skill routing and hot toggles."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.skills import SkillsLoader, handle_runtime_control
from nanobot.bus.events import (
    INBOUND_META_RUNTIME_CONTROL,
    RUNTIME_CONTROL_ACK,
    RUNTIME_CONTROL_SKILLS_RELOAD,
    InboundMessage,
)


def _write_skill(workspace: Path, name: str) -> None:
    path = workspace / "skills" / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} instructions\n---\n\n# {name}\n\nUse this workflow.",
        encoding="utf-8",
    )


def test_selected_skill_is_loaded_and_suppresses_auto_selection(tmp_path: Path) -> None:
    _write_skill(tmp_path, "alpha")
    _write_skill(tmp_path, "beta")
    context = ContextBuilder(tmp_path)
    context.skills = SkillsLoader(tmp_path, builtin_skills_dir=tmp_path / "builtin")

    prompt = context.build_system_prompt(skill_names=["alpha"])

    assert "# Selected Skills" in prompt
    assert "### Skill: alpha" in prompt
    assert "Use this workflow." in prompt
    assert "### Skill: beta" not in prompt
    assert "# Skills" not in prompt


def test_selected_skill_ignores_disabled_or_unknown_names(tmp_path: Path) -> None:
    _write_skill(tmp_path, "alpha")
    loader = SkillsLoader(
        tmp_path,
        builtin_skills_dir=tmp_path / "builtin",
        disabled_skills={"alpha"},
    )

    assert loader.selected_available_names({"selected_skills": ["alpha", "missing", "alpha"]}) == []


@pytest.mark.asyncio
async def test_skill_runtime_reload_updates_main_and_subagent_loaders(monkeypatch: pytest.MonkeyPatch) -> None:
    config = SimpleNamespace(agents=SimpleNamespace(defaults=SimpleNamespace(disabled_skills=["alpha"])))
    monkeypatch.setattr("nanobot.agent.skills.load_config", lambda: config)
    state = SimpleNamespace(
        context=SimpleNamespace(skills=SimpleNamespace(disabled_skills=set())),
        subagents=SimpleNamespace(disabled_skills=set()),
    )
    ack: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
    message = InboundMessage(
        channel="system",
        sender_id="test",
        chat_id="runtime",
        content=RUNTIME_CONTROL_SKILLS_RELOAD,
        metadata={
            INBOUND_META_RUNTIME_CONTROL: RUNTIME_CONTROL_SKILLS_RELOAD,
            RUNTIME_CONTROL_ACK: ack,
        },
    )

    assert await handle_runtime_control(state, message) is True
    assert state.context.skills.disabled_skills == {"alpha"}
    assert state.subagents.disabled_skills == {"alpha"}
    assert ack.result()["requires_restart"] is False
