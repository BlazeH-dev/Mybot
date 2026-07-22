"""Unified launch-spec builder for Agent-triggered subprocesses."""

from __future__ import annotations

import shlex
from pathlib import Path

from nanobot.security.sandbox import bwrap, seatbelt
from nanobot.security.sandbox.manager import SandboxManager
from nanobot.security.sandbox.network import (
    NetworkGrant,
    command_hash,
    current_network_grant,
    network_grant_active,
    pinned_curl_argv,
)
from nanobot.security.sandbox.types import LaunchSpec, SandboxMode, SandboxUnavailableError


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
        readable_roots: tuple[str | Path, ...] = (),
        writable_roots: tuple[str | Path, ...] = (),
        allow_network_grant: bool = True,
    ) -> LaunchSpec:
        network_grant = (
            current_network_grant()
            if allow_network_grant and mode != SandboxMode.DANGER_FULL_ACCESS
            else None
        )
        if (
            network_grant is not None
            and network_grant.command_hash == command_hash(command)
            and network_grant_active(network_grant)
        ):
            argv = pinned_curl_argv(command, network_grant)
        else:
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
            readable_roots=readable_roots,
            writable_roots=writable_roots,
            network_grant=network_grant,
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
        readable_roots: tuple[str | Path, ...] = (),
        writable_roots: tuple[str | Path, ...] = (),
        network_grant: NetworkGrant | None = None,
    ) -> LaunchSpec:
        ws = Path(workspace).expanduser().resolve(strict=False)
        launch_cwd = Path(cwd).expanduser().resolve(strict=False)
        reads = tuple(Path(p).expanduser().resolve(strict=False) for p in readable_roots)
        writes = tuple(Path(p).expanduser().resolve(strict=False) for p in writable_roots)
        if self.manager.provider == "seatbelt":
            reads = tuple(dict.fromkeys((*seatbelt.runtime_readable_roots(), *reads)))
        elif self.manager.provider == "bwrap":
            reads = tuple(dict.fromkeys((*bwrap.runtime_readable_roots(), *reads)))
        status = self.manager.status(
            mode=mode,
            workspace=ws,
            readable_roots=reads,
            writable_roots=writes,
        )
        text = command_text if command_text is not None else shlex.join(argv)
        digest = command_hash(text)
        allow_network = bool(
            network_grant is not None and network_grant.command_hash == digest
            and network_grant_active(network_grant)
        )
        if mode == SandboxMode.DANGER_FULL_ACCESS:
            wrapped = argv
        else:
            if not status.available or not status.enforced:
                raise SandboxUnavailableError(
                    "sandbox_unavailable",
                    status.reason or f"{status.provider} is unavailable",
                )
            if status.provider == "seatbelt":
                profile = seatbelt.profile_text(
                    mode=mode,
                    workspace=ws,
                    readable_roots=reads,
                    writable_roots=writes,
                    allow_network=allow_network,
                )
                wrapped = seatbelt.wrap_argv(profile, argv)
            elif status.provider == "bwrap":
                wrapped = bwrap.wrap_argv(
                    mode=mode,
                    workspace=ws,
                    cwd=launch_cwd,
                    argv=argv,
                    readable_roots=reads,
                    writable_roots=writes,
                    allow_network=allow_network,
                )
            else:
                raise SandboxUnavailableError(
                    "sandbox_unavailable",
                    f"sandbox provider {status.provider!r} is unsupported",
                )
        return LaunchSpec(
            argv=tuple(wrapped),
            cwd=str(launch_cwd),
            env=dict(env),
            mode=mode,
            provider=status.provider,
            enforced=status.enforced,
            command_hash=digest,
            writable_roots=status.writable_roots,
            readable_roots=status.readable_roots,
            network_domains=(network_grant.domains if allow_network and network_grant else ()),
            network_ports=(network_grant.ports if allow_network and network_grant else ()),
            metadata={"network": "approved_domains" if allow_network else status.network},
        )
