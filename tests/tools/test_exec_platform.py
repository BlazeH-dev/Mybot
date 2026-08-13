"""Tests for cross-platform shell execution.

Verifies that ExecTool selects the correct shell, environment, path-append
strategy, and sandbox behaviour per platform — without actually running
platform-specific binaries (all subprocess calls are mocked).
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from nanobot.agent.tools.shell import ExecTool
from nanobot.security.sandbox import LaunchSpec, SandboxMode

_WINDOWS_ENV_KEYS = {
    "APPDATA", "LOCALAPPDATA", "ProgramData",
    "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
}


def _launch(
    argv: tuple[str, ...],
    *,
    cwd: str = "/tmp",
    env: dict[str, str] | None = None,
    provider: str = "none",
) -> LaunchSpec:
    return LaunchSpec(
        argv=argv,
        cwd=cwd,
        env=env or {"HOME": "/tmp", "PATH": "/usr/bin:/bin"},
        mode=(
            SandboxMode.DANGER_FULL_ACCESS
            if provider == "none"
            else SandboxMode.WORKSPACE_WRITE
        ),
        provider=provider,
        enforced=provider != "none",
        command_hash="hash",
    )


# ---------------------------------------------------------------------------
# _build_env
# ---------------------------------------------------------------------------

class TestBuildEnvUnix:

    def test_expected_keys(self):
        with patch("nanobot.agent.tools.shell._IS_WINDOWS", False):
            env = ExecTool()._build_env()
        expected = {"HOME", "LANG", "TERM", "PATH", "PYTHONUNBUFFERED"}
        assert expected <= set(env)
        if sys.platform != "win32":
            assert set(env) == expected

    def test_home_from_environ(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/dev")
        with patch("nanobot.agent.tools.shell._IS_WINDOWS", False):
            env = ExecTool()._build_env()
        assert env["HOME"] == "/Users/dev"

    def test_secrets_excluded(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        monkeypatch.setenv("NANOBOT_TOKEN", "tok-secret")
        with patch("nanobot.agent.tools.shell._IS_WINDOWS", False):
            env = ExecTool()._build_env()
        assert "OPENAI_API_KEY" not in env
        assert "NANOBOT_TOKEN" not in env
        for v in env.values():
            assert "secret" not in v.lower()


class TestBuildEnvWindows:

    _EXPECTED_KEYS = {
        "SYSTEMROOT", "COMSPEC", "USERPROFILE", "HOMEDRIVE",
        "HOMEPATH", "TEMP", "TMP", "PATHEXT", "PATH", "PYTHONUNBUFFERED",
        *_WINDOWS_ENV_KEYS,
    }

    def test_expected_keys(self):
        with patch("nanobot.agent.tools.shell._IS_WINDOWS", True):
            env = ExecTool()._build_env()
        assert set(env) == self._EXPECTED_KEYS

    def test_secrets_excluded(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        monkeypatch.setenv("NANOBOT_TOKEN", "tok-secret")
        with patch("nanobot.agent.tools.shell._IS_WINDOWS", True):
            env = ExecTool()._build_env()
        assert "OPENAI_API_KEY" not in env
        assert "NANOBOT_TOKEN" not in env
        for v in env.values():
            assert "secret" not in v.lower()

    def test_path_has_sensible_default(self):
        with (
            patch("nanobot.agent.tools.shell._IS_WINDOWS", True),
            patch.dict("os.environ", {}, clear=True),
        ):
            env = ExecTool()._build_env()
        assert "system32" in env["PATH"].lower()

    def test_systemroot_forwarded(self, monkeypatch):
        monkeypatch.setenv("SYSTEMROOT", r"D:\Windows")
        with patch("nanobot.agent.tools.shell._IS_WINDOWS", True):
            env = ExecTool()._build_env()
        assert env["SYSTEMROOT"] == r"D:\Windows"


# ---------------------------------------------------------------------------
# _spawn
# ---------------------------------------------------------------------------

class TestSpawnUnix:

    @pytest.mark.asyncio
    async def test_executes_launch_spec_without_reparsing(self):
        launch = _launch(("/bin/bash", "-l", "-c", "echo hi"))
        with (
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
        ):
            mock_exec.return_value = AsyncMock()
            await ExecTool._spawn(launch)

        args = mock_exec.call_args[0]
        assert args == launch.argv

        kwargs = mock_exec.call_args[1]
        assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
        assert kwargs["cwd"] == launch.cwd
        assert kwargs["env"] == launch.env


class TestSpawnWindows:

    @pytest.mark.asyncio
    async def test_single_line_uses_explicit_cmd_argv(self):
        env = {"COMSPEC": r"C:\Windows\system32\cmd.exe", "PATH": ""}
        launch = ExecTool._windows_launch_spec(command="dir", cwd=r"C:\work", env=env)
        assert launch.argv == (r"C:\Windows\system32\cmd.exe", "/d", "/s", "/c", "dir")

    @pytest.mark.asyncio
    async def test_single_line_passes_cwd_and_env(self):
        env = {"COMSPEC": "cmd.exe", "PATH": ""}
        launch = ExecTool._windows_launch_spec(command="echo hi", cwd=r"C:\work", env=env)
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = AsyncMock()
            await ExecTool._spawn(launch)

        assert mock_exec.call_args.kwargs["cwd"] == r"C:\work"
        assert mock_exec.call_args.kwargs["env"] == env

    @pytest.mark.asyncio
    async def test_multiline_uses_powershell(self):
        env = {"PATH": ""}
        command = 'python -c "print(1)\nprint(2)"'
        launch = ExecTool._windows_launch_spec(command=command, cwd=r"C:\work", env=env)
        assert launch.argv == ("powershell", "-NoProfile", "-Command", command)


# ---------------------------------------------------------------------------
# path_append
# ---------------------------------------------------------------------------

class TestPathAppendPlatform:

    @pytest.mark.asyncio
    async def test_unix_uses_env_var_in_fixed_export(self):
        """On Unix, path_append must not be interpolated into shell source."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok", b"")
        mock_proc.returncode = 0

        captured_launch = None
        captured_env = {}

        async def capture_spawn(launch):
            nonlocal captured_launch
            captured_launch = launch
            captured_env.update(launch.env)
            return mock_proc

        with (
            patch("nanobot.agent.tools.shell._IS_WINDOWS", False),
            patch("nanobot.agent.tools.shell.os.pathsep", ":"),
            patch.object(ExecTool, "_spawn", side_effect=capture_spawn),
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            tool = ExecTool(path_append="/opt/bin; echo INJECTED")
            await tool.execute(command="ls")

        assert captured_launch is not None
        assert captured_launch.argv[-1] == "ls"
        assert captured_env["PATH"].endswith(":/opt/bin; echo INJECTED")
        assert "INJECTED" not in captured_launch.argv[-1]

    @pytest.mark.asyncio
    async def test_windows_modifies_env(self):
        """On Windows, path_append is appended to PATH in the env dict."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok", b"")
        mock_proc.returncode = 0

        captured_env = {}

        async def capture_spawn(launch):
            captured_env.update(launch.env)
            return mock_proc

        with (
            patch("nanobot.agent.tools.shell._IS_WINDOWS", True),
            patch("nanobot.agent.tools.shell.os.pathsep", ";"),
            patch.object(ExecTool, "_spawn", side_effect=capture_spawn),
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            tool = ExecTool(path_append=r"C:\tools\bin")
            await tool.execute(command="dir")

        assert captured_env["PATH"].endswith(r";C:\tools\bin")


# ---------------------------------------------------------------------------
# sandbox
# ---------------------------------------------------------------------------

class TestSandboxPlatform:

    @pytest.mark.asyncio
    async def test_restricted_mode_fails_closed_on_windows(self):
        """Native Windows must not silently continue without an OS sandbox."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok", b"")
        mock_proc.returncode = 0

        with (
            patch("nanobot.agent.tools.shell._IS_WINDOWS", True),
            patch.object(ExecTool, "_spawn", return_value=mock_proc) as mock_spawn,
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            tool = ExecTool(restrict_to_workspace=True)
            result = await tool.execute(command="dir")

        assert "sandbox_unavailable" in result
        mock_spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_seatbelt_applied_on_restricted_unix(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"sandboxed", b"")
        mock_proc.returncode = 0

        launch = _launch(
            ("sandbox-exec", "-p", "profile", "sh", "-c", "ls"),
            cwd="/workspace",
            provider="seatbelt",
        )
        with (
            patch("nanobot.agent.tools.shell._IS_WINDOWS", False),
            patch(
                "nanobot.agent.tools.shell.SandboxLauncher.prepare_shell",
                return_value=launch,
            ) as mock_prepare,
            patch.object(ExecTool, "_spawn", return_value=mock_proc) as mock_spawn,
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            tool = ExecTool(restrict_to_workspace=True, working_dir="/workspace")
            await tool.execute(command="ls")

        mock_prepare.assert_called_once()
        spawned_launch = mock_spawn.call_args.args[0]
        assert spawned_launch.argv[0] == "sandbox-exec"

    def test_restricted_launch_ignores_login_and_uses_workspace_home(self, tmp_path):
        launch = _launch(
            ("sandbox-exec", "-p", "profile", "/bin/bash", "-c", "pwd"),
            cwd=str(tmp_path),
            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            provider="seatbelt",
        )
        with (
            patch("nanobot.agent.tools.shell._IS_WINDOWS", False),
            patch(
                "nanobot.agent.tools.shell.SandboxLauncher.prepare_shell",
                return_value=launch,
            ) as mock_prepare,
        ):
            prepared = ExecTool(
                working_dir=str(tmp_path),
                restrict_to_workspace=True,
            )._prepare_command("pwd", login=True)

        assert not isinstance(prepared, str)
        kwargs = mock_prepare.call_args.kwargs
        assert kwargs["login"] is False
        assert kwargs["env"]["HOME"] == str(tmp_path.resolve())
        assert kwargs["env"]["PATH"]
        assert Path(kwargs["workspace"]) == tmp_path.resolve()

    @pytest.mark.asyncio
    async def test_restricted_one_shot_start_failure_is_structured(self, tmp_path):
        launch = _launch(
            ("sandbox-exec", "-p", "profile", "/bin/sh", "-c", "true"),
            cwd=str(tmp_path),
            provider="seatbelt",
        )
        with (
            patch("nanobot.agent.tools.shell._IS_WINDOWS", False),
            patch(
                "nanobot.agent.tools.shell.SandboxLauncher.prepare_shell",
                return_value=launch,
            ),
            patch.object(ExecTool, "_spawn", side_effect=FileNotFoundError("sandbox-exec")),
        ):
            result = await ExecTool(
                working_dir=str(tmp_path),
                restrict_to_workspace=True,
            ).execute(command="true")

        assert result == "Error: sandbox_unavailable: sandbox-exec"

    @pytest.mark.asyncio
    async def test_runner_failure_and_write_denial_are_distinct(self, tmp_path):
        launch = _launch(
            ("sandbox-exec", "-p", "profile", "--", "/bin/sh", "-c", "true"),
            cwd=str(tmp_path),
            provider="seatbelt",
        )
        runner = AsyncMock()
        runner.communicate.return_value = (b"", b"sandbox-exec: sandbox_init failed")
        runner.returncode = 1
        denied = AsyncMock()
        denied.communicate.return_value = (b"", b"write: Operation not permitted")
        denied.returncode = 1
        tool = ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True)

        with (
            patch("nanobot.agent.tools.shell.SandboxLauncher.prepare_shell", return_value=launch),
            patch.object(tool, "_spawn", side_effect=[runner, denied]),
        ):
            runner_result = await tool.execute(command="true")
            denied_result = await tool.execute(command="true")

        assert runner_result == "Error: sandbox_unavailable: sandbox-exec runner failed"
        assert "Sandbox: denied" in denied_result
        assert "Exit code: 1" in denied_result


# ---------------------------------------------------------------------------
# end-to-end (mocked subprocess, full execute path)
# ---------------------------------------------------------------------------

class TestExecuteEndToEnd:

    @pytest.mark.asyncio
    async def test_windows_full_path(self):
        """Full execute() flow on Windows: env, spawn, output formatting."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"hello world\r\n", b"")
        mock_proc.returncode = 0

        with (
            patch("nanobot.agent.tools.shell._IS_WINDOWS", True),
            patch.object(ExecTool, "_spawn", return_value=mock_proc),
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            tool = ExecTool()
            result = await tool.execute(command="echo hello world")

        assert "hello world" in result
        assert "Exit code: 0" in result

    @pytest.mark.asyncio
    async def test_unix_full_path(self):
        """Full execute() flow on Unix: env, spawn, output formatting."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"hello world\n", b"")
        mock_proc.returncode = 0

        with (
            patch("nanobot.agent.tools.shell._IS_WINDOWS", False),
            patch.object(ExecTool, "_spawn", return_value=mock_proc),
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            tool = ExecTool()
            result = await tool.execute(command="echo hello world")

        assert "hello world" in result
        assert "Exit code: 0" in result


# ---------------------------------------------------------------------------
# _extract_absolute_paths - UNC path support
# ---------------------------------------------------------------------------

class TestExtractAbsolutePaths:
    """Tests for Windows UNC path extraction in shell commands."""

    def test_windows_drive_path(self):
        """Test extraction of standard Windows drive paths."""
        cmd = r"dir C:\Users\Public"
        paths = ExecTool._extract_absolute_paths(cmd)
        assert r"C:\Users\Public" in paths

    def test_windows_drive_path_root(self):
        """Test extraction of Windows drive root paths."""
        cmd = r"dir C:\temp"
        paths = ExecTool._extract_absolute_paths(cmd)
        assert any("C:\\" in p for p in paths)

    def test_unc_path_simple(self):
        """Test extraction of simple UNC paths."""
        cmd = r"dir \\server\share"
        paths = ExecTool._extract_absolute_paths(cmd)
        assert r"\\server\share" in paths

    def test_unc_path_with_subdirs(self):
        """Test extraction of UNC paths with subdirectories."""
        cmd = r"copy \\server\share\folder\file.txt D:\backup"
        paths = ExecTool._extract_absolute_paths(cmd)
        assert r"\\server\share\folder\file.txt" in paths
        assert r"D:\backup" in paths

    def test_unc_path_in_quotes(self):
        """Test extraction of UNC paths enclosed in quotes."""
        cmd = r'type "\\server\share\docs\readme.txt"'
        paths = ExecTool._extract_absolute_paths(cmd)
        assert r"\\server\share\docs\readme.txt" in paths

    def test_mixed_paths(self):
        """Test extraction of mixed UNC, drive, and POSIX paths."""
        cmd = r'copy \\server\data\file.txt C:\local\temp && ls /tmp'
        paths = ExecTool._extract_absolute_paths(cmd)
        assert r"\\server\data\file.txt" in paths
        assert any("C:\\" in p for p in paths)
        assert "/tmp" in paths

    def test_home_path(self):
        """Test extraction of home directory shortcuts."""
        cmd = "cat ~/config.txt"
        paths = ExecTool._extract_absolute_paths(cmd)
        assert "~/config.txt" in paths

    def test_no_paths(self):
        """Test command with no absolute paths."""
        cmd = "echo hello"
        paths = ExecTool._extract_absolute_paths(cmd)
        assert paths == []


# ---------------------------------------------------------------------------
# Windows multi-line command PowerShell fallback
# ---------------------------------------------------------------------------

class TestWindowsMultilineExec:
    """Verify multi-line commands on Windows route through PowerShell."""

    @pytest.mark.asyncio
    async def test_multiline_python_uses_powershell(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"1\n2\n", b"")
        mock_proc.returncode = 0

        with (
            patch("nanobot.agent.tools.shell._IS_WINDOWS", True),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            mock_exec.return_value = mock_proc
            tool = ExecTool()
            result = await tool.execute(command='python -c "print(1)\nprint(2)"')

        assert "1" in result
        assert "2" in result
        assert "Exit code: 0" in result
        args = mock_exec.call_args[0]
        assert args[0] == "powershell"

    @pytest.mark.asyncio
    async def test_multiline_node_uses_powershell(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"1\n", b"")
        mock_proc.returncode = 0

        with (
            patch("nanobot.agent.tools.shell._IS_WINDOWS", True),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            mock_exec.return_value = mock_proc
            tool = ExecTool()
            result = await tool.execute(command='node -e "console.log(1)\nconsole.log(2)"')

        assert "1" in result
        args = mock_exec.call_args[0]
        assert args[0] == "powershell"

    @pytest.mark.asyncio
    async def test_single_line_uses_shell(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"1\n", b"")
        mock_proc.returncode = 0

        with (
            patch("nanobot.agent.tools.shell._IS_WINDOWS", True),
            patch.object(ExecTool, "_spawn", return_value=mock_proc) as mock_spawn,
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            tool = ExecTool()
            result = await tool.execute(command='python -c "print(1)"')

        assert "1" in result
        mock_spawn.assert_called_once()

    @pytest.mark.asyncio
    async def test_unix_unchanged(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"1\n2\n", b"")
        mock_proc.returncode = 0

        with (
            patch("nanobot.agent.tools.shell._IS_WINDOWS", False),
            patch.object(ExecTool, "_spawn", return_value=mock_proc) as mock_spawn,
            patch.object(ExecTool, "_guard_command", return_value=None),
        ):
            tool = ExecTool()
            result = await tool.execute(command='python -c "print(1)\nprint(2)"')

        assert "1" in result
        mock_spawn.assert_called_once()
