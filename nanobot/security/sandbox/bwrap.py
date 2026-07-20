"""Linux/WSL2 Bubblewrap launch profile generation."""

from __future__ import annotations

import shutil
from pathlib import Path

from nanobot.security.sandbox.types import SandboxMode, SandboxUnavailableError


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
    args: list[str] = [
        binary,
        "--new-session",
        "--die-with-parent",
        "--ro-bind",
        "/",
        "/",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
    ]
    if not allow_network:
        args.insert(3, "--unshare-net")
    home = Path.home().resolve(strict=False)
    if home != Path("/"):
        args.extend(("--tmpfs", str(home)))

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

    roots = tuple(dict.fromkeys((ws, *writable_roots)))
    if mode == SandboxMode.READ_ONLY:
        roots = ()
    for root in roots:
        root = root.expanduser().resolve(strict=False)
        ensure_masked_mount_parents(root)
        args.extend(("--bind", str(root), str(root)))
    for root in tuple(dict.fromkeys(readable_roots)):
        root = root.expanduser().resolve(strict=False)
        ensure_masked_mount_parents(root)
        args.extend(("--ro-bind-try", str(root), str(root)))

    for protected in (
        ws / ".git",
        ws / ".nanobot-runtime" / "interactions",
        ws / ".nanobot-runtime" / "checkpoints",
        ws / ".nanobot-runtime" / "trace",
    ):
        if protected.exists():
            args.extend(("--ro-bind", str(protected), str(protected)))

    args.extend(("--chdir", str(cwd), "--", *argv))
    return tuple(args)
