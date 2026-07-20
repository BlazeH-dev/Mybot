"""Compatibility wrapper around the P3 OS sandbox launcher."""

import os
import platform
import shlex
from pathlib import Path

from nanobot.config.paths import get_media_dir
from nanobot.security.sandbox import SandboxLauncher, SandboxMode
from nanobot.security.sandbox.seatbelt import profile_text
from nanobot.security.sandbox.seatbelt import wrap_argv as seatbelt_argv


def _bwrap(command: str, workspace: str, cwd: str) -> str:
    """Wrap command in a bubblewrap sandbox (requires bwrap in container).

    Only the workspace is bind-mounted read-write; its parent dir (which holds
    config.json) is hidden behind a fresh tmpfs.  The media directory is
    bind-mounted read-only so exec commands can read uploaded attachments.
    """
    ws = Path(workspace).resolve()
    media = get_media_dir().resolve()

    try:
        sandbox_cwd = str(ws / Path(cwd).resolve().relative_to(ws))
    except ValueError:
        sandbox_cwd = str(ws)

    required = ["/usr"]
    optional = [
        "/bin",
        "/lib",
        "/lib64",
        "/etc/alternatives",
        "/etc/ssl/certs",
        "/etc/resolv.conf",
        "/etc/ld.so.cache",
    ]

    args: list[str] = ["bwrap", "--new-session", "--die-with-parent", "--unshare-net"]
    for path in required:
        args += ["--ro-bind", path, path]
    for path in optional:
        args += ["--ro-bind-try", path, path]
    args += [
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--tmpfs", str(ws.parent),
        "--dir", str(ws),
        "--bind", str(ws), str(ws),
        "--ro-bind-try", str(media), str(media),
        "--chdir", sandbox_cwd,
        "--", "sh", "-c", command,
    ]
    return shlex.join(args)


def _seatbelt(command: str, workspace: str, cwd: str) -> str:
    ws = Path(workspace).resolve()
    media = get_media_dir().resolve()
    profile = profile_text(
        mode=SandboxMode.WORKSPACE_WRITE,
        workspace=ws,
        readable_roots=(media,),
    )
    return shlex.join(seatbelt_argv(profile, ("sh", "-c", command)))


def _auto(command: str, workspace: str, cwd: str) -> str:
    launcher = SandboxLauncher()
    spec = launcher.prepare_shell(
        command=command,
        workspace=workspace,
        cwd=cwd,
        env={"HOME": os.environ.get("HOME", "/tmp")},
        mode=SandboxMode.WORKSPACE_WRITE,
        readable_roots=(get_media_dir().resolve(),),
    )
    return shlex.join(spec.argv)


_BACKENDS = {"auto": _auto, "bwrap": _bwrap, "seatbelt": _seatbelt}


def wrap_command(sandbox: str, command: str, workspace: str, cwd: str) -> str:
    """Wrap *command* using the named sandbox backend."""
    if sandbox == "auto":
        system = platform.system().lower()
        sandbox = "seatbelt" if system == "darwin" else "bwrap" if system == "linux" else "auto"
    if backend := _BACKENDS.get(sandbox):
        return backend(command, workspace, cwd)
    raise ValueError(f"Unknown sandbox backend {sandbox!r}. Available: {list(_BACKENDS)}")
