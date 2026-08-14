"""Host-side lifecycle for an isolated PTC Python worker."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from nanobot.agent.ptc.protocol import (
    PtcRunError,
    PtcRunResult,
    decode_message,
    encode_message,
    lossless_json,
)
from nanobot.config.schema import PtcConfig
from nanobot.security.sandbox import SandboxLauncher, SandboxMode

ToolDispatcher = Callable[[str, str, dict[str, Any]], Awaitable[Any]]


class PtcRuntime:
    """Execute one model-authored program in a fresh Python subprocess."""

    def __init__(
        self,
        config: PtcConfig,
        workspace: Path,
        sandbox_mode: SandboxMode | None = None,
    ) -> None:
        self.config = config
        self.workspace = workspace.expanduser().resolve(strict=False)
        self.sandbox_mode = sandbox_mode

    def _launch(self):
        worker = Path(__file__).with_name("worker.py").resolve()
        argv = (sys.executable, "-I", "-u", str(worker))
        env = {
            "HOME": str(self.workspace),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": os.environ.get("PATH", os.defpath),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
        mode = (
            self.sandbox_mode or SandboxMode.WORKSPACE_WRITE
            if self.config.sandbox == "auto"
            else SandboxMode.DANGER_FULL_ACCESS
        )
        return SandboxLauncher().prepare_argv(
            argv=argv,
            command_text="mybot PTC Python worker",
            workspace=self.workspace,
            cwd=self.workspace,
            env=env,
            mode=mode,
        )

    async def run(
        self,
        *,
        code: str,
        dispatch: ToolDispatcher,
    ) -> PtcRunResult:
        try:
            launch = self._launch()
        except Exception as exc:
            return PtcRunResult(error=PtcRunError("worker_exit", f"sandbox unavailable: {exc}"))

        process = await asyncio.create_subprocess_exec(
            *launch.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=launch.cwd,
            env=launch.env,
            start_new_session=sys.platform != "win32",
            # One JSON line can contain the entire curated return value. Account for
            # UTF-8 expansion and JSON escaping beyond StreamReader's 64 KiB default.
            limit=max(1_048_576, self.config.max_output_chars * 8 + 65_536),
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        write_lock = asyncio.Lock()
        dispatch_tasks: set[asyncio.Task[None]] = set()
        seen_call_ids: set[str] = set()
        result = PtcRunResult()

        async def send(message: dict[str, Any]) -> None:
            async with write_lock:
                if process.stdin is None or process.stdin.is_closing():
                    return
                process.stdin.write(encode_message(message))
                await process.stdin.drain()

        async def handle_call(message: dict[str, Any]) -> None:
            call_id = str(message.get("call_id") or "")
            name = str(message.get("name") or "")
            arguments = message.get("arguments")
            if not call_id or not name or not isinstance(arguments, dict):
                await send({
                    "type": "tool.result",
                    "call_id": call_id,
                    "name": name,
                    "ok": False,
                    "error": "invalid tool.call message",
                })
                return
            try:
                value = lossless_json(await dispatch(call_id, name, arguments))
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                suspension = getattr(exc, "stop_reason", None)
                if suspension:
                    result.suspension = exc
                await send({
                    "type": "tool.result",
                    "call_id": call_id,
                    "name": name,
                    "ok": False,
                    "error": str(exc),
                })
                if suspension:
                    await self._terminate(process)
            else:
                await send({
                    "type": "tool.result",
                    "call_id": call_id,
                    "name": name,
                    "ok": True,
                    "value": value,
                })

        async def read_stdout() -> None:
            while True:
                line = await process.stdout.readline()
                if not line:
                    return
                message = decode_message(line)
                kind = message.get("type")
                if kind == "tool.call":
                    call_id = str(message.get("call_id") or "")
                    if not call_id or call_id in seen_call_ids:
                        await send({
                            "type": "tool.result",
                            "call_id": call_id,
                            "name": str(message.get("name") or ""),
                            "ok": False,
                            "error": "duplicate or empty tool call id",
                        })
                        continue
                    seen_call_ids.add(call_id)
                    task = asyncio.create_task(handle_call(message))
                    dispatch_tasks.add(task)
                    task.add_done_callback(dispatch_tasks.discard)
                    continue
                if kind != "done":
                    raise ValueError(f"unexpected PTC worker message type: {kind!r}")
                logs = message.get("logs")
                if isinstance(logs, list):
                    result.logs = [str(item) for item in logs]
                if "returned" in message:
                    result.returned = lossless_json(message.get("returned"))
                    result.returned_present = True
                error = message.get("error")
                if isinstance(error, dict):
                    error_kind = str(error.get("kind") or "exception")
                    known = {
                        "syntax_error", "exception", "timeout", "cancelled",
                        "output_limit", "worker_exit", "invalid_json", "approval_required",
                    }
                    result.error = PtcRunError(
                        error_kind if error_kind in known else "exception",  # type: ignore[arg-type]
                        str(error.get("message") or "PTC program failed"),
                    )
                return

        try:
            await send({
                "type": "run",
                "code": code,
                "compute_timeout_seconds": self.config.compute_timeout_seconds,
                "max_output_chars": self.config.max_output_chars,
            })
            await asyncio.wait_for(read_stdout(), timeout=self.config.wall_timeout_seconds)
            if dispatch_tasks:
                for task in dispatch_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*dispatch_tasks, return_exceptions=True)
            stderr = (await process.stderr.read()).decode("utf-8", errors="replace").strip()
            await process.wait()
            if result.suspension is not None:
                result.error = PtcRunError("approval_required", str(result.suspension))
            elif result.error is None and process.returncode != 0:
                if process.returncode in {-getattr(signal, "SIGXCPU", 24), -signal.SIGKILL}:
                    result.error = PtcRunError(
                        "timeout",
                        f"PTC program exceeded {self.config.compute_timeout_seconds}s compute timeout",
                    )
                else:
                    result.error = PtcRunError(
                        "worker_exit",
                        stderr or f"PTC worker exited with code {process.returncode}",
                    )
        except asyncio.TimeoutError:
            result.error = PtcRunError(
                "timeout",
                f"PTC program exceeded {self.config.wall_timeout_seconds}s wall timeout",
            )
            await self._terminate(process)
        except asyncio.CancelledError:
            await self._terminate(process)
            raise
        except (ValueError, TypeError) as exc:
            result.error = PtcRunError("invalid_json", str(exc))
            await self._terminate(process)
        except BaseException as exc:
            result.error = PtcRunError("worker_exit", f"{type(exc).__name__}: {exc}")
            await self._terminate(process)
        finally:
            for task in dispatch_tasks:
                if not task.done():
                    task.cancel()
            if dispatch_tasks:
                await asyncio.gather(*dispatch_tasks, return_exceptions=True)
            if process.returncode is None:
                await self._terminate(process)

        result.output = self._render_output(result)
        if len(result.output) > self.config.max_output_chars:
            result.logs = []
            result.returned = None
            result.returned_present = False
            result.error = PtcRunError("output_limit", "PTC output limit exceeded")
            result.output = self._render_output(result)
        return result

    @staticmethod
    def _render_output(result: PtcRunResult) -> str:
        parts = list(result.logs)
        if result.returned_present:
            import json

            parts.append(json.dumps(result.returned, ensure_ascii=False, indent=2))
        if result.error is not None:
            parts.append(f"Error: ptc_{result.error.kind}: {result.error.message}")
        return "\n".join(part for part in parts if part) or "(no output)"

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if sys.platform != "win32":
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            with suppress(ProcessLookupError):
                process.kill()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5)
