from __future__ import annotations

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.config.loader import save_config
from nanobot.config.schema import Config
from nanobot.providers.factory import build_provider_snapshot, build_startup_provider_snapshot, load_provider_snapshot
from nanobot.providers.unconfigured_provider import UnconfiguredProvider
from nanobot.webui.settings_api import update_provider_settings


def _config_for(provider: str, model: str) -> Config:
    config = Config()
    config.agents.defaults.model_preset = None
    config.agents.defaults.provider = provider
    config.agents.defaults.model = model
    return config


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_name", "model"),
    [("deepseek", "deepseek-v4-pro"), ("openai", "gpt-4.1-mini")],
)
async def test_startup_snapshot_tolerates_missing_api_key(
    provider_name: str,
    model: str,
) -> None:
    config = _config_for(provider_name, model)

    snapshot = build_startup_provider_snapshot(config)

    assert isinstance(snapshot.provider, UnconfiguredProvider)
    assert snapshot.model == model
    response = await snapshot.provider.chat(messages=[], tools=[{"type": "function"}])
    assert response.finish_reason == "error"
    assert response.error_kind == "configuration"
    assert response.error_should_retry is False
    assert response.tool_calls == []
    assert "设置 → Providers" in (response.content or "")


def test_strict_snapshot_still_rejects_missing_api_key() -> None:
    with pytest.raises(ValueError, match="No API key configured"):
        build_provider_snapshot(_config_for("deepseek", "deepseek-v4-pro"))


def test_startup_snapshot_does_not_hide_non_credential_errors() -> None:
    config = _config_for("azure_openai", "gpt-4.1-mini")

    with pytest.raises(ValueError, match="Azure OpenAI requires api_base"):
        build_startup_provider_snapshot(config)


def test_saved_provider_key_replaces_placeholder_on_next_turn(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = _config_for("deepseek", "deepseek-v4-pro")
    config.agents.defaults.workspace = str(tmp_path / "workspace")
    save_config(config, config_path)
    monkeypatch.setattr("nanobot.config.loader._current_config_path", config_path)

    startup_snapshot = build_startup_provider_snapshot(config)
    loop = AgentLoop.from_config(
        config,
        bus=MessageBus(),
        provider=startup_snapshot.provider,
        model=startup_snapshot.model,
        context_window_tokens=startup_snapshot.context_window_tokens,
        provider_signature=startup_snapshot.signature,
        provider_snapshot_loader=lambda: load_provider_snapshot(config_path),
    )

    loop._refresh_provider_snapshot()
    assert isinstance(loop.provider, UnconfiguredProvider)

    payload = update_provider_settings(
        {"provider": ["deepseek"], "api_key": ["sk-configured-in-webui"]}
    )
    loop._refresh_provider_snapshot()

    assert payload["agent"]["has_api_key"] is True
    provider = next(row for row in payload["providers"] if row["name"] == "deepseek")
    assert provider["configured"] is True
    assert provider["api_key_hint"] != "sk-configured-in-webui"
    assert not isinstance(loop.provider, UnconfiguredProvider)
    assert loop.model == "deepseek-v4-pro"
