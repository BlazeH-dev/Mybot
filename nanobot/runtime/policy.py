"""Deterministic allow/ask/deny policy over normalized tool calls."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from nanobot.agent.tools.base import Tool
from nanobot.security.sandbox import ApprovalPolicy, SandboxMode
from nanobot.security.workspace_access import WorkspaceScope
from nanobot.security.workspace_policy import is_path_within

PolicyAction = Literal["allow", "ask", "deny"]


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    action: PolicyAction
    reason: str
    matched_rules: tuple[str, ...]
    risk_level: str
    target: str | None = None
    hard_deny: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["matched_rules"] = list(self.matched_rules)
        return payload


@dataclass(frozen=True, slots=True)
class PolicyGateOutcome:
    decision: PermissionDecision
    interaction: dict[str, Any] | None = None
    execution_context: dict[str, Any] | None = None


_READ_TOOLS = frozenset({
    "read_file",
    "list_dir",
    "find_files",
    "grep",
    "plan",
    "list_exec_sessions",
})
_WRITE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})
_EXTERNAL_SIDE_EFFECT_TOOLS = frozenset({"message", "cron"})
_HIGH_RISK_COMMAND = re.compile(
    r"(?:^|[;&|]\s*)(?:sudo|git\s+(?:commit|checkout|switch|rebase|reset|clean)|"
    r"rm\b|mv\b|cp\b|chmod\b|chown\b|pip\s+install|npm\s+(?:install|publish)|"
    r"curl\b|wget\b|ssh\b|scp\b)",
    re.IGNORECASE,
)
_PROTECTED_NAMES = frozenset({
    ".git",
    "credentials",
    ".ssh",
    ".aws",
    ".kube",
})


def sandbox_mode_for_scope(scope: WorkspaceScope | None, *, plan_only: bool = False) -> SandboxMode:
    if plan_only:
        return SandboxMode.READ_ONLY
    if scope is not None and scope.access_mode == "full":
        return SandboxMode.DANGER_FULL_ACCESS
    return SandboxMode.WORKSPACE_WRITE


class PolicyEngine:
    def __init__(
        self,
        *,
        approval_policy: ApprovalPolicy = ApprovalPolicy.ON_REQUEST,
        audit_path: str | Path | None = None,
    ) -> None:
        self.approval_policy = approval_policy
        self.audit_path = Path(audit_path) if audit_path is not None else None

    def evaluate(
        self,
        *,
        tool: Tool,
        params: dict[str, Any],
        scope: WorkspaceScope | None,
        sandbox_mode: SandboxMode,
        task_id: str | None = None,
        plan_hash: str | None = None,
        child_id: str | None = None,
    ) -> PermissionDecision:
        del task_id, plan_hash, child_id
        hard = self._hard_boundary(tool.name, params, scope)
        if hard is not None:
            return self._record(hard)

        capability = getattr(tool, "capability", "") or self._capability(tool.name)
        risk = getattr(tool, "risk_level", "") or self._risk(tool.name, params)
        target = self._target(tool.name, params)

        if sandbox_mode == SandboxMode.READ_ONLY and not tool.read_only and tool.name != "plan":
            return self._record(PermissionDecision(
                action="deny",
                reason="plan-only/read-only mode does not permit side effects",
                matched_rules=("sandbox.read_only",),
                risk_level=risk,
                target=target,
            ))
        if tool.name in _EXTERNAL_SIDE_EFFECT_TOOLS or capability in {
            "external_write",
            "message_send",
            "remote_call",
            "remote_write",
        }:
            return self._ask_or_deny(
                reason="external side effect requires parameter-bound approval",
                rule="external_side_effect.ask",
                risk=risk,
                target=target,
            )
        if tool.name in _WRITE_TOOLS:
            existing = self._targets_existing(tool.name, params, scope)
            if existing and sandbox_mode != SandboxMode.DANGER_FULL_ACCESS:
                return self._ask_or_deny(
                    reason="modifying an existing local file requires approval in Default Permission",
                    rule="local_existing_write.ask",
                    risk="high",
                    target=target,
                )
            return self._record(PermissionDecision(
                action="allow",
                reason="workspace file operation allowed; OCC remains mandatory",
                matched_rules=("workspace_file.allow", "file_occ.required"),
                risk_level=risk,
                target=target,
            ))
        if tool.name == "exec":
            command = str(params.get("command") or params.get("cmd") or "")
            if sandbox_mode != SandboxMode.DANGER_FULL_ACCESS and _HIGH_RISK_COMMAND.search(command):
                return self._ask_or_deny(
                    reason="high-risk local command requires approval in Default Permission",
                    rule="local_shell_high_risk.ask",
                    risk="high",
                    target=target,
                )
            return self._record(PermissionDecision(
                action="allow",
                reason="local command is allowed inside the selected sandbox",
                matched_rules=("local_shell.allow",),
                risk_level=risk,
                target=target,
            ))
        return self._record(PermissionDecision(
            action="allow",
            reason="tool is allowed by the default policy",
            matched_rules=("default.allow",),
            risk_level=risk,
            target=target,
        ))

    def _hard_boundary(
        self,
        tool_name: str,
        params: dict[str, Any],
        scope: WorkspaceScope | None,
    ) -> PermissionDecision | None:
        workspace = scope.project_path if scope is not None else None
        for raw in self._path_values(tool_name, params):
            path = Path(raw).expanduser()
            if not path.is_absolute() and workspace is not None:
                path = workspace / path
            path = path.resolve(strict=False)
            runtime_control = any(
                is_path_within(path, control)
                for control in (
                    (workspace / ".nanobot-runtime" / "interactions") if workspace else Path("/__none__"),
                    (workspace / ".nanobot-runtime" / "checkpoints") if workspace else Path("/__none__"),
                    (workspace / ".nanobot-runtime" / "trace") if workspace else Path("/__none__"),
                )
            )
            credential_config = path == (Path.home() / ".nanobot" / "config.json").resolve(
                strict=False
            )
            if any(part in _PROTECTED_NAMES for part in path.parts) or runtime_control or credential_config:
                return PermissionDecision(
                    action="deny",
                    reason="protected runtime, VCS, or credential path is a hard boundary",
                    matched_rules=("hard.protected_path",),
                    risk_level="critical",
                    target=str(path),
                    hard_deny=True,
                )
            if scope is not None and scope.restrict_to_workspace and not is_path_within(
                path,
                scope.project_path,
            ):
                return PermissionDecision(
                    action="deny",
                    reason="path resolves outside the current workspace",
                    matched_rules=("hard.workspace_escape",),
                    risk_level="critical",
                    target=str(path),
                    hard_deny=True,
                )
        return None

    def _ask_or_deny(
        self,
        *,
        reason: str,
        rule: str,
        risk: str,
        target: str | None,
    ) -> PermissionDecision:
        action: PolicyAction = (
            "ask" if self.approval_policy == ApprovalPolicy.ON_REQUEST else "deny"
        )
        matched = rule if action == "ask" else f"{rule}.approval_policy_never"
        return self._record(PermissionDecision(
            action=action,
            reason=reason,
            matched_rules=(matched,),
            risk_level=risk,
            target=target,
        ))

    def _record(self, decision: PermissionDecision) -> PermissionDecision:
        if self.audit_path is None:
            return decision
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        row = {"timestamp": datetime.now().astimezone().isoformat(), **decision.as_dict()}
        fd = os.open(self.audit_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        return decision

    @staticmethod
    def _capability(tool_name: str) -> str:
        if tool_name in _READ_TOOLS:
            return "local_read"
        if tool_name in _WRITE_TOOLS:
            return "local_write"
        if tool_name == "exec":
            return "process_execute"
        if tool_name.startswith("mcp_"):
            return "remote_call"
        return "tool_call"

    @staticmethod
    def _risk(tool_name: str, params: dict[str, Any]) -> str:
        if tool_name in _READ_TOOLS:
            return "low"
        if tool_name == "exec" and _HIGH_RISK_COMMAND.search(
            str(params.get("command") or params.get("cmd") or "")
        ):
            return "high"
        if tool_name in _WRITE_TOOLS or tool_name in _EXTERNAL_SIDE_EFFECT_TOOLS:
            return "high"
        return "medium"

    @staticmethod
    def _path_values(tool_name: str, params: dict[str, Any]) -> list[str]:
        values: list[str] = []
        if tool_name == "apply_patch":
            for edit in params.get("edits") or []:
                if isinstance(edit, dict) and isinstance(edit.get("path"), str):
                    values.append(edit["path"])
        else:
            for key in ("path", "working_dir", "workdir"):
                if isinstance(params.get(key), str):
                    values.append(params[key])
        return values

    @classmethod
    def _target(cls, tool_name: str, params: dict[str, Any]) -> str | None:
        paths = cls._path_values(tool_name, params)
        if paths:
            return ", ".join(paths[:4])
        if tool_name == "exec":
            return str(params.get("command") or params.get("cmd") or "")[:500]
        if tool_name == "message":
            return str(params.get("target") or params.get("channel") or "message")
        return None

    @classmethod
    def _targets_existing(
        cls,
        tool_name: str,
        params: dict[str, Any],
        scope: WorkspaceScope | None,
    ) -> bool:
        workspace = scope.project_path if scope is not None else Path.cwd()
        for raw in cls._path_values(tool_name, params):
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = workspace / path
            if path.resolve(strict=False).exists():
                return True
        return False
