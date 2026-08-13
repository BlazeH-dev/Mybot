"""macOS Seatbelt profile generation."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Literal

from nanobot.security.sandbox.types import SandboxMode, SandboxUnavailableError


def _quote(value: str | Path) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def profile_text(
    *,
    mode: SandboxMode,
    workspace: Path,
) -> str:
    """Build a file-write-only Seatbelt profile for one process launch."""
    ws = workspace.expanduser().resolve(strict=False)
    writes: tuple[Path, ...] = ()
    if mode == SandboxMode.WORKSPACE_WRITE:
        writes = tuple(
            dict.fromkeys(
                path.expanduser().resolve(strict=False)
                for path in (ws, Path("/tmp"), Path(tempfile.gettempdir()))
            )
        )

    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f'(allow file-write* (literal "{_quote(Path("/dev/null"))}"))',
    ]
    for root in writes:
        lines.append(f'(allow file-write* (subpath "{_quote(root)}"))')
    return "\n".join(lines) + "\n"


def classify_failure(returncode: int | None, stderr: str) -> Literal["runner_failed", "denied"] | None:
    """Classify Seatbelt infrastructure failure before ordinary write denial."""
    if returncode is None:
        return None
    lowered = stderr.lower()
    if "sandbox-exec:" in lowered:
        return "runner_failed"
    if "operation not permitted" in lowered:
        return "denied"
    return None


def wrap_argv(profile: str, argv: tuple[str, ...]) -> tuple[str, ...]:
    binary = shutil.which("sandbox-exec")
    if not binary:
        raise SandboxUnavailableError("sandbox_unavailable", "macOS sandbox-exec is unavailable")
    return (binary, "-p", profile, "--", *argv)
