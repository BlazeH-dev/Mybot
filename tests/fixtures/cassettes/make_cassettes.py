"""Generate the four high-value, no-network Runtime cassette fixtures."""

from __future__ import annotations

import asyncio
from pathlib import Path

from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.runtime.replay import CassetteProvider


class OneShotProvider(LLMProvider):
    def __init__(self, response: LLMResponse) -> None:
        super().__init__()
        self.response = response

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return self.response

    def get_default_model(self) -> str:
        return "cassette-model"


CASES = {
    "plan_automatic": (
        "Create and automatically activate a plan.",
        LLMResponse(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[ToolCallRequest(
                id="call_plan_auto",
                name="plan",
                arguments={
                    "action": "create",
                    "goal": "Automatic plan",
                    "steps": [{"id": "one", "description": "Do work"}],
                },
            )],
        ),
    ),
    "plan_explicit": (
        "Create a plan-only plan and wait for confirmation.",
        LLMResponse(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[ToolCallRequest(
                id="call_plan_explicit",
                name="plan",
                arguments={
                    "action": "create",
                    "goal": "Explicit plan",
                    "steps": [{"id": "one", "description": "Do work"}],
                },
            )],
        ),
    ),
    "interaction_deadlines": (
        "Ask a non-blocking preference question.",
        LLMResponse(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[ToolCallRequest(
                id="call_question",
                name="request_user_input",
                arguments={
                    "questions": [{
                        "id": "style",
                        "header": "Style",
                        "question": "Use a concise style?",
                    }],
                    "strategy": "auto_resolve",
                    "timeout_seconds": 60,
                    "default": "concise",
                },
            )],
        ),
    ),
    "checkpoint_conflict": (
        "Resume from checkpoint and avoid overwriting a changed file.",
        LLMResponse(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[ToolCallRequest(
                id="call_read",
                name="read_file",
                arguments={"path": "changed.txt", "force": True},
            )],
        ),
    ),
}


async def main() -> None:
    root = Path(__file__).parent
    tools = [
        {"type": "function", "function": {"name": "plan"}},
        {"type": "function", "function": {"name": "request_user_input"}},
        {"type": "function", "function": {"name": "read_file"}},
    ]
    for name, (prompt, response) in CASES.items():
        path = root / f"{name}.jsonl"
        path.unlink(missing_ok=True)
        recorder = CassetteProvider(
            path,
            mode="record",
            delegate=OneShotProvider(response),
        )
        await recorder.chat(
            messages=[{"role": "user", "content": prompt}],
            tools=tools,
            model="cassette-model",
        )


if __name__ == "__main__":
    asyncio.run(main())
