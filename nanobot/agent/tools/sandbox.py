"""Legacy string adapter for the unified P3 OS sandbox launcher.

New subprocess call sites must execute ``LaunchSpec`` directly.  This module
only preserves the historical ``wrap_command`` API for external callers.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from nanobot.security.sandbox import SandboxLauncher, SandboxManager, SandboxMode


def wrap_command(sandbox: str, command: str, workspace: str, cwd: str) -> str:
    """Return a quoted compatibility string built by ``SandboxLauncher``."""
    if sandbox not in {"auto", "seatbelt"}:
        raise ValueError(f"Unknown sandbox backend {sandbox!r}. Available: ['auto', 'seatbelt']")

    ws = Path(workspace).expanduser().resolve(strict=False)
    launch_cwd = Path(cwd).expanduser().resolve(strict=False)
    try:
        launch_cwd.relative_to(ws)
    except ValueError:
        launch_cwd = ws

    manager = SandboxManager()
    launch = SandboxLauncher(manager).prepare_shell(
        command=command,
        workspace=ws,
        cwd=launch_cwd,
        env={
            "HOME": str(ws),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": os.environ.get("PATH", os.defpath),
        },
        mode=SandboxMode.WORKSPACE_WRITE,
    )
    return shlex.join(launch.argv)
