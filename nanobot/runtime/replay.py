"""Small deterministic LLM cassette recorder/replayer for CI Agent smoke tests."""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

ReplayMode = Literal["record", "replay"]
_VOLATILE_KEYS = frozenset({
    "timestamp",
    "created_at",
    "updated_at",
    "usage",
    "latency_ms",
    "request_id",
    "turn_id",
})


class CassetteMismatchError(AssertionError):
    pass


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _stable(item)
            for key, item in sorted(value.items())
            if key not in _VOLATILE_KEYS and not key.startswith("_")
        }
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


def _request_summary(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    model: str | None,
) -> dict[str, Any]:
    summary = {
        "model": model,
        "messages": _stable(messages),
        "tool_names": sorted(
            str((tool.get("function") or {}).get("name") or tool.get("name") or "")
            for tool in (tools or [])
        ),
    }
    raw = json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {**summary, "hash": hashlib.sha256(raw.encode("utf-8")).hexdigest()}


def _response_dict(response: LLMResponse) -> dict[str, Any]:
    return {
        "content": response.content,
        "finish_reason": response.finish_reason,
        "reasoning_content": response.reasoning_content,
        "thinking_blocks": response.thinking_blocks,
        "tool_calls": [
            {
                "id": call.id,
                "name": call.name,
                "arguments": _stable(call.arguments),
                "extra_content": _stable(call.extra_content),
            }
            for call in response.tool_calls
        ],
    }


def _response_from_dict(raw: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        content=raw.get("content"),
        finish_reason=str(raw.get("finish_reason") or "stop"),
        reasoning_content=raw.get("reasoning_content"),
        thinking_blocks=raw.get("thinking_blocks"),
        tool_calls=[
            ToolCallRequest(
                id=str(call["id"]),
                name=str(call["name"]),
                arguments=call.get("arguments") or {},
                extra_content=call.get("extra_content"),
            )
            for call in raw.get("tool_calls", [])
            if isinstance(call, dict)
        ],
    )


class CassetteProvider(LLMProvider):
    def __init__(
        self,
        cassette: str | Path,
        *,
        mode: ReplayMode,
        delegate: LLMProvider | None = None,
        model: str = "cassette-model",
    ) -> None:
        super().__init__(api_key=None, api_base=None)
        self.cassette = Path(cassette)
        self.mode = mode
        self.delegate = delegate
        self.model = model
        self.calls = 0
        self._rows = self._load_rows() if mode == "replay" else []
        self._cursor = 0

    def get_default_model(self) -> str:
        return self.model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        del max_tokens, temperature, reasoning_effort, tool_choice
        self.calls += 1
        request = _request_summary(messages, tools, model or self.model)
        if self.mode == "record":
            if self.delegate is None:
                raise RuntimeError("record mode requires a delegate provider")
            response = await self.delegate.chat(messages=messages, tools=tools, model=model)
            self._append({"kind": "llm", "request": request, "response": _response_dict(response)})
            return response

        if self._cursor >= len(self._rows):
            raise CassetteMismatchError(
                f"cassette exhausted after {self._cursor} LLM calls; got request {request['hash']}"
            )
        row = self._rows[self._cursor]
        self._cursor += 1
        expected = row.get("request") if isinstance(row, dict) else None
        if not isinstance(expected, dict) or expected.get("hash") != request["hash"]:
            expected_text = json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True)
            actual_text = json.dumps(request, ensure_ascii=False, indent=2, sort_keys=True)
            diff = "\n".join(difflib.unified_diff(
                expected_text.splitlines(),
                actual_text.splitlines(),
                fromfile="cassette",
                tofile="runtime",
                lineterm="",
            ))
            raise CassetteMismatchError(
                f"cassette request mismatch at row {self._cursor}:\n{diff}"
            )
        response = row.get("response")
        if not isinstance(response, dict):
            raise CassetteMismatchError(f"cassette row {self._cursor} has no response")
        return _response_from_dict(response)

    def record_control_event(self, kind: str, payload: dict[str, Any]) -> None:
        if self.mode != "record":
            return
        self._append({"kind": kind, "payload": _stable(payload)})

    def assert_consumed(self) -> None:
        if self.mode == "replay" and self._cursor != len(self._rows):
            raise CassetteMismatchError(
                f"cassette has {len(self._rows) - self._cursor} unused LLM row(s)"
            )

    def _load_rows(self) -> list[dict[str, Any]]:
        if not self.cassette.exists():
            raise FileNotFoundError(self.cassette)
        rows: list[dict[str, Any]] = []
        for line_no, line in enumerate(self.cassette.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            raw = json.loads(line)
            if isinstance(raw, dict) and raw.get("kind") == "llm":
                rows.append(raw)
            elif not isinstance(raw, dict):
                raise CassetteMismatchError(f"invalid cassette row {line_no}")
        return rows

    def _append(self, row: dict[str, Any]) -> None:
        self.cassette.parent.mkdir(parents=True, exist_ok=True)
        with self.cassette.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
