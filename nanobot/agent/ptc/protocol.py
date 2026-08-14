"""Typed values shared by the PTC host runtime and worker protocol."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
PtcErrorKind: TypeAlias = Literal[
    "syntax_error",
    "exception",
    "timeout",
    "cancelled",
    "output_limit",
    "worker_exit",
    "invalid_json",
    "approval_required",
]


@dataclass(slots=True, frozen=True)
class PtcRunError:
    kind: PtcErrorKind
    message: str


@dataclass(slots=True)
class PtcRunResult:
    output: str = ""
    returned: JsonValue | None = None
    returned_present: bool = False
    logs: list[str] = field(default_factory=list)
    error: PtcRunError | None = None
    subcall_events: list[dict[str, Any]] = field(default_factory=list)
    suspension: BaseException | None = None


def lossless_json(value: Any) -> JsonValue:
    """Return a detached JSON value or raise ``TypeError``."""
    seen: set[int] = set()

    def validate(item: Any) -> None:
        if item is None or type(item) in {bool, int, str}:
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise TypeError("non-finite floats are not lossless JSON")
            return
        if type(item) is list:
            identity = id(item)
            if identity in seen:
                raise TypeError("recursive lists are not lossless JSON")
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
                raise TypeError("recursive objects are not lossless JSON")
            if any(type(key) is not str for key in item):
                raise TypeError("JSON object keys must be strings")
            seen.add(identity)
            try:
                for child in item.values():
                    validate(child)
            finally:
                seen.remove(identity)
            return
        raise TypeError(f"unsupported JSON value type: {type(item).__name__}")

    try:
        validate(value)
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        decoded = json.loads(encoded)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise TypeError(f"value is not lossless JSON: {exc}") from exc
    return decoded


def encode_message(message: dict[str, Any]) -> bytes:
    return (json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def decode_message(line: bytes) -> dict[str, Any]:
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON-RPC message: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("invalid JSON-RPC message: expected object")
    return value
