"""Lightweight skill summaries for the WebUI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobot.agent.skills import SkillsLoader


def webui_skills_payload(
    workspace_path: Path,
    *,
    disabled_skills: set[str] | None = None,
) -> dict[str, Any]:
    """Return agent skills without leaking local filesystem paths."""
    loader = SkillsLoader(workspace_path, disabled_skills=disabled_skills)
    entries = sorted(
        loader.list_skills(filter_unavailable=False, include_disabled=True),
        key=lambda entry: (entry.get("source") != "workspace", entry["name"]),
    )
    return {"skills": [_skill_payload(loader, entry) for entry in entries]}


def webui_skill_detail_payload(
    workspace_path: Path,
    name: str,
    *,
    disabled_skills: set[str] | None = None,
) -> dict[str, Any] | None:
    """Return a single skill's safe detail payload."""
    loader = SkillsLoader(workspace_path, disabled_skills=disabled_skills)
    entries = loader.list_skills(filter_unavailable=False, include_disabled=True)
    entry = next((item for item in entries if item["name"] == name), None)
    if entry is None:
        return None
    status = loader.get_skill_status(name)
    return {
        **_skill_payload(loader, entry),
        "requirements": status["requirements"],
        "manifest": status["manifest"],
        "providers": status["providers"],
        "raw_markdown": loader.load_skill(name) or "",
    }


def _skill_payload(loader: SkillsLoader, entry: dict[str, str]) -> dict[str, Any]:
    name = entry["name"]
    status = loader.get_skill_status(name)
    manifest = status["manifest"] or {}
    return {
        "name": name,
        "description": loader._get_skill_description(name),
        "source": entry.get("source", "unknown"),
        "version": manifest.get("version"),
        "enabled": status["enabled"],
        "valid": status["valid"],
        "available": status["available"],
        "status": status["status"],
        "availability_reasons": status["reasons"],
        "unavailable_reason": ", ".join(
            reason["message"] for reason in status["reasons"]
        ),
        "permissions_required": (manifest.get("permissions") or {}).get("required", []),
        "tools_required": (manifest.get("tools") or {}).get("required", []),
    }
