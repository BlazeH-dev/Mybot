"""Sandbox provider detection, capability reporting, and fail-closed selection."""

from __future__ import annotations

import platform
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from nanobot.security.sandbox.types import SandboxMode, SandboxStatus


class SandboxManager:
    def __init__(self, *, system: str | None = None) -> None:
        self.system = (system or platform.system()).lower()

    @property
    def provider(self) -> str:
        if self.system == "darwin":
            return "seatbelt"
        if self.system == "linux":
            return "bwrap"
        if self.system == "windows":
            return "unsupported"
        return "unsupported"

    @lru_cache(maxsize=8)
    def provider_available(self) -> tuple[bool, str | None]:
        provider = self.provider
        if provider == "seatbelt":
            binary = shutil.which("sandbox-exec")
            if not binary:
                return False, "sandbox-exec not found"
            result = subprocess.run(
                [binary, "-p", "(version 1) (allow default)", "/usr/bin/true"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return (
                result.returncode == 0,
                None if result.returncode == 0 else (result.stderr.strip() or "Seatbelt smoke failed"),
            )
        if provider == "bwrap":
            binary = shutil.which("bwrap")
            if not binary:
                return False, "bwrap not found"
            try:
                result = subprocess.run(
                    [
                        binary,
                        "--new-session",
                        "--die-with-parent",
                        "--ro-bind",
                        "/",
                        "/",
                        "--unshare-net",
                        "--",
                        "/bin/true",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return False, str(exc)
            return (
                result.returncode == 0,
                None if result.returncode == 0 else (result.stderr.strip() or "Bubblewrap smoke failed"),
            )
        return False, "native Windows/unknown OS provider is unsupported"

    def status(
        self,
        *,
        mode: SandboxMode,
        workspace: str | Path,
        readable_roots: tuple[str | Path, ...] = (),
        writable_roots: tuple[str | Path, ...] = (),
    ) -> SandboxStatus:
        ws = str(Path(workspace).expanduser().resolve(strict=False))
        if mode == SandboxMode.DANGER_FULL_ACCESS:
            return SandboxStatus(
                mode=mode,
                provider="none",
                enforced=False,
                available=True,
                reason="User explicitly selected Full Access.",
                writable_roots=(ws,),
                readable_roots=(ws,),
                network="unrestricted",
            )
        available, reason = self.provider_available()
        writes = () if mode == SandboxMode.READ_ONLY else (
            ws,
            *(str(Path(p).expanduser().resolve(strict=False)) for p in writable_roots),
        )
        reads = (
            ws,
            *(str(Path(p).expanduser().resolve(strict=False)) for p in readable_roots),
        )
        return SandboxStatus(
            mode=mode,
            provider=self.provider,
            enforced=available,
            available=available,
            reason=reason,
            writable_roots=tuple(dict.fromkeys(writes)),
            readable_roots=tuple(dict.fromkeys(reads)),
            network="denied",
        )
