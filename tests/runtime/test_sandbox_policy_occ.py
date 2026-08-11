from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.runtime.policy import (
    PermissionDecision,
    PolicyEngine,
    PolicyGateOutcome,
)
from nanobot.security.sandbox import (
    ApprovalPolicy,
    SandboxLauncher,
    SandboxManager,
    SandboxMode,
    SandboxUnavailableError,
)
from nanobot.security.sandbox.bwrap import wrap_argv as bwrap_argv
from nanobot.security.sandbox.network import (
    NetworkGrant,
    bind_network_grant,
    command_hash,
    command_network_targets,
    reset_network_grant,
)
from nanobot.security.sandbox.seatbelt import profile_text, runtime_readable_roots
from nanobot.security.workspace_access import build_workspace_scope


def test_seatbelt_profile_denies_network_and_protects_runtime(tmp_path: Path) -> None:
    profile = profile_text(
        mode=SandboxMode.WORKSPACE_WRITE,
        workspace=tmp_path,
    )
    assert "(deny network*)" in profile
    assert str(tmp_path / ".git") in profile
    assert str(tmp_path / ".nanobot-runtime" / "interactions") in profile
    assert f'(allow file-write* (subpath "{tmp_path}"))' in profile
    assert f'(deny file-read* (literal "{tmp_path / ".git"}"))' in profile


