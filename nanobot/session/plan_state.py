"""Session metadata helpers for the static ``plan`` tool."""

from __future__ import annotations

import json
from typing import Any, Mapping

PLAN_STATE_KEY = "plan_state"
_MAX_GOAL_CHARS = 1200
_MAX_STEP_CHARS = 240
_MAX_RUNTIME_STEPS = 12


def parse_plan_state(blob: Any) -> dict[str, Any] | None:
    if blob is None:
        return None
    if isinstance(blob, dict):
        return blob
    if isinstance(blob, str):
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def plan_state_raw(metadata: Mapping[str, Any] | None) -> Any:
    return metadata.get(PLAN_STATE_KEY) if metadata else None


def plan_state_runtime_lines(metadata: Mapping[str, Any] | None) -> list[str]:
    """Return a compact active-plan summary for the dynamic user-message tail."""
    plan = parse_plan_state(plan_state_raw(metadata))
    if not isinstance(plan, dict):
        return []
    status = str(plan.get("status") or "")
    if status not in {"awaiting_confirmation", "active"}:
        return []

    task_id = str(plan.get("task_id") or "unknown")
    plan_hash = str(plan.get("plan_hash") or "")[:16]
    goal = str(plan.get("goal") or "").strip()
    if len(goal) > _MAX_GOAL_CHARS:
        goal = goal[:_MAX_GOAL_CHARS].rstrip() + "…"

    lines = [f"Plan: {task_id} ({status}, hash={plan_hash})"]
    if goal:
        lines.append(f"Plan goal: {goal}")
    steps = plan.get("steps")
    if isinstance(steps, list):
        summaries: list[str] = []
        for step in steps[:_MAX_RUNTIME_STEPS]:
            if not isinstance(step, dict):
                continue
            step_id = str(step.get("id") or "?")
            step_status = str(step.get("status") or "pending")
            description = str(step.get("description") or "").strip()
            if len(description) > _MAX_STEP_CHARS:
                description = description[:_MAX_STEP_CHARS].rstrip() + "…"
            summaries.append(f"[{step_status}] {step_id}: {description}")
        if summaries:
            lines.append("Plan steps: " + " | ".join(summaries))
    if status == "awaiting_confirmation":
        lines.append(
            "Plan is not confirmed. Show it to the user and wait; call plan(action='confirm') "
            "only after explicit user confirmation."
        )
    else:
        lines.append("Keep step state current with the plan tool and complete only after verification.")
    return lines
