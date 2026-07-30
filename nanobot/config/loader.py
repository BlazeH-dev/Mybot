"""Configuration loading utilities."""

import json
import os
import re
from pathlib import Path
from typing import Any

import pydantic
from loguru import logger
from pydantic import BaseModel

from nanobot.config.schema import Config, _resolve_tool_config_refs, default_model_presets

_RETIRED_BUILTIN_MODEL_PRESETS = {
    "gpt-5-5": {
        "label": "GPT-5.5",
        "model": "gpt-5.5",
        "provider": "openai",
        "maxTokens": 8192,
        "contextWindowTokens": 128000,
        "temperature": 0.1,
        "reasoningEffort": "medium",
    },
    "mimo-v2-5-pro": {
        "label": "MiMo V2.5 Pro",
        "model": "mimo-v2.5-pro",
        "provider": "xiaomi_mimo",
        "maxTokens": 8192,
        "contextWindowTokens": 65_536,
        "temperature": 0.1,
        "reasoningEffort": "medium",
    },
    "mimo-v2-5": {
        "label": "MiMo V2.5",
        "model": "mimo-v2.5",
        "provider": "xiaomi_mimo",
        "maxTokens": 8192,
        "contextWindowTokens": 65_536,
        "temperature": 0.1,
        "reasoningEffort": "medium",
    },
}

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None
_schema_refs_ready = False


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path."""
    if _current_config_path:
        return _current_config_path
    return Path.home() / ".nanobot" / "config.json"


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    global _schema_refs_ready
    if not _schema_refs_ready:
        _resolve_tool_config_refs()
        _schema_refs_ready = True

    path = config_path or get_config_path()

    config = Config()
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data = _migrate_config(data)
            config = Config.model_validate(data)
        except (json.JSONDecodeError, ValueError, pydantic.ValidationError) as e:
            logger.warning("Failed to load config from {}: {}", path, e)
            logger.warning("Using default configuration.")

    _apply_ssrf_whitelist(config)
    return config


def _apply_ssrf_whitelist(config: Config) -> None:
    """Apply SSRF whitelist from config to the network security module."""
    from nanobot.security.network import configure_ssrf_whitelist

    configure_ssrf_whitelist(config.tools.ssrf_whitelist)


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(mode="json", by_alias=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


_ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_config_env_vars(config: Config) -> Config:
    """Return *config* with ``${VAR}`` env-var references resolved.

    Walks in place so fields declared with ``exclude=True`` survive;
    returns the same instance when no references are present.
    Raises ``ValueError`` if a referenced variable is not set.
    """
    return _resolve_in_place(config)


def _resolve_in_place(obj: Any) -> Any:
    if isinstance(obj, str):
        new = _ENV_REF_PATTERN.sub(_env_replace, obj)
        return new if new != obj else obj
    if isinstance(obj, BaseModel):
        updates: dict[str, Any] = {}
        for name in type(obj).model_fields:
            old = getattr(obj, name)
            new = _resolve_in_place(old)
            if new is not old:
                updates[name] = new
        extras = obj.__pydantic_extra__
        new_extras: dict[str, Any] | None = None
        if extras:
            resolved = {k: _resolve_in_place(v) for k, v in extras.items()}
            if any(resolved[k] is not extras[k] for k in extras):
                new_extras = resolved
        if not updates and new_extras is None:
            return obj
        copy = obj.model_copy(update=updates) if updates else obj.model_copy()
        if new_extras is not None:
            copy.__pydantic_extra__ = new_extras
        return copy
    if isinstance(obj, dict):
        resolved = {k: _resolve_in_place(v) for k, v in obj.items()}
        return resolved if any(resolved[k] is not obj[k] for k in obj) else obj
    if isinstance(obj, list):
        resolved = [_resolve_in_place(v) for v in obj]
        return resolved if any(nv is not ov for nv, ov in zip(resolved, obj)) else obj
    return obj


def _resolve_env_vars(obj: object) -> object:
    """Recursively resolve ``${VAR}`` patterns in plain strings/dicts/lists."""
    if isinstance(obj, str):
        return _ENV_REF_PATTERN.sub(_env_replace, obj)
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(v) for v in obj]
    return obj


def _env_replace(match: re.Match[str]) -> str:
    name = match.group(1)
    value = os.environ.get(name)
    if value is None:
        raise ValueError(
            f"Environment variable '{name}' referenced in config is not set"
        )
    return value


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    retired_model_presets = _backfill_builtin_model_presets(data)
    _remove_retired_model_references(data, retired_model_presets)

    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")

    # Move tools.myEnabled / tools.mySet → tools.my.{enable, allowSet}.
    # The old flat keys shipped in the initial MyTool landing; wrapping them in a
    # sub-config keeps `web` / `exec` / `my` symmetric and gives room to grow.
    if "myEnabled" in tools or "mySet" in tools:
        my_cfg = tools.setdefault("my", {})
        if "myEnabled" in tools and "enable" not in my_cfg:
            my_cfg["enable"] = tools.pop("myEnabled")
        else:
            tools.pop("myEnabled", None)
        if "mySet" in tools and "allowSet" not in my_cfg:
            my_cfg["allowSet"] = tools.pop("mySet")
        else:
            tools.pop("mySet", None)

    # P1 retired the narrow office-automation Skill.
    agents = data.get("agents")
    if isinstance(agents, dict):
        defaults = agents.get("defaults")
        if isinstance(defaults, dict):
            disabled_key = (
                "disabledSkills" if "disabledSkills" in defaults else "disabled_skills"
            )
            disabled = defaults.get(disabled_key)
            if isinstance(disabled, list):
                defaults[disabled_key] = [
                    skill for skill in disabled if skill != "office-automation"
                ]

    return data


def _backfill_builtin_model_presets(data: dict) -> set[str]:
    """Merge Mybot's built-in presets into existing config without overwriting users."""
    key = "modelPresets" if "modelPresets" in data else "model_presets"
    presets = data.setdefault(key, {})
    if not isinstance(presets, dict):
        return set()

    retired = _remove_retired_builtin_model_presets(presets)

    for name, preset in default_model_presets().items():
        presets.setdefault(name, preset.model_dump(mode="json", by_alias=True))
    return retired


