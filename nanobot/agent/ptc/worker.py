"""Isolated stdlib-only worker for one PTC Python program."""

from __future__ import annotations

import ast
import asyncio
import builtins
import json
import math
import sys
import threading
import traceback
from typing import Any

_ALLOWED_MODULES = {"json": json, "math": math}


def _send(message: dict[str, Any]) -> None:
    sys.__stdout__.write(json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n")
    sys.__stdout__.flush()


class _InvalidJsonError(Exception):
    pass


def _lossless_json(value: Any) -> Any:
    seen: set[int] = set()

    def validate(item: Any) -> None:
        if item is None or type(item) in {bool, int, str}:
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise _InvalidJsonError("non-finite floats are not lossless JSON")
            return
        if type(item) is list:
            identity = id(item)
            if identity in seen:
                raise _InvalidJsonError("recursive lists are not lossless JSON")
            seen.add(identity)
            try:
                for child in item:
                    validate(child)
            finally:
                seen.remove(identity)
            return
        if type(item) is dict:
            identity = id(item)
            if identity in seen:
                raise _InvalidJsonError("recursive objects are not lossless JSON")
            if any(type(key) is not str for key in item):
                raise _InvalidJsonError("JSON object keys must be strings")
            seen.add(identity)
            try:
                for child in item.values():
                    validate(child)
            finally:
                seen.remove(identity)
            return
        raise _InvalidJsonError(f"unsupported JSON value type: {type(item).__name__}")

    validate(value)
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise _InvalidJsonError(str(exc)) from exc


class ToolCallError(Exception):
    def __init__(self, tool_name: str, message: str) -> None:
        super().__init__(message)
        self.tool_name = tool_name


class _Tools:
    def __init__(self) -> None:
        self._next_id = 0
        self._pending: dict[str, asyncio.Future[Any]] = {}

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    def __getitem__(self, name: str):
        async def call(arguments: dict[str, Any]) -> Any:
            if not isinstance(arguments, dict):
                raise TypeError("tool arguments must be a dict")
            self._next_id += 1
            call_id = str(self._next_id)
            future = asyncio.get_running_loop().create_future()
            self._pending[call_id] = future
            _send({"type": "tool.call", "call_id": call_id, "name": name, "arguments": arguments})
            return await future
        return call

    def _settle(self, message: dict[str, Any]) -> None:
        call_id = str(message.get("call_id") or "")
        future = self._pending.pop(call_id, None)
        if future is None or future.done():
            return
        if message.get("ok") is True:
            future.set_result(message.get("value"))
        else:
            future.set_exception(ToolCallError(str(message.get("name") or ""), str(message.get("error") or "tool call failed")))


class _AsyncioFacade:
    gather = staticmethod(asyncio.gather)
    sleep = staticmethod(asyncio.sleep)


def _safe_type(value: Any) -> type:
    """Expose one-argument type inspection without the three-argument constructor."""
    return builtins.type(value)


def _shape(value: Any, depth: int = 3, sample_size: int = 3) -> Any:
    """Return a bounded, JSON-safe structural description for unfamiliar tool results."""
    if depth <= 0:
        return {"type": builtins.type(value).__name__}
    if builtins.type(value) is dict:
        keys = list(value)[: max(0, sample_size)]
        return {
            "type": "object",
            "size": len(value),
            "fields": {str(key): _shape(value[key], depth - 1, sample_size) for key in keys},
            "truncated": len(value) > len(keys),
        }
    if builtins.type(value) is list:
        samples = value[: max(0, sample_size)]
        return {
            "type": "array",
            "size": len(value),
            "items": [_shape(item, depth - 1, sample_size) for item in samples],
            "truncated": len(value) > len(samples),
        }
    if builtins.type(value) is str:
        return {"type": "string", "length": len(value), "sample": value[:120]}
    if value is None or builtins.type(value) in {bool, int, float}:
        return {"type": builtins.type(value).__name__, "sample": value}
    return {"type": builtins.type(value).__name__}


class _ImportNormalizer(ast.NodeTransformer):
    """Turn allowlisted imports into references to pre-injected safe modules."""

    @staticmethod
    def _assign(target: str, value: ast.expr, source: ast.AST) -> ast.Assign:
        return ast.copy_location(
            ast.Assign(targets=[ast.Name(id=target, ctx=ast.Store())], value=value),
            source,
        )

    def visit_Import(self, node: ast.Import) -> list[ast.stmt]:
        statements: list[ast.stmt] = []
        for alias in node.names:
            if alias.name not in _ALLOWED_MODULES:
                raise SyntaxError(
                    f"import {alias.name!r} is unavailable; only math and json are allowed"
                )
            target = alias.asname or alias.name
            # Bare ``import math`` already resolves to the injected global. Rewriting it
            # as ``math = math`` inside the async function would make the RHS an
            # uninitialized local and raise UnboundLocalError.
            if target != alias.name:
                statements.append(
                    self._assign(target, ast.Name(id=alias.name, ctx=ast.Load()), node)
                )
        return statements

    def visit_ImportFrom(self, node: ast.ImportFrom) -> list[ast.stmt]:
        module = node.module or ""
        if node.level or module not in _ALLOWED_MODULES:
            raise SyntaxError(
                f"import from {module!r} is unavailable; only math and json are allowed"
            )
        statements: list[ast.stmt] = []
        for alias in node.names:
            if alias.name == "*" or alias.name.startswith("_"):
                raise SyntaxError("wildcard and private imports are unavailable in PTC programs")
            statements.append(
                self._assign(
                    alias.asname or alias.name,
                    ast.Attribute(
                        value=ast.Name(id=module, ctx=ast.Load()),
                        attr=alias.name,
                        ctx=ast.Load(),
                    ),
                    node,
                )
            )
        return statements


class _Validator(ast.NodeVisitor):
    _DENIED_NODES = (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)
    _DENIED_NAMES = {
        "open", "exec", "eval", "compile", "__import__", "globals", "locals",
        "vars", "input", "breakpoint", "help", "dir", "getattr", "setattr", "delattr",
    }

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, self._DENIED_NODES):
            raise SyntaxError(f"{type(node).__name__} is unavailable in PTC programs")
        super().generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in self._DENIED_NAMES or node.id.startswith("__"):
            raise SyntaxError(f"name {node.id!r} is unavailable in PTC programs")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_"):
            raise SyntaxError("private attribute access is unavailable in PTC programs")
        self.generic_visit(node)


