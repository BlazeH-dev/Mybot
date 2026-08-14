from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import pytest

from nanobot.agent.ptc.runtime import PtcRuntime
from nanobot.config.schema import PtcConfig
from nanobot.security.sandbox import SandboxMode


def _config(**overrides) -> PtcConfig:
    return PtcConfig(sandbox="none", wall_timeout_seconds=5, **overrides)


@pytest.mark.asyncio
async def test_runtime_executes_async_code_and_curates_output(tmp_path: Path) -> None:
    calls: list[tuple[str, dict]] = []

    async def dispatch(_call_id: str, name: str, arguments: dict):
        calls.append((name, arguments))
        return {"value": arguments["value"] * 2}

    result = await PtcRuntime(_config(), tmp_path).run(
        code=(
            'first, second = await asyncio.gather('
            'tools.double({"value": 2}), tools.double({"value": 4}))\n'
            'print("processed", len([first, second]))\n'
            'return {"total": first["value"] + second["value"]}'
        ),
        dispatch=dispatch,
    )

    assert result.error is None
    assert result.logs == ["processed 2"]
    assert result.returned == {"total": 12}
    assert result.returned_present is True
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_runtime_supports_null_return(tmp_path: Path) -> None:
    async def dispatch(_call_id: str, _name: str, _arguments: dict):
        return None

    result = await PtcRuntime(_config(), tmp_path).run(
        code="return None",
        dispatch=dispatch,
    )
    assert result.error is None
    assert result.returned is None
    assert result.returned_present is True
    assert result.output == "null"


@pytest.mark.asyncio
async def test_runtime_allows_safe_imports_and_blocks_environment_access(tmp_path: Path) -> None:
    async def dispatch(_call_id: str, _name: str, _arguments: dict):
        return None

    os.environ["PTC_TEST_SECRET"] = "not-visible"
    safe_result = await PtcRuntime(_config(), tmp_path).run(
        code=(
            "import math\n"
            "import math as m\n"
            "import json\n"
            "from json import dumps as encode\n"
            "return {'ceil': math.ceil(1.1) + m.floor(1.9), "
            "'json': encode(json.loads('{\"ok\": true}'))}"
        ),
        dispatch=dispatch,
    )
    blocked_result = await PtcRuntime(_config(), tmp_path).run(
        code="import os\nreturn os.environ.get('PTC_TEST_SECRET')",
        dispatch=dispatch,
    )
    assert safe_result.error is None
    assert safe_result.returned == {"ceil": 3, "json": '{"ok": true}'}
    assert blocked_result.error is not None
    assert blocked_result.error.kind == "syntax_error"


@pytest.mark.asyncio
async def test_runtime_exposes_one_argument_type_and_bounded_shape(tmp_path: Path) -> None:
    async def dispatch(_call_id: str, _name: str, _arguments: dict):
        return None

    result = await PtcRuntime(_config(), tmp_path).run(
        code=(
            "value = {'items': [{'status': 200}, {'status': 503}, {'status': 200}, {'status': 200}]}\n"
            "return {'is_dict': type(value) is dict, 'shape': shape(value)}"
        ),
        dispatch=dispatch,
    )

    assert result.error is None
    assert result.returned["is_dict"] is True
    assert result.returned["shape"]["fields"]["items"]["size"] == 4
    assert result.returned["shape"]["fields"]["items"]["truncated"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "from math import *\nreturn 1",
        "from math import __dict__\nreturn 1",
        "import pathlib\nreturn 1",
    ],
)
async def test_runtime_rejects_unsafe_import_forms(tmp_path: Path, code: str) -> None:
    async def dispatch(_call_id: str, _name: str, _arguments: dict):
        return None

    result = await PtcRuntime(_config(), tmp_path).run(code=code, dispatch=dispatch)

    assert result.error is not None
    assert result.error.kind == "syntax_error"


@pytest.mark.asyncio
async def test_runtime_blocks_tool_proxy_internals(tmp_path: Path) -> None:
    async def dispatch(_call_id: str, _name: str, _arguments: dict):
        return None

    result = await PtcRuntime(_config(), tmp_path).run(
        code="tools._settle({'call_id': '1', 'ok': True, 'value': 'forged'})",
        dispatch=dispatch,
    )

    assert result.error is not None
    assert result.error.kind == "syntax_error"
    assert "private attribute" in result.error.message