def _remove_retired_builtin_model_presets(presets: dict) -> set[str]:
    """Drop retired built-in presets while preserving user-modified entries."""
    aliases = {
        "maxTokens": "max_tokens",
        "contextWindowTokens": "context_window_tokens",
        "reasoningEffort": "reasoning_effort",
    }
    retired: set[str] = set()
    for name, expected in _RETIRED_BUILTIN_MODEL_PRESETS.items():
        current = presets.get(name)
        if not isinstance(current, dict):
            continue
        matches = all(
            current.get(key, current.get(aliases.get(key, key))) == value
            for key, value in expected.items()
        )
        if matches:
            presets.pop(name, None)
            retired.add(name)
    return retired


def _remove_retired_model_references(data: dict, retired: set[str]) -> None:
    """Keep active/fallback references valid when an exact built-in is retired."""
    if not retired:
        return
    agents = data.get("agents")
    if not isinstance(agents, dict):
        return
    defaults = agents.get("defaults")
    if not isinstance(defaults, dict):
        return

    preset_key = "modelPreset" if "modelPreset" in defaults else "model_preset"
    if defaults.get(preset_key) in retired:
        defaults[preset_key] = None

    fallback_key = "fallbackModels" if "fallbackModels" in defaults else "fallback_models"
    fallbacks = defaults.get(fallback_key)
    if isinstance(fallbacks, list):
        defaults[fallback_key] = [
            fallback
            for fallback in fallbacks
            if not (isinstance(fallback, str) and fallback in retired)
        ]
