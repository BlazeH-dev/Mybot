from __future__ import annotations

import pytest

from nanobot.agent.ptc.sdk import build_ptc_system_prompt, build_tools_sdk, run_code_schema
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import Config, PtcConfig


class _Tool(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Use {self._name}"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"],
        }

    async def execute(self, **kwargs):
        return kwargs


class _TypedTool(_Tool):
    @property
    def output_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "records": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "status": {"type": "integer"},
                            "latency_ms": {"type": "integer"},
                        },
                        "required": ["status", "latency_ms"],
                    },
                },
            },
            "required": ["service", "records"],
        }


def test_tools_config_defaults_to_native_and_accepts_camel_case_ptc() -> None:
    config = Config.model_validate({
        "tools": {
            "mode": "both",
            "ptc": {"maxParallelSubCalls": 3, "wallTimeoutSeconds": 12, "sandbox": "none"},
        }
    })
    assert config.tools.mode == "both"
    assert config.tools.ptc.max_parallel_sub_calls == 3
    assert config.tools.ptc.wall_timeout_seconds == 12
    assert Config().tools.mode == "native"


def test_tools_config_rejects_invalid_mode() -> None:
    with pytest.raises(ValueError):
        Config.model_validate({"tools": {"mode": "automatic"}})


def test_sdk_is_stable_sorted_and_documents_exotic_names() -> None:
    registry = ToolRegistry()
    registry.register(_Tool("zebra"))
    registry.register(_Tool("my-tool"))
    registry.register(_Tool("alpha"))

    sdk = build_tools_sdk(registry.get_definitions())

    assert sdk.index("async def alpha") < sdk.index("async def zebra")
    assert "def __getitem__" in sdk
    assert "class AlphaArgs" in sdk
    assert "path: Required[str]" in sdk
    assert build_tools_sdk(registry.get_definitions()) == sdk
    prompt = build_ptc_system_prompt(registry.get_ptc_definitions())
    assert "asyncio.gather" in prompt
    assert "complete, stateless program" in prompt
    assert "Never print or return raw tool results" in prompt
    assert "math: Any" in prompt
    assert "json: Any" in prompt
    assert "def shape(" in prompt


def test_ptc_sdk_uses_output_schema_without_leaking_it_to_provider() -> None:
    registry = ToolRegistry()
    registry.register(_TypedTool("read_logs"))

    native = registry.get_definitions()
    ptc = registry.get_ptc_definitions()
    sdk = build_tools_sdk(ptc)

    assert "x-output-schema" not in native[0]["function"]
    assert "x-output-schema" in ptc[0]["function"]
    assert "class ReadLogsResultRecordsItem(TypedDict, total=False):" in sdk
    assert "latency_ms: Required[int]" in sdk
    assert "service: Required[str]" in sdk
    assert "async def read_logs(self, args: ReadLogsArgs) -> ReadLogsResult" in sdk


def test_run_code_schema_requires_code_and_description() -> None:
    schema = run_code_schema()["function"]
    assert schema["name"] == "run_code"
    assert schema["parameters"]["required"] == ["code", "description"]


def test_run_code_name_is_reserved() -> None:
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="reserved"):
        registry.register(_Tool("run_code"))


def test_ptc_config_limits_are_validated() -> None:
    with pytest.raises(ValueError):
        PtcConfig(max_parallel_sub_calls=0)
