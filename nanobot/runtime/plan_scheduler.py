"""Durable DAG plan state, revision, and user-facing Markdown rendering."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Iterable

NODE_STATUSES = (
    "pending",
    "ready",
    "running",
    "succeeded",
    "failed",
    "blocked",
    "uncertain",
    "cancelled",
)
EXECUTORS = ("parent", "child")
SUCCESS_STATUSES = frozenset({"succeeded", "cancelled"})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "uncertain", "cancelled"})

_LEGACY_STATUS_MAP = {
    "in_progress": "running",
    "done": "succeeded",
    "skipped": "cancelled",
}


class PlanGraphError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    completed: tuple[str, ...]
    pending: tuple[str, ...]
    uncertain: tuple[str, ...]


def canonical_contract(plan: dict[str, Any]) -> dict[str, Any]:
    schema_version = int(plan.get("schema_version") or 1)
    steps: list[dict[str, Any]] = []
    for raw in plan.get("steps", []):
        if not isinstance(raw, dict):
            continue
        step = {
            "id": raw.get("id"),
            "description": raw.get("description"),
            "expected_artifacts": raw.get("expected_artifacts", []),
            "depends_on": raw.get("depends_on", []),
        }
        if schema_version >= 2:
            step["executor"] = raw.get("executor", "parent")
        steps.append(step)
    contract: dict[str, Any] = {
        "schema_version": schema_version,
        "task_id": plan.get("task_id"),
        "goal": plan.get("goal"),
        "constraints": plan.get("constraints", {}),
        "steps": steps,
    }
    if schema_version >= 2:
        contract["revision"] = int(plan.get("revision") or 1)
    return contract


def contract_hash(plan: dict[str, Any]) -> str:
    raw = json.dumps(
        canonical_contract(plan),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PlanScheduler:
    """Deterministic DAG state machine; execution adapters live at the Agent boundary."""

    @staticmethod
    def normalize_steps(
        steps: Iterable[Any],
        *,
        initial_status: str = "pending",
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        ids: set[str] = set()
        for raw in steps:
            if not isinstance(raw, dict):
                raise PlanGraphError("invalid_step", "each plan step must be an object")
            step_id = str(raw.get("id") or "").strip()
            description = str(raw.get("description") or "").strip()
            if not step_id or not description:
                raise PlanGraphError(
                    "invalid_step",
                    "each plan step requires non-empty id and description",
                )
            if step_id in ids:
                raise PlanGraphError("duplicate_step", f"duplicate plan step id: {step_id}")
            ids.add(step_id)
            executor = str(raw.get("executor") or "parent").strip().lower()
            if executor not in EXECUTORS:
                raise PlanGraphError(
                    "invalid_executor",
                    f"step {step_id} executor must be one of {list(EXECUTORS)}",
                )
            status = _LEGACY_STATUS_MAP.get(
                str(raw.get("status") or initial_status),
                str(raw.get("status") or initial_status),
            )
            if status not in NODE_STATUSES:
                status = initial_status
            normalized.append({
                "id": step_id,
                "description": description,
                "expected_artifacts": [
                    str(item) for item in raw.get("expected_artifacts", [])
                ],
                "depends_on": [str(item) for item in raw.get("depends_on", [])],
                "executor": executor,
                "status": status,
                **(
                    {"child_id": str(raw["child_id"])}
                    if raw.get("child_id")
                    else {}
                ),
                **(
                    {"result": deepcopy(raw["result"])}
                    if isinstance(raw.get("result"), (dict, list, str))
                    else {}
                ),
                **({"error": str(raw["error"])} if raw.get("error") else {}),
            })
        PlanScheduler.validate_graph(normalized)
        return normalized

    @staticmethod
    def validate_graph(steps: list[dict[str, Any]]) -> None:
        ids = {str(step.get("id")) for step in steps}
        for step in steps:
            step_id = str(step.get("id"))
            dependencies = [str(item) for item in step.get("depends_on", [])]
            if len(dependencies) != len(set(dependencies)):
                raise PlanGraphError(
                    "duplicate_dependency",
                    f"step {step_id} contains duplicate dependencies",
                )
            missing = sorted(set(dependencies) - ids)
            if missing:
                raise PlanGraphError(
                    "unknown_dependency",
                    f"step {step_id} depends on unknown step(s): {missing}",
                )
            if step_id in dependencies:
                raise PlanGraphError(
                    "self_dependency",
                    f"step {step_id} cannot depend on itself",
                )
        PlanScheduler.topological_layers(steps)

    @staticmethod
    def topological_layers(steps: list[dict[str, Any]]) -> list[list[str]]:
        order = [str(step["id"]) for step in steps]
        remaining = {
            str(step["id"]): set(str(item) for item in step.get("depends_on", []))
            for step in steps
        }
        layers: list[list[str]] = []
        resolved: set[str] = set()
        while remaining:
            layer = [step_id for step_id in order if step_id in remaining and not remaining[step_id] - resolved]
            if not layer:
                cycle = sorted(remaining)
                raise PlanGraphError(
                    "dependency_cycle",
                    f"plan dependency graph contains a cycle involving: {cycle}",
                )
            layers.append(layer)
            resolved.update(layer)
            for step_id in layer:
                remaining.pop(step_id, None)
        return layers

    @staticmethod
    def refresh(plan: dict[str, Any]) -> dict[str, Any]:
        steps = [step for step in plan.get("steps", []) if isinstance(step, dict)]
        by_id = {str(step.get("id")): step for step in steps}
        if plan.get("status") != "active":
            return plan
        for step in steps:
            status = _LEGACY_STATUS_MAP.get(str(step.get("status") or "pending"), str(step.get("status") or "pending"))
            step["status"] = status
            if status in {"running", *TERMINAL_STATUSES}:
                continue
            dependencies = [by_id[str(item)] for item in step.get("depends_on", [])]
            if any(dep.get("status") in {"failed", "blocked", "uncertain"} for dep in dependencies):
                step["status"] = "blocked"
            elif all(dep.get("status") in SUCCESS_STATUSES for dep in dependencies):
                step["status"] = "ready"
            else:
                step["status"] = "pending"
        return plan

    @staticmethod
    def ready_steps(plan: dict[str, Any], *, executor: str | None = None) -> list[dict[str, Any]]:
        PlanScheduler.refresh(plan)
        return [
            step
            for step in plan.get("steps", [])
            if isinstance(step, dict)
            and step.get("status") == "ready"
            and (executor is None or step.get("executor") == executor)
        ]

    @staticmethod
    def transition(
        plan: dict[str, Any],
        step_id: str,
        status: str,
        *,
        child_id: str | None = None,
        result: Any = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        mapped = _LEGACY_STATUS_MAP.get(status, status)
        if mapped not in NODE_STATUSES:
            raise PlanGraphError("invalid_status", f"invalid node status: {status}")
        target = next(
            (
                step
                for step in plan.get("steps", [])
                if isinstance(step, dict) and str(step.get("id")) == step_id
            ),
            None,
        )
        if target is None:
            raise PlanGraphError("unknown_step", f"unknown plan step: {step_id}")
        PlanScheduler.refresh(plan)
        current = str(target.get("status") or "pending")
        allowed: dict[str, set[str]] = {
            "pending": {"ready", "cancelled"},
            "ready": {"running", "succeeded", "failed", "cancelled"},
            "running": {"ready", "succeeded", "failed", "uncertain", "cancelled"},
            "failed": {"ready", "cancelled"},
            "blocked": {"pending", "ready", "cancelled"},
            "uncertain": {"succeeded", "failed", "ready", "cancelled"},
            "succeeded": set(),
            "cancelled": set(),
        }
        if mapped != current and mapped not in allowed.get(current, set()):
            raise PlanGraphError(
                "invalid_transition",
                f"step {step_id} cannot transition from {current} to {mapped}",
            )
        if mapped == "ready" and current != "ready":
            for key in ("child_id", "artifact_root", "result", "error"):
                target.pop(key, None)
        target["status"] = mapped
        if child_id:
            target["child_id"] = child_id
        if result is not None:
            target["result"] = deepcopy(result)
        if error:
            target["error"] = error
        elif mapped in {"ready", "running", "succeeded"}:
            target.pop("error", None)
        return PlanScheduler.refresh(plan)

    @staticmethod
    def retry_failed(
        plan: dict[str, Any],
        step_ids: Iterable[str] | None = None,
    ) -> list[str]:
        """Reset explicitly selected failed nodes without changing the plan contract."""

        selected = {str(item) for item in step_ids} if step_ids is not None else None
        retried: list[str] = []
        for step in plan.get("steps", []):
            if not isinstance(step, dict) or step.get("status") != "failed":
                continue
            step_id = str(step.get("id"))
            if selected is not None and step_id not in selected:
                continue
            history = step.setdefault("attempt_history", [])
            if isinstance(history, list):
                history.append({
                    "status": "failed",
                    "child_id": step.get("child_id"),
                    "result": deepcopy(step.get("result")),
                    "error": step.get("error"),
                })
            step["retry_count"] = int(step.get("retry_count") or 0) + 1
            PlanScheduler.transition(plan, step_id, "ready")
            retried.append(step_id)
        PlanScheduler.refresh(plan)
        return retried

    @staticmethod
    def descendants(plan: dict[str, Any], step_ids: Iterable[str]) -> set[str]:
        affected = {str(item) for item in step_ids}
        changed = True
        while changed:
            changed = False
            for step in plan.get("steps", []):
                if not isinstance(step, dict):
                    continue
                step_id = str(step.get("id"))
                if step_id not in affected and affected.intersection(step.get("depends_on", [])):
                    affected.add(step_id)
                    changed = True
        return affected

    @staticmethod
    def revise(
        plan: dict[str, Any],
        *,
        steps: Iterable[Any],
        goal: str | None = None,
        constraints: dict[str, Any] | None = None,
        reason: str | None = None,
        allow_reset_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        old_steps = {
            str(step.get("id")): step
            for step in plan.get("steps", [])
            if isinstance(step, dict)
        }
        normalized = PlanScheduler.normalize_steps(steps)
        new_steps = {str(step["id"]): step for step in normalized}
        reset = set(allow_reset_ids or set())

        removed = set(old_steps) - set(new_steps)
        protected_removed = [
            step_id
            for step_id in removed
            if old_steps[step_id].get("status") in {"running", "succeeded"}
            and step_id not in reset
        ]
        if protected_removed:
            raise PlanGraphError(
                "protected_step_change",
                f"running or succeeded steps cannot be removed: {sorted(protected_removed)}",
            )

        changed_ids: set[str] = set(removed)
        for step_id, step in new_steps.items():
            old = old_steps.get(step_id)
            if old is None:
                changed_ids.add(step_id)
                continue
            old_contract = {
                key: old.get(key, [] if key in {"depends_on", "expected_artifacts"} else "parent")
                for key in ("description", "depends_on", "expected_artifacts", "executor")
            }
            new_contract = {
                key: step.get(key, [] if key in {"depends_on", "expected_artifacts"} else "parent")
                for key in ("description", "depends_on", "expected_artifacts", "executor")
            }
            if old_contract != new_contract:
                changed_ids.add(step_id)
                if old.get("status") in {"running", "succeeded"} and step_id not in reset:
                    raise PlanGraphError(
                        "protected_step_change",
                        f"running or succeeded step cannot be modified: {step_id}",
                    )

        candidate = deepcopy(plan)
        candidate["schema_version"] = 2
        candidate["revision"] = int(plan.get("revision") or 1) + 1
        candidate["goal"] = str(goal if goal is not None else plan.get("goal") or "").strip()
        candidate["constraints"] = deepcopy(
            constraints if constraints is not None else plan.get("constraints") or {}
        )
        candidate["steps"] = normalized
        affected = PlanScheduler.descendants({"steps": normalized}, changed_ids | reset)
        for step in candidate["steps"]:
            old = old_steps.get(str(step["id"]))
            if old is not None and str(step["id"]) not in affected:
                step["status"] = str(old.get("status") or "pending")
                for key in ("child_id", "result", "error"):
                    if key in old:
                        step[key] = deepcopy(old[key])
            else:
                step["status"] = "pending"
        candidate["status"] = "awaiting_revision_confirmation"
        candidate["approved_plan_hash"] = None
        candidate["approval"] = None
        candidate["previous_plan_hash"] = plan.get("plan_hash")
        candidate["revision_reason"] = str(reason or "Plan contract revised.")
        candidate["affected_steps"] = sorted(affected)
        candidate.pop("interaction_request_id", None)
        candidate["plan_hash"] = contract_hash(candidate)
        return candidate

    @staticmethod
    def recovery_summary(plan: dict[str, Any]) -> RecoverySummary:
        completed: list[str] = []
        pending: list[str] = []
        uncertain: list[str] = []
        for step in plan.get("steps", []):
            if not isinstance(step, dict):
                continue
            step_id = str(step.get("id"))
            status = str(step.get("status") or "pending")
            if status in SUCCESS_STATUSES:
                completed.append(step_id)
            elif status == "running":
                if (
                    (step.get("executor") == "child" and step.get("child_id"))
                    or step.get("external_side_effect") is True
                ):
                    uncertain.append(step_id)
                else:
                    pending.append(step_id)
            elif status == "uncertain":
                uncertain.append(step_id)
            else:
                pending.append(step_id)
        return RecoverySummary(tuple(completed), tuple(pending), tuple(uncertain))

    @staticmethod
    def recover_running(
        plan: dict[str, Any],
        *,
        live_child_ids: set[str] | None = None,
        artifacts_verified: Callable[[dict[str, Any]], bool] | None = None,
    ) -> RecoverySummary:
        """Classify orphaned running nodes without replaying uncertain child work."""
        live_children = live_child_ids or set()
        changed = False
        for step in plan.get("steps", []):
            if not isinstance(step, dict) or step.get("status") != "running":
                continue
            if artifacts_verified is not None and artifacts_verified(step):
                step["status"] = "succeeded"
                step["recovered_from"] = "verified_artifacts"
                changed = True
                continue
            if step.get("executor") == "child":
                child_id = str(step.get("child_id") or "")
                if not child_id:
                    step["status"] = "pending"
                    step["recovered_from"] = "interrupted_before_child_dispatch"
                    changed = True
                    continue
                if child_id and child_id in live_children:
                    continue
                step["status"] = "uncertain"
                step["error"] = (
                    "The child execution is no longer present in this Runtime and has no "
                    "complete verified artifact set."
                )
                changed = True
                continue
            if step.get("external_side_effect") is True:
                step["status"] = "uncertain"
                step["error"] = "The interrupted node may have produced an external side effect."
            else:
                step["status"] = "pending"
                step.pop("error", None)
                step["recovered_from"] = "interrupted_parent"
            changed = True
        if changed:
            PlanScheduler.refresh(plan)
        return PlanScheduler.recovery_summary(plan)


def render_plan_markdown(plan: dict[str, Any]) -> str:
    """Render a deterministic user-facing view of the immutable plan contract."""
    layers = PlanScheduler.topological_layers(
        [step for step in plan.get("steps", []) if isinstance(step, dict)]
    )
    constraints = plan.get("constraints") if isinstance(plan.get("constraints"), dict) else {}
    lines = [
        f"# Task Plan: {plan.get('goal') or plan.get('task_id')}",
        "",
        f"- Task ID: `{plan.get('task_id')}`",
        f"- Revision: `{int(plan.get('revision') or 1)}`",
        f"- Plan hash: `{plan.get('plan_hash')}`",
        "",
        "## Summary",
        "",
        str(plan.get("goal") or "No goal provided."),
        "",
        "## 核心改造",
        "",
    ]
    for step in plan.get("steps", []):
        lines.append(f"- {step.get('description')}")
    lines.extend([
        "",
        "## 执行步骤",
        "",
        "| Step | Description | Executor | Depends on | Expected artifacts |",
        "| --- | --- | --- | --- | --- |",
    ])
    for step in plan.get("steps", []):
        deps = ", ".join(f"`{item}`" for item in step.get("depends_on", [])) or "-"
        artifacts = ", ".join(f"`{item}`" for item in step.get("expected_artifacts", [])) or "-"
        description = str(step.get("description") or "").replace("|", "\\|")
        lines.append(
            f"| `{step.get('id')}` | {description} | `{step.get('executor', 'parent')}` | "
            f"{deps} | {artifacts} |"
        )
    lines.extend(["", "## DAG 依赖与并行安排", ""])
    for index, layer in enumerate(layers, start=1):
        lines.append(f"- Batch {index}: " + ", ".join(f"`{item}`" for item in layer))
    lines.extend(["", "## 输入、产物与血缘", ""])
    inputs = plan.get("input_artifacts") if isinstance(plan.get("input_artifacts"), list) else []
    lines.append("- Input artifacts: " + (", ".join(f"`{item}`" for item in inputs) or "none"))
    expected = [
        str(item)
        for step in plan.get("steps", [])
        for item in step.get("expected_artifacts", [])
    ]
    lines.append("- Expected outputs: " + (", ".join(f"`{item}`" for item in expected) or "none"))
    lines.extend(["", "## 风险、审批与恢复", ""])
    if constraints:
        for key in sorted(constraints):
            value = json.dumps(constraints[key], ensure_ascii=False, sort_keys=True)
            lines.append(f"- `{key}`: {value}")
    else:
        lines.append("- Runtime policy, approval, OCC, artifact, and checkpoint rules apply.")
    lines.extend([
        "- Completed nodes with verified artifacts are reused after restart.",
        "- Pending work may be replayed; uncertain external side effects require a user decision.",
        "",
        "## 验收标准",
        "",
        "- Every required DAG node reaches `succeeded` or an explicitly accepted terminal state.",
        "- Every expected artifact resolves to a safe path and exists.",
        "",
    ])
    return "\n".join(lines)
