"""OS-enforced sandbox primitives for Agent-triggered subprocesses."""

from nanobot.security.sandbox.launcher import SandboxLauncher
from nanobot.security.sandbox.manager import SandboxManager
from nanobot.security.sandbox.types import (
    ApprovalPolicy,
    ApprovalsReviewer,
    LaunchSpec,
    SandboxMode,
    SandboxStatus,
    SandboxUnavailableError,
    SandboxViolation,
)

__all__ = [
    "ApprovalPolicy",
    "ApprovalsReviewer",
    "LaunchSpec",
    "SandboxLauncher",
    "SandboxManager",
    "SandboxMode",
    "SandboxStatus",
    "SandboxUnavailableError",
    "SandboxViolation",
]
