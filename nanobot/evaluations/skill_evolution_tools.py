"""Restricted tools and activity hooks for Skill evolution editing runs."""

from __future__ import annotations

import asyncio
import difflib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nanobot.agent.hook import AgentHook, AgentHookContext, AgentRunHookContext
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    ArraySchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)

_EDITABLE_FILE = ("SKILL.md", "scripts/", "references/", "assets/")
_FROZEN_FILES = {"skill.yaml", "references/officecli-runtime.json"}
_MAX_TEXT_BYTES = 200_000


def _safe_relative(value: str, *, editable: bool = False) -> str:
    normalized = value.strip().replace("\\", "/")
    path = Path(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts or "\0" in normalized:
        raise ValueError("path must be a safe relative Skill path")
    if editable:
        if normalized in _FROZEN_FILES:
            raise ValueError(f"{normalized} is frozen")
        if not (normalized == "SKILL.md" or normalized.startswith(_EDITABLE_FILE[1:])):
            raise ValueError(f"path is outside the editable Skill surface: {normalized}")
    return normalized


class _SkillTool(Tool):
    def __init__(
        self,
        candidate: Path,
        emit: Callable[..., None],
        *,
        cancelled: Callable[[], bool],
    ) -> None:
        self.candidate = candidate.resolve()
        self.emit = emit
        self.cancelled = cancelled

    def _path(self, value: str, *, editable: bool = False) -> Path:
        if self.cancelled():
            raise RuntimeError("Skill evolution was cancelled")
        relative = _safe_relative(value, editable=editable)
        target = (self.candidate / relative).resolve(strict=False)
        target.relative_to(self.candidate)
        return target

    def _activity(self, *, status: str, label: str, file_path: str | None = None) -> None:
        self.emit(
            phase="editing",
            kind="file" if file_path else "tool",
            status=status,
            label=label,
            tool_name=self.name,
            file_path=file_path,
        )


@tool_parameters(tool_parameters_schema())
class ListSkillFilesTool(_SkillTool):
    @property
    def name(self) -> str:
        return "list_skill_files"

    @property
    def description(self) -> str:
        return "List files in the isolated candidate Skill directory."

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        del kwargs
        rows = [
            path.relative_to(self.candidate).as_posix()
            for path in sorted(self.candidate.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        ]
        self._activity(status="completed", label=f"Listed {len(rows)} Skill files")
        return "\n".join(rows)


@tool_parameters(tool_parameters_schema(
    path=StringSchema("Relative path inside the candidate Skill."),
    required=["path"],
))
class ReadSkillFileTool(_SkillTool):
    @property
    def name(self) -> str:
        return "read_skill_file"

    @property
    def description(self) -> str:
        return "Read one UTF-8 text file from the isolated candidate Skill."

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, path: str, **kwargs: Any) -> str:
        del kwargs
        target = self._path(path)
        if not target.is_file():
            raise ValueError(f"Skill file does not exist: {path}")
        text = target.read_text(encoding="utf-8")
        self._activity(status="completed", label="Read Skill file", file_path=path)
        return text[:128_000]


@tool_parameters(tool_parameters_schema(
    evidence_ids=ArraySchema(
        StringSchema("Evidence identifier from an approved finding."),
        min_items=1,
        max_items=20,
    ),
    required=["evidence_ids"],
))
class ReadEvolutionEvidenceTool(_SkillTool):
    def __init__(self, *args: Any, evidence: dict[str, Any], allowed_ids: set[str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.evidence = evidence
        self.allowed_ids = allowed_ids

    @property
    def name(self) -> str:
        return "read_evolution_evidence"

    @property
    def description(self) -> str:
        return "Read approved, redacted evidence by identifier."

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, evidence_ids: list[str], **kwargs: Any) -> str:
        del kwargs
        requested = {str(value) for value in evidence_ids}
        forbidden = requested - self.allowed_ids
        if forbidden:
            raise ValueError("evidence was not approved: " + ", ".join(sorted(forbidden)))
        rows = [self.evidence[value] for value in evidence_ids if value in self.evidence]
        self._activity(status="completed", label=f"Read {len(rows)} approved evidence items")
        return json.dumps(rows, ensure_ascii=False)[:128_000]


@tool_parameters(tool_parameters_schema(
    edits=ArraySchema(
        ObjectSchema(
            path=StringSchema("Relative editable Skill path."),
            old_text=StringSchema("Exact existing text to replace."),
            new_text=StringSchema("Replacement text."),
            required=["path", "old_text", "new_text"],
        ),
        min_items=1,
        max_items=20,
    ),
    required=["edits"],
))
class ApplySkillPatchTool(_SkillTool):
    @property
    def name(self) -> str:
        return "apply_skill_patch"

    @property
    def description(self) -> str:
        return "Apply exact text replacements to editable candidate Skill files."

    async def execute(self, edits: list[dict[str, Any]], **kwargs: Any) -> str:
        del kwargs
        pending: dict[Path, str] = {}
        summaries: list[str] = []
        for edit in edits:
            relative = str(edit["path"])
            target = self._path(relative, editable=True)
            if not target.is_file():
                raise ValueError(f"Skill file does not exist: {relative}")
            content = pending.get(target, target.read_text(encoding="utf-8"))
            old_text = str(edit["old_text"])
            new_text = str(edit["new_text"])
            count = content.count(old_text)
            if count != 1:
                raise ValueError(f"old_text must match exactly once in {relative}; found {count}")
            updated = content.replace(old_text, new_text, 1)
            if len(updated.encode()) > _MAX_TEXT_BYTES:
                raise ValueError(f"edited file is too large: {relative}")
            pending[target] = updated
            diff = list(difflib.ndiff(content.splitlines(), updated.splitlines()))
            summaries.append(f"{relative} (+{sum(x.startswith('+ ') for x in diff)}/-{sum(x.startswith('- ') for x in diff)})")
        for target, content in pending.items():
            target.write_text(content, encoding="utf-8")
        for summary in summaries:
            self._activity(status="completed", label="Patched Skill file", file_path=summary)
        return "\n".join(summaries)


@tool_parameters(tool_parameters_schema(
    path=StringSchema("Relative editable Skill path."),
    content=StringSchema("Complete UTF-8 file content", max_length=_MAX_TEXT_BYTES),
    required=["path", "content"],
))
class WriteSkillFileTool(_SkillTool):
    @property
    def name(self) -> str:
        return "write_skill_file"

    @property
    def description(self) -> str:
        return "Create or replace an editable UTF-8 file in the candidate Skill."

    async def execute(self, path: str, content: str, **kwargs: Any) -> str:
        del kwargs
        target = self._path(path, editable=True)
        if len(content.encode()) > _MAX_TEXT_BYTES:
            raise ValueError(f"file is too large: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._activity(status="completed", label="Wrote Skill file", file_path=path)
        return f"Wrote {path}"


@tool_parameters(tool_parameters_schema(
    path=StringSchema("Relative editable Skill path."),
    required=["path"],
))
class DeleteSkillFileTool(_SkillTool):
    @property
    def name(self) -> str:
        return "delete_skill_file"

    @property
    def description(self) -> str:
        return "Delete an editable file from the candidate Skill."

    async def execute(self, path: str, **kwargs: Any) -> str:
        del kwargs
        target = self._path(path, editable=True)
        if path == "SKILL.md":
            raise ValueError("SKILL.md is required and cannot be deleted")
        if not target.is_file():
            raise ValueError(f"Skill file does not exist: {path}")
        target.unlink()
        self._activity(status="completed", label="Deleted Skill file", file_path=path)
        return f"Deleted {path}"


@tool_parameters(tool_parameters_schema())
class ValidateSkillCandidateTool(_SkillTool):
    def __init__(self, *args: Any, validate: Callable[[], dict[str, Any]], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.validate = validate

    @property
    def name(self) -> str:
        return "validate_skill_candidate"

    @property
    def description(self) -> str:
        return "Run deterministic validation against the current candidate Skill."

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        del kwargs
        result = self.validate()
        self.emit(
            phase="validating",
            kind="validation",
            status="completed" if result.get("valid") else "failed",
            label="Candidate validation completed",
            detail="; ".join(result.get("errors") or [])[:1000],
            tool_name=self.name,
        )
        return json.dumps(result, ensure_ascii=False)


class SkillEvolutionAgentHook(AgentHook):
    """Persist sanitized Agent iteration and tool activity."""

    def __init__(self, emit: Callable[..., None], cancelled: Callable[[], bool]) -> None:
        super().__init__(reraise=True)
        self.emit = emit
        self.cancelled = cancelled

    async def before_run(self, context: AgentRunHookContext) -> None:
        del context
        self.emit(phase="editing", kind="model", status="started", label="Skill editor started")

    async def before_iteration(self, context: AgentHookContext) -> None:
        if self.cancelled():
            raise asyncio.CancelledError()
        self.emit(
            phase="editing",
            kind="model",
            status="running",
            label=f"Editor iteration {context.iteration + 1}",
        )

    async def after_iteration(self, context: AgentHookContext) -> None:
        for event in context.tool_events:
            self.emit(
                phase="editing",
                kind="tool",
                status="completed" if event.get("status") == "ok" else "failed",
                label=str(event.get("name") or "Tool call"),
                detail=str(event.get("detail") or "")[:500] or None,
                tool_name=str(event.get("name") or "") or None,
            )
        if context.usage:
            self.emit(
                phase="editing",
                kind="usage",
                status="running",
                label="Editor model usage updated",
                usage={
                    key: int(value)
                    for key, value in context.usage.items()
                    if isinstance(value, (int, float))
                },
            )

    async def after_run(self, context: AgentRunHookContext) -> None:
        self.emit(
            phase="editing",
            kind="model",
            status="failed" if context.error else "completed",
            label="Skill editor finished",
            detail=context.error,
            usage={
                key: int(value)
                for key, value in context.usage.items()
                if isinstance(value, (int, float))
            },
        )
