"""Tests for the legacy string adapter over the Seatbelt launcher."""

import shlex
from unittest.mock import patch

import pytest

from nanobot.agent.tools.sandbox import wrap_command


@pytest.fixture(autouse=True)
def _available_seatbelt():
    with (
        patch(
            "nanobot.security.sandbox.manager.SandboxManager.provider_available",
            return_value=(True, None),
        ),
        patch("nanobot.security.sandbox.seatbelt.shutil.which", return_value="sandbox-exec"),
    ):
        yield


def test_seatbelt_adapter_wraps_command(tmp_path):
    workspace = tmp_path / "project"
    result = wrap_command("seatbelt", "echo 'hello world'", str(workspace), str(workspace))
    tokens = shlex.split(result)

    assert tokens[0] == "sandbox-exec"
    assert tokens[1] == "-p"
    assert "(deny file-write*)" in tokens[2]
    assert tokens[-4:] == ["--", "/bin/sh", "-c", "echo 'hello world'"]


def test_auto_adapter_falls_back_to_workspace_cwd(tmp_path):
    workspace = tmp_path / "project"
    outside = tmp_path / "outside"
    result = wrap_command("auto", "pwd", str(workspace), str(outside))
    tokens = shlex.split(result)

    assert str(workspace.resolve()) in tokens[2]


@pytest.mark.parametrize("backend", ["", "bwrap", "nonexistent"])
def test_removed_or_unknown_backend_is_rejected(tmp_path, backend):
    with pytest.raises(ValueError, match="Unknown sandbox backend"):
        wrap_command(backend, "ls", str(tmp_path), str(tmp_path))
