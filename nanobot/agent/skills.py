"""Skills loader for agent capabilities."""

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from nanobot.agent.skill_manifest import SkillManifest
from nanobot.officecli_runtime import OfficeCliBootstrapError, select_officecli_asset

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"
_PACKAGED_CONSOLE_SCRIPTS = {"officecli"}

# Opening ---, YAML body (group 1), closing --- on its own line; supports CRLF.
_STRIP_SKILL_FRONTMATTER = re.compile(
    r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?",
    re.DOTALL,
)


def _which_command(command: str) -> str | None:
    """Resolve a requirement from PATH or the active Python scripts directory."""
    if resolved := shutil.which(command):
        return resolved
    if command not in _PACKAGED_CONSOLE_SCRIPTS:
        return None

    scripts_dir = Path(sys.executable).parent
    suffixes = (".exe", ".cmd", ".bat", "") if os.name == "nt" else ("",)
    for suffix in suffixes:
        candidate = scripts_dir / f"{command}{suffix}"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


class SkillsLoader:
    """
    Loader for agent skills.

    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.
    """

    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None, disabled_skills: set[str] | None = None):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR
        self.disabled_skills = disabled_skills or set()

    def _skill_dir(self, name: str) -> Path | None:
        for root in (self.workspace_skills, self.builtin_skills):
            if root:
                path = root / name
                if (path / "SKILL.md").is_file():
                    return path
        return None

    def _skill_entries_from_dir(self, base: Path, source: str, *, skip_names: set[str] | None = None) -> list[dict[str, str]]:
        if not base.exists():
            return []
        entries: list[dict[str, str]] = []
        for skill_dir in base.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            name = skill_dir.name
            if skip_names is not None and name in skip_names:
                continue
            entries.append({"name": name, "path": str(skill_file), "source": source})
        return entries

    def list_skills(
        self,
        filter_unavailable: bool = True,
        *,
        include_disabled: bool = False,
    ) -> list[dict[str, str]]:
        """
        List all available skills.

        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.

        Returns:
            List of skill info dicts with 'name', 'path', 'source'.
        """
        skills = self._skill_entries_from_dir(self.workspace_skills, "workspace")
        workspace_names = {entry["name"] for entry in skills}
        if self.builtin_skills and self.builtin_skills.exists():
            skills.extend(
                self._skill_entries_from_dir(self.builtin_skills, "builtin", skip_names=workspace_names)
            )

        if self.disabled_skills and not include_disabled:
            skills = [s for s in skills if s["name"] not in self.disabled_skills]

        if filter_unavailable:
            return [skill for skill in skills if self.get_skill_status(skill["name"])["available"]]
        return skills

    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.

        Args:
            name: Skill name (directory name).

        Returns:
            Skill content or None if not found.
        """
        roots = [self.workspace_skills]
        if self.builtin_skills:
            roots.append(self.builtin_skills)
        for root in roots:
            path = root / name / "SKILL.md"
            if path.exists():
                return path.read_text(encoding="utf-8")
        return None

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Load specific skills for inclusion in agent context.

        Args:
            skill_names: List of skill names to load.

        Returns:
            Formatted skills content.
        """
        parts = [
            f"### Skill: {name}\n\n{self._strip_frontmatter(markdown)}"
            for name in skill_names
            if self.get_skill_status(name)["available"]
            and (markdown := self.load_skill(name))
        ]
        return "\n\n---\n\n".join(parts)

    def build_skills_summary(self, exclude: set[str] | None = None) -> str:
        """
        Build a summary of all skills (name, description, path, availability).

        This is used for progressive loading - the agent can read the full
        skill content using read_file when needed.

        Args:
            exclude: Set of skill names to omit from the summary.

        Returns:
            Markdown-formatted skills summary.
        """
        all_skills = self.list_skills(filter_unavailable=True)
        if not all_skills:
            return ""

        lines: list[str] = []
        for entry in all_skills:
            skill_name = entry["name"]
            if exclude and skill_name in exclude:
                continue
            desc = self._get_skill_description(skill_name)
            lines.append(f"- **{skill_name}** — {desc}  `{entry['path']}`")
        return "\n".join(lines)

    @staticmethod
    def _reason(code: str, message: str, *, field: str | None = None) -> dict[str, str]:
        reason = {"code": code, "message": message}
        if field:
            reason["field"] = field
        return reason

    def _load_manifest(self, name: str) -> tuple[SkillManifest | None, list[dict[str, str]], bool]:
        skill_dir = self._skill_dir(name)
        manifest_path = skill_dir / "skill.yaml" if skill_dir else None
        if manifest_path is None or not manifest_path.is_file():
            return None, [], False
        try:
            raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            return None, [self._reason("invalid_manifest", str(exc), field="skill.yaml")], True
        try:
            manifest = SkillManifest.model_validate(raw)
        except ValidationError as exc:
            reasons = [
                self._reason(
                    "invalid_manifest",
                    error["msg"],
                    field=".".join(str(part) for part in error["loc"]),
                )
                for error in exc.errors()
            ]
            return None, reasons, True
        if manifest.name != name:
            return None, [
                self._reason(
                    "invalid_manifest",
                    f"manifest name {manifest.name!r} must match directory {name!r}",
                    field="name",
                )
            ], True
        return manifest, [], True

    @staticmethod
    def _safe_relative_path(base: Path, value: str) -> Path | None:
        relative = Path(value)
        if not value or relative.is_absolute() or ".." in relative.parts:
            return None
        resolved = (base / relative).resolve(strict=False)
        try:
            resolved.relative_to(base.resolve(strict=False))
        except ValueError:
            return None
        return resolved

    def _manifest_reasons(
        self,
        name: str,
        manifest: SkillManifest,
    ) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
        skill_dir = self._skill_dir(name)
        if skill_dir is None:
            return [self._reason("invalid_manifest", "skill directory is missing")], []
        reasons: list[dict[str, str]] = []
        providers: list[dict[str, Any]] = []
        for index, entrypoint in enumerate(manifest.entrypoints):
            path = self._safe_relative_path(skill_dir, entrypoint)
            if path is None:
                reasons.append(
                    self._reason(
                        "invalid_manifest",
                        "entrypoint must be a safe relative path",
                        field=f"entrypoints.{index}",
                    )
                )
            elif not path.is_file():
                reasons.append(
                    self._reason(
                        "missing_entrypoint",
                        f"entrypoint does not exist: {entrypoint}",
                        field=f"entrypoints.{index}",
                    )
                )
        for provider_name, provider in manifest.providers.items():
            provider_reasons: list[dict[str, str]] = []
            if provider.contract:
                contract_path = self._safe_relative_path(skill_dir, provider.contract)
                if contract_path is None:
                    provider_reasons.append(
                        self._reason(
                            "invalid_manifest",
                            "provider contract must be a safe relative path",
                            field=f"providers.{provider_name}.contract",
                        )
                    )
                elif not contract_path.is_file():
                    provider_reasons.append(
                        self._reason(
                            "missing_contract",
                            f"provider contract does not exist: {provider.contract}",
                            field=f"providers.{provider_name}.contract",
                        )
                    )
                else:
                    try:
                        payload = json.loads(contract_path.read_text(encoding="utf-8"))
                        if not isinstance(payload, dict):
                            raise ValueError("contract root must be an object")
                        if provider_name == "officecli":
                            if payload.get("provider") != "officecli":
                                raise ValueError("contract provider must be 'officecli'")
                            select_officecli_asset(payload)
                    except (
                        OfficeCliBootstrapError,
                        OSError,
                        ValueError,
                        json.JSONDecodeError,
                    ) as exc:
                        provider_reasons.append(
                            self._reason(
                                "invalid_contract",
                                str(exc),
                                field=f"providers.{provider_name}.contract",
                            )
                        )
            provider_available = not provider_reasons
            providers.append(
                {
                    "name": provider_name,
                    "required": provider.required,
                    "contract": provider.contract,
                    "available": provider_available,
                    "reasons": provider_reasons,
                }
            )
            if provider.required:
                reasons.extend(provider_reasons)
        return reasons, providers

    def get_skill_status(self, name: str) -> dict[str, Any]:
        manifest, reasons, manifest_present = self._load_manifest(name)
        valid = not reasons
        providers: list[dict[str, Any]] = []
        if manifest is not None:
            manifest_reasons, providers = self._manifest_reasons(name, manifest)
            reasons.extend(manifest_reasons)
            valid = valid and not any(
                reason["code"] in {"invalid_manifest", "invalid_contract", "missing_contract"}
                for reason in manifest_reasons
            )

        requirements = self.get_skill_requirements(name)
        reasons.extend(
            self._reason("missing_binary", f"CLI: {value}", field="requires.bins")
            for value in requirements["missing_bins"]
        )
        reasons.extend(
            self._reason("missing_env", f"ENV: {value}", field="requires.env")
            for value in requirements["missing_env"]
        )
        enabled = name not in self.disabled_skills
        if not enabled:
            reasons.insert(0, self._reason("disabled", "Skill is disabled by configuration"))
        available = enabled and valid and not reasons
        status = (
            "disabled"
            if not enabled
            else "invalid"
            if not valid
            else "available"
            if available
            else "unavailable"
        )
        return {
            "enabled": enabled,
            "valid": valid,
            "available": available,
            "status": status,
            "reasons": reasons,
            "manifest_present": manifest_present,
            "manifest": manifest.model_dump() if manifest is not None else None,
            "providers": providers,
            "requirements": requirements,
        }

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """Get a description of missing requirements."""
        requires = skill_meta.get("requires", {})
        required_bins = requires.get("bins", [])
        required_env_vars = requires.get("env", [])
        return ", ".join(
            [f"CLI: {command_name}" for command_name in required_bins if not _which_command(command_name)]
            + [f"ENV: {env_name}" for env_name in required_env_vars if not os.environ.get(env_name)]
        )

    def get_skill_availability(self, name: str) -> tuple[bool, str]:
        """Return whether a skill can run and why not when it cannot."""
        status = self.get_skill_status(name)
        reason = ", ".join(item["message"] for item in status["reasons"])
        return status["available"], reason

    def get_skill_requirements(self, name: str) -> dict[str, list[str]]:
        """Return explicit command/env requirements and currently missing entries."""
        requires = self._get_skill_meta(name).get("requires", {})
        bins = [str(value) for value in requires.get("bins", [])]
        env = [str(value) for value in requires.get("env", [])]
        return {
            "bins": bins,
            "env": env,
            "missing_bins": [value for value in bins if not _which_command(value)],
            "missing_env": [value for value in env if not os.environ.get(value)],
        }

    def _get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        manifest, _, _ = self._load_manifest(name)
        if manifest and manifest.description.strip():
            return manifest.description.strip()
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name  # Fallback to skill name

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if not content.startswith("---"):
            return content
        match = _STRIP_SKILL_FRONTMATTER.match(content)
        if match:
            return content[match.end():].strip()
        return content

    def _parse_nanobot_metadata(self, raw: object) -> dict:
        """Extract nanobot/openclaw metadata from a frontmatter field.

        ``raw`` may be a dict (already parsed by yaml.safe_load) or a JSON str.
        """
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, str):
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {}
        else:
            return {}
        if not isinstance(data, dict):
            return {}
        payload = data.get("nanobot", data.get("openclaw", {}))
        return payload if isinstance(payload, dict) else {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """Check if skill requirements are met (bins, env vars)."""
        requires = skill_meta.get("requires", {})
        required_bins = requires.get("bins", [])
        required_env_vars = requires.get("env", [])
        return all(_which_command(cmd) for cmd in required_bins) and all(
            os.environ.get(var) for var in required_env_vars
        )

    def _get_skill_meta(self, name: str) -> dict:
        """Get nanobot metadata for a skill (cached in frontmatter)."""
        raw_meta = self.get_skill_metadata(name) or {}
        return self._parse_nanobot_metadata(raw_meta.get("metadata"))

    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        return [
            entry["name"]
            for entry in self.list_skills(filter_unavailable=True)
            if (meta := self.get_skill_metadata(entry["name"]) or {})
            and (
                self._parse_nanobot_metadata(meta.get("metadata")).get("always")
                or meta.get("always")
            )
        ]

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Get metadata from a skill's frontmatter.

        Args:
            name: Skill name.

        Returns:
            Metadata dict or None.
        """
        content = self.load_skill(name)
        if not content or not content.startswith("---"):
            return None
        match = _STRIP_SKILL_FRONTMATTER.match(content)
        if not match:
            return None
        try:
            parsed = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            return None
        if not isinstance(parsed, dict):
            return None
        # yaml.safe_load returns native types (int, bool, list, etc.);
        # keep values as-is so downstream consumers get correct types.
        metadata: dict[str, object] = {}
        for key, value in parsed.items():
            metadata[str(key)] = value
        return metadata
