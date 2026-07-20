"""macOS Seatbelt profile generation."""

from __future__ import annotations

import shutil
from pathlib import Path

from nanobot.security.sandbox.types import SandboxMode, SandboxUnavailableError


def _quote(value: str | Path) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def profile_text(
    *,
    mode: SandboxMode,
    workspace: Path,
    readable_roots: tuple[Path, ...] = (),
    writable_roots: tuple[Path, ...] = (),
    allow_network: bool = False,
) -> str:
    """Build a deny-by-default Seatbelt profile for one task launch."""
    ws = workspace.expanduser().resolve(strict=False)
    reads = tuple(dict.fromkeys((ws, *readable_roots)))
    writes = () if mode == SandboxMode.READ_ONLY else tuple(dict.fromkeys((ws, *writable_roots)))

    lines = [
        "(version 1)",
        "(deny default)",
        '(import "system.sb")',
        "(allow process*)",
        "(allow signal (target self))",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow ipc-posix-shm-read-data)",
        "(allow file-read-metadata)",
        "(allow network-outbound)" if allow_network else "(deny network*)",
    ]
    for root in reads:
        lines.append(f'(allow file-read* (subpath "{_quote(root)}"))')
    for root in writes:
        lines.append(f'(allow file-write* (subpath "{_quote(root)}"))')

    protected = (
        ws / ".git",
        ws / ".nanobot-runtime" / "interactions",
        ws / ".nanobot-runtime" / "checkpoints",
        ws / ".nanobot-runtime" / "trace",
    )
    for root in protected:
        lines.append(f'(deny file-write* (subpath "{_quote(root)}"))')
    return "\n".join(lines) + "\n"


def wrap_argv(profile: str, argv: tuple[str, ...]) -> tuple[str, ...]:
    binary = shutil.which("sandbox-exec")
    if not binary:
        raise SandboxUnavailableError("sandbox_unavailable", "macOS sandbox-exec is unavailable")
    return (binary, "-p", profile, *argv)
