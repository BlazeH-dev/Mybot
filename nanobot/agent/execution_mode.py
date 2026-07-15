"""Per-turn execution modes shared by WebUI transport and the agent loop."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from nanobot.agent.tools.registry import ToolRegistry

EXECUTION_MODE_METADATA_KEY = "execution_mode"
EXECUTION_MODE_DEFAULT = "default"
EXECUTION_MODE_PLAN_ONLY = "plan_only"
EXECUTION_MODES = frozenset({EXECUTION_MODE_DEFAULT, EXECUTION_MODE_PLAN_ONLY})

PLAN_ONLY_TOOL_NAMES = (
    "plan",
    "read_file",
    "list_dir",
    "find_files",
    "grep",
    "web_fetch",
    "web_search",
)


def normalize_execution_mode(value: Any) -> str | None:
    """Return a supported execution mode, or ``None`` for invalid input."""
    if not isinstance(value, str):
        return None
    mode = value.strip().lower()
    return mode if mode in EXECUTION_MODES else None


def execution_mode_from_metadata(metadata: Mapping[str, Any] | None) -> str | None:
    if not metadata:
        return None
    return normalize_execution_mode(metadata.get(EXECUTION_MODE_METADATA_KEY))


def plan_only_prompt(content: str, metadata: Mapping[str, Any] | None) -> str:
    """Add a user-message-local planning contract without changing the system prefix."""
    if execution_mode_from_metadata(metadata) != EXECUTION_MODE_PLAN_ONLY:
        return content
    return (
        "[Plan-only mode]\n"
        "Inspect the workspace with read-only tools when useful. Create or replace one "
        "structured plan with the plan tool, but do not confirm it, execute it, modify files, "
        "run commands, or perform external side effects. Your final response must present only "
        "the proposed plan and ask the user to execute or revise it.\n\n"
        f"User request:\n{content}"
    )


def plan_only_registry(registry: ToolRegistry) -> ToolRegistry:
    """Build a registry view containing only planning-safe, read-only tools."""
    selected = ToolRegistry()
    for name in PLAN_ONLY_TOOL_NAMES:
        tool = registry.get(name)
        if tool is not None:
            selected.register(tool)
    return selected
