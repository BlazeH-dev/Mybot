"""Typed sandbox state shared by providers, policy, status, and trace."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal


class SandboxMode(StrEnum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    DANGER_FULL_ACCESS = "danger_full_access"


class ApprovalPolicy(StrEnum):
    ON_REQUEST = "on_request"
    NEVER = "never"


class ApprovalsReviewer(StrEnum):
    USER = "user"


NetworkMode = Literal["unrestricted"]
SandboxEnforcement = Literal["full", "none"]


def command_hash(command: str) -> str:
    """Return the stable digest used to identify one process launch."""
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SandboxExecutionPolicy:
    """Complete file-write policy resolved for one process launch."""

    mode: SandboxMode
    workspace_root: str

    @classmethod
    def resolve(
        cls,
        *,
        mode: SandboxMode,
        workspace: str | Path,
    ) -> "SandboxExecutionPolicy":
        root = Path(workspace).expanduser().resolve(strict=False)
        return cls(mode=mode, workspace_root=str(root))


@dataclass(frozen=True, slots=True)
class SandboxStatus:
    mode: SandboxMode
    provider: str
    enforced: bool
    available: bool
    reason: str | None
    writable_roots: tuple[str, ...] = ()
    readable_roots: tuple[str, ...] = ()
    enforcement: SandboxEnforcement = "none"
    file_write_restricted: bool = False
    file_read_restricted: bool = False
    network_restricted: bool = False
    network: NetworkMode = "unrestricted"
    uncovered_processes: tuple[str, ...] = (
        "stdio_mcp",
        "officecli_internal",
        "gateway",
    )

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["mode"] = self.mode.value
        return payload


@dataclass(frozen=True, slots=True)
class SandboxViolation:
    code: str
    message: str
    target: str | None = None
    hard_deny: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    argv: tuple[str, ...]
    cwd: str
    env: dict[str, str]
    mode: SandboxMode
    provider: str
    enforced: bool
    command_hash: str
    enforcement: SandboxEnforcement = "none"
    writable_roots: tuple[str, ...] = ()
    readable_roots: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)

    def summary(self) -> dict[str, object]:
        return {
            "cwd": self.cwd,
            "mode": self.mode.value,
            "provider": self.provider,
            "enforced": self.enforced,
            "enforcement": self.enforcement,
            "command_hash": self.command_hash,
            "writable_roots": list(self.writable_roots),
            "readable_roots": list(self.readable_roots),
            **self.metadata,
        }


class SandboxUnavailableError(RuntimeError):
    """Raised when a restricted launch cannot be OS-enforced."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