def test_bwrap_profile_unshares_network_and_rebinds_controls_read_only(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    with patch("nanobot.security.sandbox.bwrap.shutil.which", return_value="/usr/bin/bwrap"):
        argv = bwrap_argv(
            mode=SandboxMode.WORKSPACE_WRITE,
            workspace=tmp_path,
            cwd=tmp_path,
            argv=("/bin/sh", "-c", "true"),
        )
    assert "--unshare-net" in argv
    assert ("--ro-bind", "/", "/") not in tuple(zip(argv, argv[1:], argv[2:]))
    assert "/usr" in argv
    git = str(tmp_path / ".git")
    git_index = argv.index(git, argv.index("--bind") + 1)
    assert argv[git_index - 1] == "--tmpfs"
    assert argv[git_index + 1:git_index + 3] == ("--remount-ro", git)
    for name in ("interactions", "checkpoints", "trace"):
        control = tmp_path / ".nanobot-runtime" / name
        assert control.is_dir()
        index = argv.index(str(control), git_index + 1)
        assert argv[index - 1] == "--tmpfs"
        assert argv[index + 1:index + 3] == ("--remount-ro", str(control))


def test_bwrap_masks_linked_worktree_git_file(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("gitdir: /private/common/worktrees/chat\n")
    with patch("nanobot.security.sandbox.bwrap.shutil.which", return_value="/usr/bin/bwrap"):
        argv = bwrap_argv(
            mode=SandboxMode.WORKSPACE_WRITE,
            workspace=tmp_path,
            cwd=tmp_path,
            argv=("/bin/sh", "-c", "true"),
        )
    git = str(tmp_path / ".git")
    git_index = argv.index(git, argv.index("--bind") + 1)
    assert argv[git_index - 2:git_index + 1] == ("--ro-bind", "/dev/null", git)


def test_bwrap_rejects_symlinked_protected_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".git").symlink_to(outside, target_is_directory=True)

    with (
        patch("nanobot.security.sandbox.bwrap.shutil.which", return_value="/usr/bin/bwrap"),
        pytest.raises(SandboxUnavailableError, match="cannot be a symlink"),
    ):
        bwrap_argv(
            mode=SandboxMode.WORKSPACE_WRITE,
            workspace=tmp_path,
            cwd=tmp_path,
            argv=("/bin/sh", "-c", "true"),
        )


def test_bwrap_read_only_mounts_workspace_without_write_bind(tmp_path: Path) -> None:
    with patch("nanobot.security.sandbox.bwrap.shutil.which", return_value="/usr/bin/bwrap"):
        argv = bwrap_argv(
            mode=SandboxMode.READ_ONLY,
            workspace=tmp_path,
            cwd=tmp_path,
            argv=("/bin/sh", "-c", "cat input.txt"),
        )

    workspace = str(tmp_path.resolve())
    assert ("--ro-bind", workspace, workspace) in tuple(zip(argv, argv[1:], argv[2:]))
    assert ("--bind", workspace, workspace) not in tuple(zip(argv, argv[1:], argv[2:]))


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="real Seatbelt smoke is macOS-only")
def test_real_seatbelt_allows_workspace_write_and_blocks_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    launcher = SandboxLauncher(SandboxManager(system="darwin"))

    allowed = launcher.prepare_shell(
        command="echo ok > inside.txt",
        workspace=workspace,
        cwd=workspace,
        env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")},
        mode=SandboxMode.WORKSPACE_WRITE,
    )
    allowed_result = subprocess.run(allowed.argv, cwd=allowed.cwd, env=allowed.env, check=False)
    assert allowed_result.returncode == 0
    assert (workspace / "inside.txt").read_text().strip() == "ok"

    denied = launcher.prepare_shell(
        command=f"(sleep 0.05; echo bad > {outside}) & wait",
        workspace=workspace,
        cwd=workspace,
        env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")},
        mode=SandboxMode.WORKSPACE_WRITE,
    )
    denied_result = subprocess.run(
        denied.argv,
        cwd=denied.cwd,
        env=denied.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert not outside.exists()
    assert denied_result.stderr

    protected = workspace / ".git"
    protected.mkdir()
    (protected / "config").write_text("seatbelt-secret")
    read_denied = launcher.prepare_shell(
        command="cat .git/config",
        workspace=workspace,
        cwd=workspace,
        env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")},
        mode=SandboxMode.WORKSPACE_WRITE,
    )
    read_result = subprocess.run(
        read_denied.argv,
        cwd=read_denied.cwd,
        env=read_denied.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert read_result.returncode != 0
    assert "seatbelt-secret" not in read_result.stdout


@pytest.mark.skipif(
    os.uname().sysname != "Linux" or shutil.which("bwrap") is None,
    reason="real Bubblewrap smoke requires Linux with bwrap",
)
def test_real_bwrap_allows_workspace_write_and_blocks_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    launcher = SandboxLauncher(SandboxManager(system="linux"))

    allowed = launcher.prepare_shell(
        command="echo ok > inside.txt",
        workspace=workspace,
        cwd=workspace,
        env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")},
        mode=SandboxMode.WORKSPACE_WRITE,
    )
    allowed_result = subprocess.run(allowed.argv, cwd=allowed.cwd, env=allowed.env, check=False)
    assert allowed_result.returncode == 0
    assert (workspace / "inside.txt").read_text().strip() == "ok"

    denied = launcher.prepare_shell(
        command=f"(sleep 0.05; echo bad > {outside}) & wait",
        workspace=workspace,
        cwd=workspace,
        env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")},
        mode=SandboxMode.WORKSPACE_WRITE,
    )
    denied_result = subprocess.run(
        denied.argv,
        cwd=denied.cwd,
        env=denied.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert not outside.exists()
    assert denied_result.stderr

    protected = workspace / ".git"
    protected.mkdir()
    (protected / "config").write_text("bwrap-secret")
    read_denied = launcher.prepare_shell(
        command="cat .git/config",
        workspace=workspace,
        cwd=workspace,
        env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")},
        mode=SandboxMode.WORKSPACE_WRITE,
    )
    read_result = subprocess.run(
        read_denied.argv,
        cwd=read_denied.cwd,
        env=read_denied.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert read_result.returncode != 0
    assert "bwrap-secret" not in read_result.stdout


def test_restricted_exec_fails_closed_when_provider_is_unavailable(tmp_path: Path) -> None:
    manager = SandboxManager(system="windows")
    launcher = SandboxLauncher(manager)
    with pytest.raises(SandboxUnavailableError) as exc:
        launcher.prepare_shell(
            command="echo hello",
            workspace=tmp_path,
            cwd=tmp_path,
            env={},
            mode=SandboxMode.WORKSPACE_WRITE,
        )
    assert exc.value.code == "sandbox_unavailable"


def test_seatbelt_launch_reports_required_runtime_read_roots(tmp_path: Path) -> None:
    manager = SandboxManager(system="darwin")
    with patch.object(manager, "provider_available", return_value=(True, None)):
        launch = SandboxLauncher(manager).prepare_shell(
            command="python -c 'print(123)'",
            workspace=tmp_path,
            cwd=tmp_path,
            env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")},
            mode=SandboxMode.WORKSPACE_WRITE,
        )

    expected = {str(root) for root in runtime_readable_roots()}
    assert expected <= set(launch.readable_roots)
    assert str(tmp_path.resolve()) in launch.readable_roots


def test_bwrap_launch_reports_only_runtime_and_explicit_read_roots(tmp_path: Path) -> None:
    from nanobot.security.sandbox.bwrap import runtime_readable_roots as bwrap_runtime_roots

    manager = SandboxManager(system="linux")
    with (
        patch.object(manager, "provider_available", return_value=(True, None)),
        patch("nanobot.security.sandbox.bwrap.shutil.which", return_value="bwrap"),
    ):
        launch = SandboxLauncher(manager).prepare_shell(
            command="python -c 'print(123)'",
            workspace=tmp_path,
            cwd=tmp_path,
            env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")},
            mode=SandboxMode.WORKSPACE_WRITE,
        )

    expected = {str(root.resolve(strict=False)) for root in bwrap_runtime_roots()}
    assert expected <= set(launch.readable_roots)
    triples = tuple(zip(launch.argv, launch.argv[1:], launch.argv[2:]))
    assert ("--ro-bind", "/", "/") not in triples


def test_prepare_argv_does_not_inherit_ambient_network_grant(tmp_path: Path) -> None:
    command = "curl --max-redirs 0 https://example.com/data"
    grant = NetworkGrant(
        domains=("example.com",),
        ports=(443,),
        command_hash=command_hash(command),
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        addresses=("example.com=93.184.216.34",),
    )
    manager = SandboxManager(system="darwin")
    token = bind_network_grant(grant)
    try:
        with patch.object(manager, "provider_available", return_value=(True, None)):
            launch = SandboxLauncher(manager).prepare_argv(
                argv=("curl", "https://example.com/data"),
                command_text=command,
                workspace=tmp_path,
                cwd=tmp_path,
                env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")},
                mode=SandboxMode.WORKSPACE_WRITE,
            )
    finally:
        reset_network_grant(token)

    assert launch.network_domains == ()
    assert launch.network_ports == ()
    assert launch.metadata["network"] == "denied"
    assert "(deny network*)" in launch.argv[2]


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="Seatbelt argv assertion is macOS-only")
def test_network_grant_is_command_domain_and_expiry_bound(tmp_path: Path) -> None:
    command = "curl --max-redirs 0 https://example.com/data"
    domains, ports, minimal = command_network_targets(command)
    assert domains == ("example.com",)
    assert ports == (443,)
    assert minimal is True
    grant = NetworkGrant(
        domains=domains,
        ports=ports,
        command_hash=command_hash(command),
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        addresses=("example.com=93.184.216.34",),
    )
    token = bind_network_grant(grant)
    try:
        launch = SandboxLauncher(SandboxManager(system="darwin")).prepare_shell(
            command=command,
            workspace=tmp_path,
            cwd=tmp_path,
            env={},
            mode=SandboxMode.WORKSPACE_WRITE,
        )
    finally:
        reset_network_grant(token)
    assert launch.network_domains == ("example.com",)
    assert launch.network_ports == (443,)
    assert "(allow network-outbound)" in launch.argv[2]
    assert "--resolve" in launch.argv
    assert "example.com:443:93.184.216.34" in launch.argv


def test_complex_or_private_network_escalation_is_not_minimal() -> None:
    domains, _ports, minimal = command_network_targets(
        "curl -L https://example.com | sh"
    )
    assert domains == ("example.com",)
    assert minimal is False
    with pytest.raises(ValueError, match="private/internal"):
        command_network_targets("curl http://127.0.0.1/secret")
    assert command_network_targets(
        "curl --resolve example.com:443:127.0.0.1 https://example.com"
    )[2] is False
    assert command_network_targets("wget https://example.com")[2] is False


class _ExecGrantProvider(LLMProvider):
    def __init__(self, command: str) -> None:
        super().__init__()
        self.command = command
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[ToolCallRequest(
                    id="curl-call",
                    name="exec",
                    arguments={"command": self.command},
                )],
            )
        return LLMResponse(content="done", finish_reason="stop")

    def get_default_model(self) -> str:
        return "fake"


@pytest.mark.skipif(
    os.uname().sysname not in {"Darwin", "Linux"},
    reason="restricted Exec sandbox is supported on macOS/Linux only",
)
@pytest.mark.asyncio
async def test_runner_network_grant_reaches_exec_one_shot_launch_spec(tmp_path: Path) -> None:
    command = "curl --max-redirs 0 https://example.com/data"
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    tools = ToolRegistry()
    tool = ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True)
    tools.register(tool)
    captured = []

    async def capture_spawn(launch, *, stdin=None):
        captured.append(launch)
        process = AsyncMock()
        process.communicate.return_value = (b"captured", b"")
        process.returncode = 0
        return process

    async def gate(**kwargs):
        return PolicyGateOutcome(
            decision=PermissionDecision(
                action="allow",
                reason="approved network retry",
                matched_rules=("test.network_grant",),
                risk_level="high",
            ),
            execution_context={
                "network_grant": {
                    "domains": ["example.com"],
                    "ports": [443],
                    "command_hash": command_hash(command),
                    "expires_at": expires_at,
                    "addresses": ["example.com=93.184.216.34"],
                }
            },
        )

    with patch.object(ExecTool, "_spawn", side_effect=capture_spawn):
        result = await AgentRunner(_ExecGrantProvider(command)).run(AgentRunSpec(
            initial_messages=[{"role": "user", "content": "fetch approved data"}],
            tools=tools,
            model="fake",
            max_iterations=3,
            max_tool_result_chars=1000,
            policy_gate=gate,
        ))

    assert result.final_content == "done"
    assert len(captured) == 1
    launch = captured[0]
    assert launch.network_domains == ("example.com",)
    assert launch.network_ports == (443,)
    assert "--resolve" in launch.argv
    assert "example.com:443:93.184.216.34" in launch.argv
    assert command not in launch.argv


@pytest.mark.skipif(
    os.uname().sysname not in {"Darwin", "Linux"},
    reason="restricted Exec sandbox is supported on macOS/Linux only",
)
@pytest.mark.asyncio
async def test_exec_session_never_consumes_network_grant(tmp_path: Path) -> None:
    command = "curl --max-redirs 0 https://example.com/data"
    grant = NetworkGrant(
        domains=("example.com",),
        ports=(443,),
        command_hash=command_hash(command),
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        addresses=("example.com=93.184.216.34",),
    )

    class CaptureManager:
        launch = None

        async def start(self, **kwargs):
            self.launch = kwargs["launch"]
            return "session", SimpleNamespace(
                output="",
                done=True,
                exit_code=0,
                elapsed_s=0.0,
                timed_out=False,
                terminated=False,
                stdin_closed=False,
                truncated_chars=0,
            )

    manager = CaptureManager()
    tool = ExecTool(
        working_dir=str(tmp_path),
        restrict_to_workspace=True,
        session_manager=manager,
    )
    token = bind_network_grant(grant)
    try:
        result = await tool.execute(command=command, yield_time_ms=0)
    finally:
        reset_network_grant(token)

    assert "Exit code: 0" in result
    assert manager.launch is not None
    assert manager.launch.network_domains == ()
    assert "--resolve" not in manager.launch.argv
    if manager.launch.provider == "seatbelt":
        assert "(deny network*)" in manager.launch.argv[2]
    else:
        assert "--unshare-net" in manager.launch.argv


@pytest.mark.asyncio
async def test_file_occ_requires_actor_read_and_compares_hash_when_mtime_is_unchanged(
    tmp_path: Path,
) -> None:
    target = tmp_path / "data.txt"
    target.write_text("one", encoding="utf-8")
    writer = WriteFileTool(workspace=tmp_path, enforce_occ=True)
    blocked = await writer.execute(path="data.txt", content="two")
    assert "file_conflict:not_read" in blocked

    reader = ReadFileTool(
        workspace=tmp_path,
        file_states=writer._file_states,
        enforce_occ=True,
    )
    await reader.execute(path="data.txt", force=True)
    original_mtime = target.stat().st_mtime
    target.write_text("external", encoding="utf-8")
    os.utime(target, (original_mtime, original_mtime))
    conflict = await writer.execute(path="data.txt", content="two")
    assert "file_conflict:modified_since_read" in conflict
    assert target.read_text() == "external"


def test_policy_default_vs_full_and_never(tmp_path: Path) -> None:
    target = tmp_path / "existing.txt"
    target.write_text("x")
    tool = WriteFileTool(workspace=tmp_path, enforce_occ=True)
    restricted = build_workspace_scope(tmp_path, "restricted")
    full = build_workspace_scope(tmp_path, "full")
    engine = PolicyEngine()
    ask = engine.evaluate(
        tool=tool,
        params={"path": "existing.txt", "content": "y"},
        scope=restricted,
        sandbox_mode=SandboxMode.WORKSPACE_WRITE,
    )
    assert ask.action == "ask"
    allow = engine.evaluate(
        tool=tool,
        params={"path": "existing.txt", "content": "y"},
        scope=full,
        sandbox_mode=SandboxMode.DANGER_FULL_ACCESS,
    )
    assert allow.action == "allow"
    never = PolicyEngine(approval_policy=ApprovalPolicy.NEVER).evaluate(
        tool=tool,
        params={"path": "existing.txt", "content": "y"},
        scope=restricted,
        sandbox_mode=SandboxMode.WORKSPACE_WRITE,
    )
    assert never.action == "deny"


def test_policy_hard_deny_cannot_be_approved(tmp_path: Path) -> None:
    tool = WriteFileTool(workspace=tmp_path, enforce_occ=True)
    scope = build_workspace_scope(tmp_path, "full")
    decision = PolicyEngine().evaluate(
        tool=tool,
        params={"path": str(Path.home() / ".nanobot" / "config.json"), "content": "x"},
        scope=scope,
        sandbox_mode=SandboxMode.DANGER_FULL_ACCESS,
    )
    assert decision.action == "deny"
    assert decision.hard_deny is True


def test_policy_allows_declared_trusted_read_root_but_never_writes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    skills = tmp_path / "builtin-skills"
    workspace.mkdir()
    skills.mkdir()
    skill_file = skills / "SKILL.md"
    skill_file.write_text("# Trusted skill\n", encoding="utf-8")
    scope = build_workspace_scope(workspace, "restricted")
    engine = PolicyEngine()
    reader = ReadFileTool(
        workspace=workspace,
        allowed_dir=workspace,
        extra_allowed_dirs=[skills],
        restrict_to_workspace=True,
    )
    writer = WriteFileTool(
        workspace=workspace,
        allowed_dir=workspace,
        extra_allowed_dirs=[skills],
        restrict_to_workspace=True,
    )

    read = engine.evaluate(
        tool=reader,
        params={"path": str(skill_file)},
        scope=scope,
        sandbox_mode=SandboxMode.WORKSPACE_WRITE,
    )
    write = engine.evaluate(
        tool=writer,
        params={"path": str(skill_file), "content": "changed"},
        scope=scope,
        sandbox_mode=SandboxMode.WORKSPACE_WRITE,
    )

    assert read.action == "allow"
    assert write.action == "deny"
    assert write.hard_deny is True


@pytest.mark.asyncio
async def test_restricted_exec_uses_os_sandbox_by_default(tmp_path: Path) -> None:
    tool = ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True)
    result = await tool.execute(command="echo ok > result.txt")
    assert "Exit code: 0" in result
    assert (tmp_path / "result.txt").read_text().strip() == "ok"
