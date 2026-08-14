"""Stable Python SDK prompt generation for PTC Code Mode."""

from __future__ import annotations

import keyword
import re
from typing import Any

RUN_CODE_NAME = "run_code"


def run_code_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": RUN_CODE_NAME,
            "description": (
                "Execute one complete async Python tool workflow. Combine discovery, reads, "
                "filtering, and aggregation in this single call; do not use run_code as a REPL. "
                "Only small printed diagnostics and the compact returned JSON reach the model."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The body of an async Python function.",
                    },
                    "description": {
                        "type": "string",
                        "description": "A clear 5-10 word description shown in the UI.",
                    },
                },
                "required": ["code", "description"],
                "additionalProperties": False,
            },
        },
    }


def _schema_name(schema: dict[str, Any]) -> str:
    function = schema.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    return ""


def _py_type(schema: Any) -> str:
    if not isinstance(schema, dict):
        return "JsonValue"
    if "enum" in schema and isinstance(schema["enum"], list):
        literals = ", ".join(repr(item) for item in schema["enum"])
        return f"Literal[{literals}]" if literals else "JsonValue"
    raw_type = schema.get("type")
    nullable = isinstance(raw_type, list) and "null" in raw_type
    if isinstance(raw_type, list):
        raw_type = next((item for item in raw_type if item != "null"), None)
    mapping = {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "null": "None",
    }
    if raw_type == "array":
        result = f"list[{_py_type(schema.get('items'))}]"
    elif raw_type == "object":
        result = "dict[str, JsonValue]"
    else:
        result = mapping.get(raw_type, "JsonValue")
    return f"{result} | None" if nullable and result != "None" else result


def _safe_identifier(name: str) -> bool:
    return name.isidentifier() and not keyword.iskeyword(name) and not name.startswith("_")


def _class_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]", " ", value).title().replace(" ", "")
    if not normalized:
        normalized = "Value"
    return f"Tool{normalized}" if normalized[0].isdigit() else normalized


def _render_named_type(
    schema: Any,
    class_name: str,
    classes: list[str],
) -> str:
    if not isinstance(schema, dict):
        return "JsonValue"
    raw_type = schema.get("type")
    nullable = isinstance(raw_type, list) and "null" in raw_type
    if isinstance(raw_type, list):
        raw_type = next((item for item in raw_type if item != "null"), None)
    if raw_type == "array":
        result = f"list[{_render_named_type(schema.get('items'), class_name + 'Item', classes)}]"
    elif raw_type == "object" and isinstance(schema.get("properties"), dict):
        properties = schema["properties"]
        if not properties or any(not _safe_identifier(str(key)) for key in properties):
            result = "dict[str, JsonValue]"
        else:
            required = set(schema.get("required") or [])
            lines = [f"class {class_name}(TypedDict, total=False):"]
            for key in sorted(properties):
                annotation = _render_named_type(
                    properties[key],
                    class_name + _class_name(str(key)),
                    classes,
                )
                if key in required:
                    annotation = f"Required[{annotation}]"
                lines.append(f"    {key}: {annotation}")
            classes.append("\n".join(lines))
            result = class_name
    else:
        result = _py_type(schema)
    return f"{result} | None" if nullable and "None" not in result else result


def _method_name(name: str) -> str:
    return name if _safe_identifier(name) else "__getitem__"


def _render_args_type(name: str, parameters: Any) -> tuple[str, list[str]]:
    if not isinstance(parameters, dict) or parameters.get("type", "object") != "object":
        return "dict[str, JsonValue]", []
    properties = parameters.get("properties")
    if not isinstance(properties, dict) or not properties:
        return "dict[str, JsonValue]", []
    if any(not _safe_identifier(str(key)) for key in properties):
        return "dict[str, JsonValue]", []
    class_name = re.sub(r"[^A-Za-z0-9]", " ", name).title().replace(" ", "") + "Args"
    if not class_name or class_name[0].isdigit():
        class_name = f"Tool{class_name}"
    required = set(parameters.get("required") or [])
    lines = [f"class {class_name}(TypedDict, total=False):"]
    for key in sorted(properties):
        annotation = _py_type(properties[key])
        if key in required:
            annotation = f"Required[{annotation}]"
        lines.append(f"    {key}: {annotation}")
    return class_name, lines