_SAFE_BUILTINS = {
    name: getattr(builtins, name)
    for name in (
        "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int",
        "isinstance", "len", "list", "map", "max", "min", "next", "range", "repr",
        "reversed", "round", "set", "slice", "sorted", "str", "sum", "tuple", "zip",
        "Exception", "RuntimeError", "TypeError", "ValueError",
    )
}
_SAFE_BUILTINS["type"] = _safe_type


def _install_audit_guard() -> None:
    denied_exact = {
        "open",
        "os.system",
        "os.posix_spawn",
        "os.spawn",
        "subprocess.Popen",
        "socket.__new__",
        "socket.connect",
        "socket.bind",
        "ctypes.dlopen",
        "import",
    }
    denied_prefixes = ("socket.", "subprocess.")

    def guard(event: str, _args: tuple[Any, ...]) -> None:
        if event in denied_exact or event.startswith(denied_prefixes):
            raise PermissionError(f"audit event {event!r} is unavailable in PTC programs")

    sys.addaudithook(guard)


def _compile(code: str):
    source = "async def __ptc_main__():\n" + "".join(f"    {line}\n" for line in code.splitlines())
    if not code.strip():
        source += "    return None\n"
    tree = ast.parse(source, filename="<ptc>", mode="exec")
    tree = _ImportNormalizer().visit(tree)
    ast.fix_missing_locations(tree)
    _Validator().visit(tree)
    return compile(tree, "<ptc>", "exec")


def _start_response_reader(tools: _Tools, loop: asyncio.AbstractEventLoop) -> None:
    def read() -> None:
        for line in sys.stdin:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("type") == "tool.result":
                loop.call_soon_threadsafe(tools._settle, message)

    threading.Thread(target=read, name="ptc-rpc-reader", daemon=True).start()


async def _main(request: dict[str, Any]) -> None:
    max_output = int(request.get("max_output_chars") or 65_536)
    logs: list[str] = []
    output_size = 0
    max_log_output = max(128, min(8192, max_output // 4))

    def capture_print(*values: Any, sep: str = " ", end: str = "\n", **_: Any) -> None:
        nonlocal output_size
        text = sep.join(str(value) for value in values) + end
        remaining = max_log_output - output_size
        if remaining <= 0:
            return
        if len(text) > remaining:
            marker = "... [PTC print truncated; return only the compact final result]"
            prefix_size = max(0, remaining - len(marker))
            text = text[:prefix_size] + marker
        output_size += len(text)
        logs.append(text.rstrip("\n"))

    try:
        executable = _compile(str(request.get("code") or ""))
    except SyntaxError as exc:
        _send({"type": "done", "logs": logs, "error": {"kind": "syntax_error", "message": str(exc)}})
        return

    tools = _Tools()
    globals_dict = {
        "__builtins__": {**_SAFE_BUILTINS, "print": capture_print},
        "tools": tools,
        "ToolCallError": ToolCallError,
        "asyncio": _AsyncioFacade,
        "json": json,
        "math": math,
        "shape": _shape,
    }
    try:
        _install_audit_guard()
        exec(executable, globals_dict, globals_dict)
        _start_response_reader(tools, asyncio.get_running_loop())
        returned = _lossless_json(await globals_dict["__ptc_main__"]())
        encoded = json.dumps(returned, ensure_ascii=False, allow_nan=False)
        if output_size + len(encoded) > max_output:
            summary = json.dumps(_shape(returned), ensure_ascii=False, allow_nan=False)
            raise OverflowError(
                "PTC return value exceeds the output limit. "
                f"Return shape: {summary}. Compute and return the compact final answer "
                "inside the same run_code call instead of returning raw tool results."
            )
        output_size += len(encoded)
        _send({"type": "done", "logs": logs, "returned": returned})
    except OverflowError as exc:
        _send({"type": "done", "logs": logs, "error": {"kind": "output_limit", "message": str(exc)}})
    except _InvalidJsonError as exc:
        _send({"type": "done", "logs": logs, "error": {"kind": "invalid_json", "message": str(exc)}})
    except BaseException as exc:
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        _send({"type": "done", "logs": logs, "error": {"kind": "exception", "message": detail}})


if __name__ == "__main__":
    line = sys.stdin.readline()
    try:
        request = json.loads(line)
        if not isinstance(request, dict) or request.get("type") != "run":
            raise ValueError("expected run request")
        compute_seconds = int(request.get("compute_timeout_seconds") or 60)
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_CPU, (math.ceil(compute_seconds), math.ceil(compute_seconds) + 1))
        except (ImportError, ValueError, OSError):
            pass
        asyncio.run(_main(request))
    except BaseException as exc:
        _send({"type": "done", "logs": [], "error": {"kind": "worker_exit", "message": str(exc)}})
