"""Safe placeholder used while a chat provider has not been configured yet."""

from __future__ import annotations

from typing import Any

from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse


class UnconfiguredProvider(LLMProvider):
    """Return setup guidance without making a network request or running tools."""

    def __init__(self, provider_name: str, default_model: str) -> None:
        self.provider_name = provider_name or "当前模型提供商"
        self.default_model = default_model
        self.generation = GenerationSettings()

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
        """Return a deterministic setup prompt without touching external services."""
        _ = messages, tools, model, max_tokens, temperature, reasoning_effort, tool_choice
        return LLMResponse(
            content=(
                f"当前模型提供商“{self.provider_name}”尚未配置 API Key。"
                "请打开 WebUI 的“设置 → Providers”，填写并保存对应 API Key 后，"
                "再发送消息即可使用，无需重启网关。"
            ),
            finish_reason="error",
            error_kind="configuration",
            error_should_retry=False,
        )

    def get_default_model(self) -> str:
        return self.default_model