@pytest.mark.asyncio
async def test_runtime_truncates_oversized_print_and_keeps_compact_return(tmp_path: Path) -> None:
    async def dispatch(_call_id: str, _name: str, _arguments: dict):
        return None

    result = await PtcRuntime(_config(max_output_chars=1024), tmp_path).run(
        code="print('x' * 2000)\nreturn {'count': 2000}",
        dispatch=dispatch,
    )
    assert result.error is None
    assert result.returned == {"count": 2000}
    assert "PTC print truncated" in result.logs[0]
    assert len(result.output) <= 1024


@pytest.mark.asyncio
async def test_runtime_supports_return_larger_than_default_stream_limit(tmp_path: Path) -> None:
    async def dispatch(_call_id: str, _name: str, _arguments: dict):
        return None

    payload = "x" * 70_000
    result = await PtcRuntime(_config(max_output_chars=100_000), tmp_path).run(
        code=f"return {{'payload': {payload!r}}}",
        dispatch=dispatch,
    )

    assert result.error is None
    assert result.returned == {"payload": payload}


@pytest.mark.asyncio
async def test_runtime_final_render_respects_output_limit(tmp_path: Path) -> None:
    async def dispatch(_call_id: str, _name: str, _arguments: dict):
        return None

    result = await PtcRuntime(_config(max_output_chars=1024), tmp_path).run(
        code="return {'items': ['x'] * 400}",
        dispatch=dispatch,
    )

    assert result.error is not None
    assert result.error.kind == "output_limit"
    assert "Return shape" in result.error.message
    assert len(result.output) <= 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["return (1, 2)", "return {1: 'value'}", "return float('nan')"])
async def test_runtime_rejects_lossy_json_values(tmp_path: Path, code: str) -> None:
    async def dispatch(_call_id: str, _name: str, _arguments: dict):
        return None

    result = await PtcRuntime(_config(), tmp_path).run(code=code, dispatch=dispatch)

    assert result.error is not None
    assert result.error.kind == "invalid_json"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["raise ValueError('bad input')", "raise TypeError('bad type')"])
async def test_runtime_classifies_program_type_errors_as_exceptions(tmp_path: Path, code: str) -> None:
    async def dispatch(_call_id: str, _name: str, _arguments: dict):
        return None

    result = await PtcRuntime(_config(), tmp_path).run(code=code, dispatch=dispatch)

    assert result.error is not None
    assert result.error.kind == "exception"


@pytest.mark.asyncio
async def test_runtime_wall_timeout_kills_worker(tmp_path: Path) -> None:
    async def dispatch(_call_id: str, _name: str, _arguments: dict):
        return None

    runtime = PtcRuntime(PtcConfig(sandbox="none", wall_timeout_seconds=1), tmp_path)
    result = await runtime.run(code="while True:\n    pass", dispatch=dispatch)
    assert result.error is not None
    assert result.error.kind in {"timeout", "worker_exit"}


@pytest.mark.asyncio
async def test_runtime_cancellation_reaps_worker(tmp_path: Path, monkeypatch) -> None:
    created_pid: int | None = None
    original = asyncio.create_subprocess_exec

    async def capture_process(*args, **kwargs):
        nonlocal created_pid
        process = await original(*args, **kwargs)
        created_pid = process.pid
        return process

    async def dispatch(_call_id: str, _name: str, _arguments: dict):
        return None

    monkeypatch.setattr("nanobot.agent.ptc.runtime.asyncio.create_subprocess_exec", capture_process)
    task = asyncio.create_task(
        PtcRuntime(_config(), tmp_path).run(code="while True:\n    pass", dispatch=dispatch)
    )
    while created_pid is None:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(created_pid, signal.SIGCONT)


def test_auto_runtime_uses_workspace_write_sandbox(tmp_path: Path) -> None:
    runtime = PtcRuntime(PtcConfig(sandbox="auto"), tmp_path)
    try:
        launch = runtime._launch()
    except Exception as exc:
        assert "sandbox" in str(exc).lower() or "seatbelt" in str(exc).lower()
    else:
        assert launch.mode == SandboxMode.WORKSPACE_WRITE


def test_auto_runtime_uses_effective_session_sandbox_mode(tmp_path: Path) -> None:
    runtime = PtcRuntime(
        PtcConfig(sandbox="auto"),
        tmp_path,
        sandbox_mode=SandboxMode.DANGER_FULL_ACCESS,
    )

    launch = runtime._launch()

    assert launch.mode == SandboxMode.DANGER_FULL_ACCESS