def build_tools_sdk(definitions: list[dict[str, Any]]) -> str:
    classes: list[str] = []
    methods: list[str] = []
    exotic = False
    for schema in sorted(definitions, key=_schema_name):
        function = schema.get("function") if isinstance(schema, dict) else None
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name or name == RUN_CODE_NAME:
            continue
        args_type, class_lines = _render_args_type(name, function.get("parameters"))
        if class_lines:
            classes.append("\n".join(class_lines))
        result_type = "JsonValue"
        output_schema = function.get("x-output-schema")
        if isinstance(output_schema, dict):
            result_classes: list[str] = []
            result_type = _render_named_type(
                output_schema,
                _class_name(name) + "Result",
                result_classes,
            )
            classes.extend(result_classes)
        description = str(function.get("description") or "").replace("\n", " ").strip()
        if _safe_identifier(name):
            if description:
                methods.append(f"    # {description}")
            methods.append(f"    async def {name}(self, args: {args_type}) -> {result_type}: ...")
        else:
            exotic = True
    if exotic:
        methods.append("    def __getitem__(self, name: str) -> ToolFunction: ...")
    if not methods:
        methods.append("    pass")
    declarations = "\n\n".join(classes + ["class Tools(Protocol):\n" + "\n".join(methods)])
    return (
        "from typing import Any, Literal, Protocol, Required, TypeAlias, TypedDict\n\n"
        "JsonValue: TypeAlias = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]\n"
        "class ToolCallError(Exception):\n"
        "    tool_name: str\n"
        "class ToolFunction(Protocol):\n"
        "    async def __call__(self, args: dict[str, JsonValue]) -> JsonValue: ...\n\n"
        + declarations
        + "\n\n"
        "tools: Tools\n"
        "math: Any  # Safe pre-injected module; `import math` is also normalized.\n"
        "json: Any  # Safe pre-injected module; `import json` is also normalized.\n"
        "def shape(value: JsonValue) -> JsonValue: ...  # Bounded structure summary."
    )


def build_ptc_system_prompt(definitions: list[dict[str, Any]]) -> str:
    return (
        "# Programmatic Tool Calling\n\n"
        "Use `run_code` for multi-step tool workflows that benefit from loops, branching, "
        "parallel reads, or filtering intermediate results. Its `code` argument is the BODY "
        "of an async Python function. The runtime provides `tools`, `ToolCallError`, and a "
        "restricted `asyncio` helper supporting `gather` and `sleep`, safe `math`/`json`, "
        "one-argument `type`, and `shape(value)`. Other imports and direct host access are "
        "unavailable. The declarations below are STATIC TYPE STUBS for guidance; "
        "do not copy their imports or class definitions into the program.\n\n"
        "- Call tools with `await tools.name({...})`; use `await tools[\"tool-name\"]({...})` "
        "for names that are not Python identifiers.\n"
        "- Failed calls raise `ToolCallError` with `tool_name` and a human-readable message.\n"
        "- Treat one `run_code` call as a complete, stateless program: discover inputs, read them, "
        "compute, filter, and return the final answer without asking the model to inspect raw data.\n"
        "- Independent read-only calls may overlap with `asyncio.gather`; mutating and exclusive "
        "calls run alone in submission order.\n"
        "- Never print or return raw tool results. `print` is only for small diagnostics and may "
        "be truncated. Return only the compact final lossless-JSON answer.\n"
        "- Tool outputs are JSON values. Use declared result types when available; otherwise use "
        "`isinstance`/`shape` and handle plausible dict/list forms inside the same program.\n"
        "- Do not copy SDK imports. `math` and `json` are already available; allowlisted import "
        "statements are normalized only for compatibility.\n\n"
        "```python\n"
        + build_tools_sdk(definitions)
        + "\n```"
    )
