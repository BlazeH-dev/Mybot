"""Model-facing entry point for durable Runtime questions."""

from __future__ import annotations

from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    ArraySchema,
    BooleanSchema,
    IntegerSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)


@tool_parameters(
    tool_parameters_schema(
        questions=ArraySchema(
            items=ObjectSchema(
                id=StringSchema("Stable question id."),
                question=StringSchema("Question shown to the user."),
                header=StringSchema("Short label."),
                options=ArraySchema(
                    items=ObjectSchema(
                        properties={
                            "label": StringSchema("Choice label."),
                            "description": StringSchema("Choice tradeoff."),
                        },
                        required=["label", "description"],
                    ),
                    nullable=True,
                ),
                multiple=BooleanSchema(
                    description="Allow selecting more than one option.",
                    default=False,
                    nullable=True,
                ),
                required=["id", "question", "header"],
            ),
            min_items=1,
            max_items=3,
        ),
        strategy=StringSchema(
            "Waiting strategy.",
            enum=["required", "auto_resolve"],
        ),
        timeout_seconds=IntegerSchema(
            description="Deadline for auto_resolve (60-240 seconds).",
            minimum=60,
            maximum=240,
            nullable=True,
        ),
        default=StringSchema(
            "Deterministic default used when auto_resolve reaches its deadline.",
            nullable=True,
        ),
        required=["questions", "strategy"],
    )
)
class RequestUserInputTool(Tool):
    """Suspend the current turn and ask the user a durable typed question."""

    _scopes = {"core", "subagent"}
    capability = "human_interaction"
    risk_level = "low"

    @property
    def name(self) -> str:
        return "request_user_input"

    @property
    def description(self) -> str:
        return (
            "Ask one to three short questions and suspend the current turn. "
            "Use required when work cannot continue without an answer. Use "
            "auto_resolve only for non-blocking preferences with a 60-240 second deadline."
        )

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        return "Error: request_user_input must be handled by the Runtime interaction gate."
