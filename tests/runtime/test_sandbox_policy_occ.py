from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.runtime.policy import (
    PolicyEngine,
)
from nanobot.security.sandbox import (
    ApprovalPolicy,
    SandboxLauncher,
    SandboxManager,
    SandboxMode,
    SandboxUnavailableError,
)
from nanobot.security.sandbox.seatbelt import classify_failure, profile_text
from nanobot.security.workspace_access import build_workspace_scope


def test_seatbelt_workspace_write_profile_is_write_only_and_canonical(tmp_path: Path) -> None:
    profile = profile_text(
        mode=SandboxMode.WORKSPACE_WRITE,
        workspace=tmp_path,
    )
    assert "(allow default)" in profile
    assert "(deny file-write*)" in profile
    assert '(allow file-write* (literal "/dev/null"))' in profile
    assert f'(allow file-write* (subpath "{tmp_path}"))' in profile
    assert str(Path("/tmp").resolve()) in profile
    assert str(Path(tempfile.gettempdir()).resolve()) in profile
    assert "file-read" not in profile
    assert "network" not in profile


def test_seatbelt_read_only_profile_only_grants_dev_null(tmp_path: Path) -> None:
    profile = profile_text(mode=SandboxMode.READ_ONLY, workspace=tmp_path)
    assert '(allow file-write* (literal "/dev/null"))' in profile
    assert "(subpath" not in profile


def test_seatbelt_profile_escapes_workspace_path(tmp_path: Path) -> None:
    workspace = tmp_path / 'quoted"path\\segment'
    profile = profile_text(mode=SandboxMode.WORKSPACE_WRITE, workspace=workspace)
    assert 'quoted\\"path\\\\segment' in profile


def test_seatbelt_failure_classification() -> None:
    assert classify_failure(1, "sandbox-exec: sandbox_init: Invalid argument") == "runner_failed"
    assert classify_failure(1, "sh: file: Operation not permitted") == "denied"
    assert classify_failure(1, "ordinary child failure") is None


def test_seatbelt_wrap_separates_runner_and_child_argv(tmp_path: Path) -> None:
    manager = SandboxManager(system="darwin")
    with (
        patch.object(manager, "provider_available", return_value=(True, None)),
        patch("nanobot.security.sandbox.seatbelt.shutil.which", return_value="sandbox-exec"),
    ):
        launch = SandboxLauncher(manager).prepare_argv(
            argv=("/bin/echo", "-n", "ok"),
            command_text="echo -n ok",
            workspace=tmp_path,
            cwd=tmp_path,
            env={},
            mode=SandboxMode.WORKSPACE_WRITE,
        )

    assert launch.argv[:2] == ("sandbox-exec", "-p")
    assert launch.argv[3:] == ("--", "/bin/echo", "-n", "ok")


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="real Seatbelt smoke is macOS-only")
def test_real_seatbelt_workspace_write_semantics(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    outside_dir = Path.home() / f".mybot-seatbelt-test-{uuid.uuid4().hex}"
    outside_dir.mkdir()
    outside = outside_dir / "outside.txt"
    readable = outside_dir / "readable.txt"
    readable.write_text("outside-readable", encoding="utf-8")
    tmp_target = Path(tempfile.gettempdir()) / "mybot-seatbelt-temp.txt"
    slash_tmp_target = Path("/tmp/mybot-seatbelt-slash-tmp.txt")
    launcher = SandboxLauncher(SandboxManager(system="darwin"))
    command = (
        "echo inside > inside.txt; echo git > .git/config; "
        f"echo tmp > {tmp_target}; echo slash-tmp > {slash_tmp_target}; "
        f"cat {readable}; "
        f"{shlex.quote(os.sys.executable)} -B -c \"import socket; "
        "s=socket.socket(); s.bind(('127.0.0.1', 0)); "
        "print('network-ok')\"; "
        f"(echo blocked > {outside}) & wait"
    )
    try:
        launch = launcher.prepare_shell(
            command=command,
            workspace=workspace,
            cwd=workspace,
            env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")},
            mode=SandboxMode.WORKSPACE_WRITE,
        )
        result = subprocess.run(
            launch.argv, cwd=launch.cwd, env=launch.env, capture_output=True, text=True, check=False
        )
        assert (workspace / "inside.txt").read_text().strip() == "inside"
        assert (workspace / ".git" / "config").read_text().strip() == "git"
        assert tmp_target.read_text().strip() == "tmp"
        assert slash_tmp_target.read_text().strip() == "slash-tmp"
        assert "outside-readable" in result.stdout
        assert "network-ok" in result.stdout
        assert not outside.exists()
        assert classify_failure(result.returncode, result.stderr) == "denied"
    finally:
        shutil.rmtree(outside_dir)
        tmp_target.unlink(missing_ok=True)
        slash_tmp_target.unlink(missing_ok=True)


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="real Seatbelt smoke is macOS-only")
def test_real_seatbelt_read_only_blocks_writes_but_allows_dev_null(tmp_path: Path) -> None:
    launch = SandboxLauncher(SandboxManager(system="darwin")).prepare_shell(
        command=f"echo no > blocked.txt; echo no > {tempfile.gettempdir()}/blocked.txt; echo ok > /dev/null",
        workspace=tmp_path,
        cwd=tmp_path,
        env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")},
        mode=SandboxMode.READ_ONLY,
    )
    result = subprocess.run(
        launch.argv, cwd=launch.cwd, env=launch.env, capture_output=True, text=True, check=False
    )
    assert not (tmp_path / "blocked.txt").exists()
    assert result.returncode == 0
    assert classify_failure(result.returncode, result.stderr) == "denied"


def test_full_access_never_requires_a_platform_provider(tmp_path: Path) -> None:
    launch = SandboxLauncher(SandboxManager(system="linux")).prepare_argv(
        argv=("/bin/echo", "ok"),
        command_text="echo ok",
        workspace=tmp_path,
        cwd=tmp_path,
        env={},
        mode=SandboxMode.DANGER_FULL_ACCESS,
    )
    assert launch.argv == ("/bin/echo", "ok")
    assert launch.provider == "none"
    assert launch.enforced is False


def test_exec_keeps_application_level_ssrf_check(tmp_path: Path) -> None:
    tool = ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True)
    with patch("nanobot.security.network.socket.getaddrinfo") as resolve:
        resolve.return_value = [(2, 1, 0, "", ("127.0.0.1", 0))]
        prepared = tool._prepare_command("curl http://example.test/private")

    assert prepared == "Error: Command blocked by safety guard (internal/private URL detected)"


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


def test_seatbelt_launch_reports_write_only_guarantees(tmp_path: Path) -> None:
    manager = SandboxManager(system="darwin")
    with patch.object(manager, "provider_available", return_value=(True, None)):
        launch = SandboxLauncher(manager).prepare_shell(
            command="python -c 'print(123)'",
            workspace=tmp_path,
            cwd=tmp_path,
            env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")},
            mode=SandboxMode.WORKSPACE_WRITE,
        )

    assert launch.readable_roots == ("/",)
    assert launch.metadata["file_write_restricted"] is True
    assert launch.metadata["file_read_restricted"] is False
    assert launch.metadata["network_restricted"] is False
    assert launch.metadata["network"] == "unrestricted"


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
