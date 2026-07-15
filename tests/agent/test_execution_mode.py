from __future__ import annotations

from nanobot.agent.execution_mode import (
    EXECUTION_MODE_DEFAULT,
    EXECUTION_MODE_PLAN_ONLY,
    normalize_execution_mode,
    plan_only_prompt,
    plan_only_registry,
)
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry


class _NamedTool(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return "ok"


def test_execution_mode_normalization_and_prompt() -> None:
    assert normalize_execution_mode(" DEFAULT ") == EXECUTION_MODE_DEFAULT
    assert normalize_execution_mode("plan_only") == EXECUTION_MODE_PLAN_ONLY
    assert normalize_execution_mode("execute_everything") is None

    original = "Refactor the service"
    assert plan_only_prompt(original, {}) == original
    prompted = plan_only_prompt(original, {"execution_mode": "plan_only"})
    assert "[Plan-only mode]" in prompted
    assert "do not confirm it" in prompted
    assert prompted.endswith(original)


def test_plan_only_registry_excludes_execution_tools() -> None:
    registry = ToolRegistry()
    for name in ["plan", "read_file", "grep", "exec", "write_file"]:
        registry.register(_NamedTool(name))

    selected = plan_only_registry(registry)

    assert selected.tool_names == ["plan", "read_file", "grep"]
    assert registry.has("exec")
    assert registry.has("write_file")
