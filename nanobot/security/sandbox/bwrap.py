"""Linux/WSL2 Bubblewrap launch profile generation."""

from __future__ import annotations

import shutil
from pathlib import Path

from nanobot.security.sandbox.types import SandboxMode, SandboxUnavailableError

_SYSTEM_RUNTIME_ROOTS = (
    Path("/usr"),
    Path("/usr/local"),
    Path("/bin"),
    Path("/sbin"),
    Path("/lib"),
    Path("/lib64"),
    Path("/etc/alternatives"),
    Path("/etc/ssl/certs"),
    Path("/etc/resolv.conf"),
    Path("/etc/nsswitch.conf"),
    Path("/etc/hosts"),
    Path("/etc/gai.conf"),
    Path("/etc/services"),
    Path("/etc/localtime"),
    Path("/etc/ld.so.cache"),
)


def runtime_readable_roots() -> tuple[Path, ...]:
    """Minimal host roots needed by normal Linux command-line programs."""
    return tuple(dict.fromkeys(root for root in _SYSTEM_RUNTIME_ROOTS if root.exists()))


def _protected_roots(workspace: Path) -> tuple[Path, ...]:
    return (
        workspace / ".git",
        workspace / ".nanobot-runtime" / "interactions",
        workspace / ".nanobot-runtime" / "checkpoints",
        workspace / ".nanobot-runtime" / "trace",
    )


def wrap_argv(
    *,
    mode: SandboxMode,
    workspace: Path,
    cwd: Path,
    argv: tuple[str, ...],
    readable_roots: tuple[Path, ...] = (),
    writable_roots: tuple[Path, ...] = (),
    allow_network: bool = False,
) -> tuple[str, ...]:
    binary = shutil.which("bwrap")
    if not binary:
        raise SandboxUnavailableError("sandbox_unavailable", "Bubblewrap (bwrap) is unavailable")

    ws = workspace.expanduser().resolve(strict=False)
    protected_roots = _protected_roots(ws)
    for protected in protected_roots:
        if protected.is_symlink():
            raise SandboxUnavailableError(
                "sandbox_unavailable",
                f"protected sandbox path cannot be a symlink: {protected}",
            )
    for control in protected_roots[1:]:
        try:
            control.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SandboxUnavailableError(
                "sandbox_unavailable",
                f"cannot establish protected runtime path {control}: {exc}",
            ) from exc

    args: list[str] = [
        binary,
        "--new-session",
        "--die-with-parent",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
    ]
    runtime_roots = runtime_readable_roots()
    for root in runtime_roots:
        args.extend(("--ro-bind", str(root), str(root)))
    if not allow_network:
        args.insert(3, "--unshare-net")
    home = Path.home().resolve(strict=False)
    if home != Path("/"):
        args.extend(("--dir", str(home)))

    def ensure_masked_parents(root: Path, masked_root: Path) -> None:
        try:
            relative = root.relative_to(masked_root)
        except ValueError:
            return
        current = masked_root
        for part in relative.parts:
            current /= part
            args.extend(("--dir", str(current)))

    def ensure_masked_mount_parents(root: Path) -> None:
        ensure_masked_parents(root, home)
        ensure_masked_parents(root, Path("/tmp"))

    if mode == SandboxMode.READ_ONLY:
        ensure_masked_mount_parents(ws)
        args.extend(("--ro-bind", str(ws), str(ws)))
    else:
        for root in tuple(dict.fromkeys((ws, *writable_roots))):
            root = root.expanduser().resolve(strict=False)
            ensure_masked_mount_parents(root)
            args.extend(("--bind", str(root), str(root)))
    runtime_resolved = {root.resolve(strict=False) for root in runtime_roots}
    for root in tuple(dict.fromkeys(readable_roots)):
        root = root.expanduser().resolve(strict=False)
        if root in runtime_resolved:
            continue
        ensure_masked_mount_parents(root)
        args.extend(("--ro-bind-try", str(root), str(root)))

    for protected in protected_roots:
        if protected.is_dir():
            args.extend(("--tmpfs", str(protected), "--remount-ro", str(protected)))
        elif protected.exists():
            args.extend(("--ro-bind", "/dev/null", str(protected)))

    args.extend(("--chdir", str(cwd), "--", *argv))
    return tuple(args)
