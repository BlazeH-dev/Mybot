"""OS-enforced sandbox primitives for Agent-triggered subprocesses."""

from nanobot.security.sandbox.launcher import SandboxLauncher
from nanobot.security.sandbox.manager import SandboxManager
from nanobot.security.sandbox.types import (
    ApprovalPolicy,
    ApprovalsReviewer,
    LaunchSpec,
    SandboxExecutionPolicy,
    SandboxMode,
    SandboxStatus,
    SandboxUnavailableError,
    SandboxViolation,
    command_hash,
)

__all__ = [
    "ApprovalPolicy",
    "ApprovalsReviewer",
    "LaunchSpec",
    "SandboxExecutionPolicy",
    "SandboxLauncher",
    "SandboxManager",
    "SandboxMode",
    "SandboxStatus",
    "SandboxUnavailableError",
    "command_hash",
    "SandboxViolation",
]
