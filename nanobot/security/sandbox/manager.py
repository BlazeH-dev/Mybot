"""Seatbelt capability reporting and fail-closed platform selection."""

from __future__ import annotations

import platform
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from nanobot.security.sandbox.types import SandboxMode, SandboxStatus


class SandboxManager:
    def __init__(self, *, system: str | None = None) -> None:
        self.system = (system or platform.system()).lower()

    @property
    def provider(self) -> str:
        return "seatbelt" if self.system == "darwin" else "unsupported"

    @lru_cache(maxsize=8)
    def provider_available(self) -> tuple[bool, str | None]:
        provider = self.provider
        if provider == "seatbelt":
            binary = shutil.which("sandbox-exec")
            if not binary:
                return False, "sandbox-exec not found"
            try:
                result = subprocess.run(
                    [
                        binary,
                        "-p",
                        (
                            "(version 1) (allow default) (deny file-write*) "
                            '(allow file-write* (literal "/dev/null"))'
                        ),
                        "--",
                        "/usr/bin/true",
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
                None if result.returncode == 0 else (result.stderr.strip() or "Seatbelt smoke failed"),
            )
        return False, "restricted process execution is supported only on macOS Seatbelt"

    def status(
        self,
        *,
        mode: SandboxMode,
        workspace: str | Path,
    ) -> SandboxStatus:
        ws = str(Path(workspace).expanduser().resolve(strict=False))
        if mode == SandboxMode.DANGER_FULL_ACCESS:
            return SandboxStatus(
                mode=mode,
                provider="none",
                enforced=False,
                available=True,
                reason="User explicitly selected Full Access.",
                writable_roots=("/",),
                readable_roots=("/",),
                enforcement="none",
                network="unrestricted",
            )
        available, reason = self.provider_available()
        writes = (str(Path("/dev/null").resolve(strict=False)),)
        if mode == SandboxMode.WORKSPACE_WRITE:
            writes = tuple(
                dict.fromkeys(
                    (
                        ws,
                        str(Path("/tmp").resolve(strict=False)),
                        str(Path(tempfile.gettempdir()).resolve(strict=False)),
                    )
                )
            )
        return SandboxStatus(
            mode=mode,
            provider=self.provider,
            enforced=available,
            available=available,
            reason=reason,
            writable_roots=tuple(dict.fromkeys(writes)),
            readable_roots=("/",),
            enforcement="full" if available else "none",
            file_write_restricted=available,
            file_read_restricted=False,
            network_restricted=False,
            network="unrestricted",
        )
