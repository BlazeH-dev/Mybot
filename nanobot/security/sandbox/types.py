"""Typed sandbox state shared by providers, policy, status, and trace."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
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


NetworkMode = Literal["denied", "approved_domains", "unrestricted"]


@dataclass(frozen=True, slots=True)
class SandboxStatus:
    mode: SandboxMode
    provider: str
    enforced: bool
    available: bool
    reason: str | None
    writable_roots: tuple[str, ...] = ()
    readable_roots: tuple[str, ...] = ()
    network: NetworkMode = "denied"
    uncovered_processes: tuple[str, ...] = (
        "gateway",
        "channels",
        "preconfigured_stdio_mcp",
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
    writable_roots: tuple[str, ...] = ()
    readable_roots: tuple[str, ...] = ()
    network_domains: tuple[str, ...] = ()
    network_ports: tuple[int, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)

    def summary(self) -> dict[str, object]:
        return {
            "cwd": self.cwd,
            "mode": self.mode.value,
            "provider": self.provider,
            "enforced": self.enforced,
            "command_hash": self.command_hash,
            "writable_roots": list(self.writable_roots),
            "readable_roots": list(self.readable_roots),
            "network_domains": list(self.network_domains),
            "network_ports": list(self.network_ports),
            **self.metadata,
        }


class SandboxUnavailableError(RuntimeError):
    """Raised when a restricted launch cannot be OS-enforced."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
