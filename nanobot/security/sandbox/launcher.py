"""Unified launch-spec builder for Agent-triggered subprocesses."""

from __future__ import annotations

import shlex
from pathlib import Path

from nanobot.security.sandbox import seatbelt
from nanobot.security.sandbox.manager import SandboxManager
from nanobot.security.sandbox.types import (
    LaunchSpec,
    SandboxExecutionPolicy,
    SandboxMode,
    SandboxUnavailableError,
    command_hash,
)


class SandboxLauncher:
    def __init__(self, manager: SandboxManager | None = None) -> None:
        self.manager = manager or SandboxManager()

    def prepare_shell(
        self,
        *,
        command: str,
        workspace: str | Path,
        cwd: str | Path,
        env: dict[str, str],
        mode: SandboxMode,
        shell: str = "/bin/sh",
        login: bool = False,
    ) -> LaunchSpec:
        shell_argv = [shell]
        if login and Path(shell).name.lower() in {"bash", "bash.exe", "zsh", "zsh.exe"}:
            shell_argv.append("-l")
        shell_argv.extend(("-c", command))
        argv = tuple(shell_argv)
        return self.prepare_argv(
            argv=argv,
            command_text=command,
            workspace=workspace,
            cwd=cwd,
            env=env,
            mode=mode,
        )

    def prepare_argv(
        self,
        *,
        argv: tuple[str, ...],
        command_text: str | None,
        workspace: str | Path,
        cwd: str | Path,
        env: dict[str, str],
        mode: SandboxMode,
    ) -> LaunchSpec:
        policy = SandboxExecutionPolicy.resolve(mode=mode, workspace=workspace)
        ws = Path(policy.workspace_root)
        launch_cwd = Path(cwd).expanduser().resolve(strict=False)
        status = self.manager.status(
            mode=mode,
            workspace=ws,
        )
        text = command_text if command_text is not None else shlex.join(argv)
        digest = command_hash(text)
        if mode == SandboxMode.DANGER_FULL_ACCESS:
            wrapped = argv
        else:
            if not status.available or not status.enforced:
                raise SandboxUnavailableError(
                    "sandbox_unavailable",
                    status.reason or f"{status.provider} is unavailable",
                )
            if status.provider != "seatbelt":
                raise SandboxUnavailableError(
                    "sandbox_unavailable",
                    f"sandbox provider {status.provider!r} is unsupported",
                )
            profile = seatbelt.profile_text(
                mode=mode,
                workspace=ws,
            )
            wrapped = seatbelt.wrap_argv(profile, argv)
        return LaunchSpec(
            argv=tuple(wrapped),
            cwd=str(launch_cwd),
            env=dict(env),
            mode=mode,
            provider=status.provider,
            enforced=status.enforced,
            enforcement=status.enforcement,
            command_hash=digest,
            writable_roots=status.writable_roots,
            readable_roots=status.readable_roots,
            metadata={
                "file_write_restricted": status.file_write_restricted,
                "file_read_restricted": False,
                "network_restricted": False,
                "network": "unrestricted",
            },
        )
