"""Evaluation-driven, review-gated Skill evolution for OfficeCLI."""

from __future__ import annotations

import ast
import asyncio
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Literal

import json_repair
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nanobot.agent.hook import CompositeHook
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.skills import SkillsLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.loader import load_config, resolve_config_env_vars, save_config
from nanobot.evaluations.catalog import ROOT, benchmark_cache_root
from nanobot.evaluations.skill_evolution_tools import (
    ApplySkillPatchTool,
    DeleteSkillFileTool,
    ListSkillFilesTool,
    ReadEvolutionEvidenceTool,
    ReadSkillFileTool,
    SkillEvolutionAgentHook,
    ValidateSkillCandidateTool,
    WriteSkillFileTool,
)
from nanobot.providers.factory import make_provider
from nanobot.security.sandbox import SandboxLauncher, SandboxMode, SandboxUnavailableError

BASE_SKILL = "officecli"
DERIVED_SKILL = "officecli-evolved"
DEFAULT_OPTIMIZER_PRESET = "gpt-5-6-sol"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_ALLOWED_FILE = re.compile(r"^(SKILL\.md|(?:scripts|references|assets)/[^/].*)$")
_REDACT_KEYS = re.compile(r"(api.?key|authorization|secret|token|password|cookie)", re.I)
_ANALYSIS_BATCH_CHARS = 100_000
_DETAIL_LIMIT = 1000
_SKILL_EDITOR_MAX_ITERATIONS = 20
_SKILL_EDITOR_BASE_TOOL_CALLS = 40
_SKILL_EDITOR_MAX_TOOL_CALLS = 100
_SKILL_EDITOR_SCRIPT_TOOL_CALLS = 4
_SKILL_EDITOR_WORKFLOW_TOOL_CALLS = 2
_FIX_OWNERS = (
    "skill",
    "runtime",
    "provider",
    "model_capability",
    "benchmark_or_gold",
    "input_asset",
    "evaluator",
    "mixed",
    "inconclusive",
)

_PROBE_OUTPUT_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("chart", "series", "plot", "fill", "图表", "系列", "网格线", "前景", "背景"),
        ("series", "chart_fill", "plot_fill", "text_colors", "gridline_colors"),
    ),
    (
        ("formula", "shared formula", "公式", "共享公式"),
        ("requested_cells", "formulas"),
    ),
    (
        (
            "date format",
            "duplicate",
            "label distribution",
            "analysis population",
            "data quality",
            "日期格式",
            "重复值",
            "标签分布",
            "分析人口",
            "数据质量",
        ),
        (
            "date_formats",
            "parse_failures",
            "duplicates",
            "label_distribution",
            "analysis_population",
        ),
    ),
    (
        ("word count", "paragraph count", "table count", "构造计数", "段落计数", "表格计数"),
        ("counts",),
    ),
)


class RootCauseFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str = ""
    case_ids: list[str] = Field(min_length=1)
    root_cause: str = Field(min_length=1)
    fix_owner: Literal[
        "skill",
        "runtime",
        "provider",
        "model_capability",
        "benchmark_or_gold",
        "input_asset",
        "evaluator",
        "mixed",
        "inconclusive",
    ]
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str] = Field(min_length=1)
    symptoms: list[str] = Field(default_factory=list)
    skill_gap: str = ""
    change_hypothesis: str = ""
    expected_effect: str = ""
    risk: str = ""
    should_modify_skill: bool = Field(
        default=False,
        description=(
            "Whether this finding should be preselected for Skill editing; omission defaults to false."
        ),
    )


class AnalysisCluster(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_cause: str
    fix_owner: str
    finding_ids: list[str]
    case_ids: list[str]


class InterventionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repair_mode: Literal["script_required", "workflow_required", "not_skill_repairable"]
    trigger: str = Field(min_length=1)
    required_action: str = Field(min_length=1)
    entrypoint: str = Field(min_length=1)
    required_outputs: list[str] = Field(default_factory=list)
    final_answer_check: list[str] = Field(min_length=1)
    observable_success: str = Field(min_length=1)


class ReasonCategory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_id: str = ""
    title: str = Field(min_length=1)
    root_cause: str = Field(min_length=1)
    fix_owner: Literal[
        "skill",
        "runtime",
        "provider",
        "model_capability",
        "benchmark_or_gold",
        "input_asset",
        "evaluator",
        "mixed",
        "inconclusive",
    ]
    confidence: float = Field(ge=0, le=1)
    finding_ids: list[str] = Field(min_length=1)
    case_ids: list[str] = Field(default_factory=list)
    risk: str = ""
    should_modify_skill: bool = False
    intervention: InterventionContract


class CategorySynthesisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    categories: list[ReasonCategory] = Field(min_length=1)


class InterventionProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    args: list[str] = Field(min_length=1)
    required_json_fields: list[str] = Field(min_length=1)
    timeout_seconds: int = Field(default=30, ge=1, le=120)


class InterventionImplementation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_id: str = Field(min_length=1)
    repair_mode: Literal["script_required", "workflow_required"]
    changed_paths: list[str] = Field(min_length=1)
    entrypoint: str = Field(min_length=1)
    skill_marker: str = Field(min_length=1)
    probe: InterventionProbe | None = None


class InterventionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    interventions: list[InterventionImplementation] = Field(min_length=1)


class AnalysisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    findings: list[RootCauseFinding]
    clusters: list[AnalysisCluster] = Field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        item
        for item in root.rglob("*")
        if item.is_file() and "__pycache__" not in item.parts and item.suffix != ".pyc"
    ):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _numeric_score(case: dict[str, Any]) -> float | None:
    scores = case.get("scores")
    if not isinstance(scores, dict):
        return None
    for name in ("mybot_score", "mybot-ocb-judge-v1", "official_score"):
        value = scores.get(name)
        if isinstance(value, dict):
            value = value.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _case_key(case: dict[str, Any]) -> str:
    return "\0".join(
        str(case.get(name) or "")
        for name in ("benchmark", "skill", "model_preset", "case_id")
    )


def _redact(value: Any, *, key: str = "") -> Any:
    if _REDACT_KEYS.search(key) and isinstance(value, str):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+", "Bearer [REDACTED]", value)
        value = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
        value = re.sub(
            r"(?i)(api[_-]?key|authorization|password|secret)\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
            value,
        )
        return value[:20_000]
    return value


def _manifest_rows(profile: str, benchmark: str) -> dict[str, dict[str, Any]]:
    path = benchmark_cache_root() / "cases" / f"{profile}-{benchmark}.jsonl"
    rows: dict[str, dict[str, Any]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        case_id = str((row.get("metadata") or {}).get("case_id") or "")
        if case_id:
            rows[case_id] = row
    return rows


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _skill_editor_tool_call_budget(categories: list[dict[str, Any]]) -> int:
    script_count = sum(
        1
        for category in categories
        if str((category.get("intervention") or {}).get("repair_mode") or "")
        == "script_required"
    )
    workflow_count = sum(
        1
        for category in categories
        if str((category.get("intervention") or {}).get("repair_mode") or "")
        == "workflow_required"
    )
    requested = (
        _SKILL_EDITOR_BASE_TOOL_CALLS
        + script_count * _SKILL_EDITOR_SCRIPT_TOOL_CALLS
        + workflow_count * _SKILL_EDITOR_WORKFLOW_TOOL_CALLS
    )
    return min(requested, _SKILL_EDITOR_MAX_TOOL_CALLS)


class SkillEvolutionStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or Path.home() / ".cache" / "nanobot" / "skill-evolution").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def task_path(self, task_id: str) -> Path:
        if not _SAFE_ID.fullmatch(task_id):
            raise ValueError("invalid Skill evolution task id")
        return self.root / "tasks" / f"{task_id}.json"

    def revision_root(self, task_id: str, revision_id: str) -> Path:
        if not _SAFE_ID.fullmatch(task_id) or not _SAFE_ID.fullmatch(revision_id):
            raise ValueError("invalid Skill evolution revision id")
        return self.root / "revisions" / task_id / revision_id

    def task_root(self, task_id: str) -> Path:
        if not _SAFE_ID.fullmatch(task_id):
            raise ValueError("invalid Skill evolution task id")
        return self.root / "task-data" / task_id

    def activity_path(self, task_id: str) -> Path:
        return self.task_root(task_id) / "activity.jsonl"

    def read(self, task_id: str) -> dict[str, Any] | None:
        try:
            return json.loads(self.task_path(task_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def write(
        self,
        task: dict[str, Any],
        *,
        clear_cancel_requested: bool = False,
    ) -> dict[str, Any]:
        task = dict(task)
        task["updated_at"] = _now()
        with self._lock:
            try:
                current = json.loads(self.task_path(str(task["task_id"])).read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                current = {}
            task["activity_cursor"] = max(
                int(current.get("activity_cursor") or 0),
                int(task.get("activity_cursor") or 0),
            )
            if clear_cancel_requested:
                task["cancel_requested"] = False
            elif current.get("cancel_requested") and task.get("status") in {
                "collecting_evidence",
                "analyzing",
                "editing",
                "testing",
            }:
                task["cancel_requested"] = True
            _json_write(self.task_path(str(task["task_id"])), task)
        return task

    def list(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in (self.root / "tasks").glob("*.json"):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(row, dict):
                rows.append(row)
        return sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=True)

    def append_activity(self, task_id: str, event: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            task = self.read(task_id)
            if task is None:
                raise KeyError(task_id)
            seq = int(task.get("activity_cursor") or 0) + 1
            row = {"seq": seq, "timestamp": _now(), **event}
            path = self.activity_path(task_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            task["activity_cursor"] = seq
            self.write(task)
            return row

    def activities(self, task_id: str, after: int = 0) -> list[dict[str, Any]]:
        if self.read(task_id) is None:
            raise KeyError(task_id)
        rows: list[dict[str, Any]] = []
        try:
            lines = self.activity_path(task_id).read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return rows
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and int(row.get("seq") or 0) > after:
                rows.append(row)
        return rows


class SkillEvolutionService:
    """Create, test, and install derived Skills without mutating the base Skill."""

    def __init__(self, evaluations: Any, results: Any, store: SkillEvolutionStore | None = None) -> None:
        self.evaluations = evaluations
        self.results = results
        self.store = store or SkillEvolutionStore()
        self._threads: dict[str, threading.Thread] = {}

    def list(self) -> list[dict[str, Any]]:
        return self.store.list()

    def get(self, task_id: str) -> dict[str, Any] | None:
        return self.store.read(task_id)

    def bad_cases(self, run_id: str, threshold: float = 0.6) -> dict[str, Any]:
        run, cases = self._run_and_cases(run_id)
        rows = []
        for case in cases:
            score = _numeric_score(case)
            if score is None or score >= threshold:
                continue
            rows.append({**case, "score": score, "case_key": _case_key(case)})
        rows.sort(key=lambda row: (float(row["score"]), str(row.get("case_id") or "")))
        return {
            "run": {key: run.get(key) for key in ("job_id", "dataset_run_id", "profile", "status")},
            "threshold": threshold,
            "cases": rows,
            "partial_regression_check": True,
        }

    def generate(
        self,
        run_id: str,
        case_ids: list[str],
        source_model_preset: str,
        optimizer_preset: str,
        threshold: float = 0.6,
    ) -> dict[str, Any]:
        """Compatibility alias for callers migrating to the analysis-first API."""
        return self.start_analysis(
            run_id,
            case_ids,
            source_model_preset,
            optimizer_preset,
            threshold,
        )

    def start_analysis(
        self,
        run_id: str,
        case_ids: list[str],
        source_model_preset: str,
        optimizer_preset: str,
        threshold: float = 0.6,
    ) -> dict[str, Any]:
        selected_ids = {str(case_id) for case_id in case_ids if str(case_id)}
        if not selected_ids:
            raise ValueError("select at least one bad Case")
        if not source_model_preset:
            raise ValueError("source_model_preset is required")
        self._optimizer_runtime(optimizer_preset)
        run, cases = self._run_and_cases(run_id)
        selected = [
            case
            for case in cases
            if str(case.get("model_preset") or "") == source_model_preset
            and str(case.get("case_id") or "") in selected_ids
        ]
        if len(selected) != len(selected_ids):
            raise ValueError("one or more selected Cases are not in the evaluation run")
        for case in selected:
            score = _numeric_score(case)
            if score is None or score >= threshold:
                raise ValueError("selected Cases must have a score below the threshold")
        model_presets = {str(case.get("model_preset") or "") for case in selected}
        if len(model_presets) != 1 or not next(iter(model_presets)):
            raise ValueError("selected Cases must belong to exactly one model")
        if next(iter(model_presets)) != source_model_preset:
            raise ValueError("selected Cases do not match source_model_preset")

        task_id = uuid.uuid4().hex[:20]
        task = {
            "schema_version": 2,
            "task_id": task_id,
            "title": "OfficeCLI evaluation-driven evolution",
            "source_run_id": run_id,
            "source_profile": str(run.get("profile") or ""),
            "source_model_preset": source_model_preset,
            "base_skill": BASE_SKILL,
            "derived_skill": DERIVED_SKILL,
            "optimizer_model": optimizer_preset,
            "threshold": threshold,
            "status": "collecting_evidence",
            "phase": "collecting_evidence",
            "selected_cases": [self._case_summary(case) for case in selected],
            "analyses": [],
            "active_analysis_id": None,
            "revisions": [],
            "active_revision_id": None,
            "activity_cursor": 0,
            "cancel_requested": False,
            "error": None,
            "created_at": _now(),
        }
        self.store.write(task, clear_cancel_requested=True)
        self._emit(
            task_id,
            phase="collecting_evidence",
            kind="stage",
            status="started",
            label="Collecting and freezing evaluation evidence",
        )
        self._start_thread(task_id, self._run_analysis, task_id, selected, None)
        return self._require_task(task_id)

    def reanalyze(self, task_id: str) -> dict[str, Any]:
        task = self._require_task(task_id)
        self._ensure_idle(task_id)
        category_source = self._category_retry_source(task)
        if category_source is not None:
            task["status"] = "analyzing"
            task["phase"] = "analyzing"
            task["cancel_requested"] = False
            task["error"] = None
            self.store.write(task, clear_cancel_requested=True)
            self._emit(
                task_id,
                phase="analyzing",
                kind="stage",
                status="started",
                label="Resuming reason categorization from validated findings",
                detail=f"{len(category_source.get('findings') or [])} existing findings; no Evidence refresh",
            )
            self._start_thread(
                task_id,
                self._run_category_retry,
                task_id,
                category_source,
            )
            return self._require_task(task_id)
        _run, cases = self._run_and_cases(str(task["source_run_id"]))
        selected_keys = {str(row.get("case_key") or "") for row in task["selected_cases"]}
        selected = [case for case in cases if _case_key(case) in selected_keys]
        if len(selected) != len(selected_keys):
            raise ValueError("selected baseline Cases are no longer available")
        task["status"] = "collecting_evidence"
        task["phase"] = "collecting_evidence"
        task["cancel_requested"] = False
        task["error"] = None
        self.store.write(task, clear_cancel_requested=True)
        self._emit(
            task_id,
            phase="collecting_evidence",
            kind="stage",
            status="started",
            label="Refreshing evidence for a new analysis",
        )
        parent_analysis_id = str(task.get("active_analysis_id") or "") or None
        self._start_thread(task_id, self._run_analysis, task_id, selected, parent_analysis_id)
        return self._require_task(task_id)

    def start_evolution(
        self,
        task_id: str,
        finding_ids: list[str] | None = None,
        *,
        category_ids: list[str] | None = None,
        analysis_id: str | None = None,
        analysis_digest: str | None = None,
        parent_revision_id: str | None = None,
    ) -> dict[str, Any]:
        task = self._require_task(task_id)
        self._ensure_idle(task_id)
        analysis = self._require_analysis(task, analysis_id or str(task.get("active_analysis_id") or ""))
        if analysis_digest and analysis_digest != analysis.get("digest"):
            raise ValueError("analysis digest changed; refresh before editing")
        selected_categories, selected_ids = self._resolve_evolution_selection(
            analysis,
            finding_ids=finding_ids,
            category_ids=category_ids,
        )
        findings = [row for row in analysis.get("findings") or [] if row.get("finding_id") in selected_ids]
        if not findings or len(findings) != len(selected_ids):
            raise ValueError("select at least one valid analysis finding")
        for finding in findings:
            owner = str(finding.get("fix_owner") or "")
            if owner not in {"skill", "mixed"}:
                raise ValueError(f"finding {finding['finding_id']} is owned by {owner}, not the Skill")
        if not analysis.get("categories"):
            raise ValueError(
                "reason categorization is incomplete; resume categorization before editing the Skill"
            )
        source_candidate = None
        if parent_revision_id:
            parent = self._require_revision(task, parent_revision_id)
            parent_status = str(parent.get("status") or "")
            resumable_incomplete = (
                parent_status in {"failed", "cancelled"}
                and bool(parent.get("candidate_retained_for_audit"))
                and self._candidate_path(task_id, parent_revision_id).is_dir()
            )
            if (
                parent_status not in {"ready_for_review", "tested", "test_failed"}
                and not resumable_incomplete
            ):
                raise ValueError("parent revision is not reviewable")
            source_candidate = self._candidate_path(task_id, parent_revision_id)
        revision_id = f"r{len(task.get('revisions') or []) + 1}"
        task["status"] = "editing"
        task["phase"] = "editing"
        task["active_revision_id"] = revision_id
        task["cancel_requested"] = False
        task["error"] = None
        self.store.write(task, clear_cancel_requested=True)
        self._emit(
            task_id,
            phase="editing",
            kind="stage",
            status="started",
            label=(
                f"Editing candidate Skill from {len(selected_categories)} reason categories"
                if selected_categories
                else f"Editing candidate Skill from {len(findings)} selected findings"
            ),
        )
        self._start_thread(
            task_id,
            self._run_editing,
            task_id,
            revision_id,
            analysis,
            findings,
            selected_categories,
            parent_revision_id,
            source_candidate,
        )
        return self._require_task(task_id)

    def revise(
        self,
        task_id: str,
        finding_ids: list[str] | None = None,
        category_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        task = self._require_task(task_id)
        parent_id = str(task.get("active_revision_id") or "")
        parent = self._require_revision(task, parent_id)
        selected = finding_ids or list(parent.get("finding_ids") or [])
        selected_categories = category_ids or list(parent.get("category_ids") or [])
        return self.start_evolution(
            task_id,
            selected,
            category_ids=selected_categories or None,
            analysis_id=str(parent.get("analysis_id") or ""),
            analysis_digest=str(parent.get("analysis_digest") or ""),
            parent_revision_id=parent_id,
        )

    @staticmethod
    def _resolve_evolution_selection(
        analysis: dict[str, Any],
        *,
        finding_ids: list[str] | None,
        category_ids: list[str] | None,
    ) -> tuple[list[dict[str, Any]], set[str]]:
        requested_categories = {str(value) for value in category_ids or [] if str(value)}
        categories = [
            row
            for row in analysis.get("categories") or []
            if str(row.get("category_id") or "") in requested_categories
        ]
        if requested_categories:
            if len(categories) != len(requested_categories):
                raise ValueError("select at least one valid reason category")
            for category in categories:
                owner = str(category.get("fix_owner") or "")
                mode = str((category.get("intervention") or {}).get("repair_mode") or "")
                if owner not in {"skill", "mixed"} or mode == "not_skill_repairable":
                    raise ValueError(
                        f"category {category.get('category_id')} is not repairable by the Skill"
                    )
            selected_findings = {
                str(finding_id)
                for category in categories
                for finding_id in category.get("finding_ids") or []
            }
            return categories, selected_findings
        selected_findings = {str(value) for value in finding_ids or [] if str(value)}
        return [], selected_findings

    def cancel(self, task_id: str) -> dict[str, Any]:
        task = self._require_task(task_id)
        if task.get("status") not in {"collecting_evidence", "analyzing", "editing", "testing"}:
            raise ValueError("Skill evolution task is not running")
        task["cancel_requested"] = True
        self.store.write(task)
        self._emit(
            task_id,
            phase=str(task.get("phase") or "analyzing"),
            kind="stage",
            status="cancelled",
            label="Cancellation requested",
        )
        return self._require_task(task_id)

    def activities(self, task_id: str, after: int = 0) -> dict[str, Any]:
        rows = self.store.activities(task_id, max(0, after))
        return {"activities": rows, "cursor": rows[-1]["seq"] if rows else max(0, after)}

    def _start_thread(self, task_id: str, target: Any, *args: Any) -> None:
        self._ensure_idle(task_id)
        thread = threading.Thread(
            target=target,
            args=args,
            daemon=True,
            name=f"skill-evolution-{task_id}",
        )
        self._threads[task_id] = thread
        thread.start()

    def _ensure_idle(self, task_id: str) -> None:
        thread = self._threads.get(task_id)
        if thread is not None and thread.is_alive():
            raise ValueError("this Skill evolution task is already running")

    def _is_cancelled(self, task_id: str) -> bool:
        task = self.store.read(task_id)
        return bool(task and task.get("cancel_requested"))

    def _check_cancelled(self, task_id: str) -> None:
        if self._is_cancelled(task_id):
            raise asyncio.CancelledError()

    def _emit(self, task_id: str, **event: Any) -> dict[str, Any]:
        allowed = {
            "phase", "kind", "status", "label", "detail", "case_id", "tool_name",
            "file_path", "usage", "trace_url",
        }
        row = {key: value for key, value in event.items() if key in allowed and value is not None}
        row["phase"] = str(row.get("phase") or "analyzing")
        row["kind"] = str(row.get("kind") or "stage")
        row["status"] = str(row.get("status") or "running")
        row["label"] = str(_redact(row.get("label") or "Activity"))[:200]
        if "detail" in row:
            row["detail"] = str(_redact(row["detail"]))[:_DETAIL_LIMIT]
        for key in ("case_id", "tool_name", "file_path", "trace_url"):
            if key in row:
                row[key] = str(_redact(row[key]))[:500]
        if "usage" in row:
            row["usage"] = {
                str(key)[:80]: int(value)
                for key, value in dict(row["usage"]).items()
                if isinstance(value, (int, float))
            }
        for source, target in (
            ("case_id", "caseId"),
            ("tool_name", "toolName"),
            ("file_path", "filePath"),
            ("trace_url", "traceUrl"),
        ):
            if source in row:
                row[target] = row.pop(source)
        return self.store.append_activity(task_id, row)

    def _finish_cancelled(self, task_id: str) -> None:
        task = self._require_task(task_id)
        phase = str(task.get("phase") or "analyzing")
        task["status"] = "cancelled"
        task["error"] = "Cancelled by user"
        self.store.write(task)
        self._emit(
            task_id,
            phase=phase,
            kind="stage",
            status="cancelled",
            label="Skill evolution cancelled",
        )

    def _finish_failed(self, task_id: str, phase: str, exc: BaseException) -> None:
        task = self._require_task(task_id)
        task["status"] = "failed"
        task["phase"] = phase
        task["error"] = str(_redact(str(exc)))[:_DETAIL_LIMIT]
        self.store.write(task)
        self._emit(
            task_id,
            phase=phase,
            kind="error",
            status="failed",
            label=f"{phase.replace('_', ' ').title()} failed",
            detail=str(exc),
        )

    def start_test(self, task_id: str, revision_id: str) -> dict[str, Any]:
        task = self._require_task(task_id)
        revision = self._require_revision(task, revision_id)
        recover_cancelled_test = (
            task.get("status") == "cancelled" and revision.get("status") == "testing"
        )
        if (
            revision.get("status") not in {"ready_for_review", "tested", "test_failed"}
            and not recover_cancelled_test
        ):
            raise ValueError("revision is not ready to test")
        self._ensure_idle(task_id)
        if recover_cancelled_test:
            revision["status"] = "test_failed"
            revision["test_error"] = "Previous test was cancelled before revision state was saved"
        candidate = self._candidate_path(task_id, revision_id)
        checks = self._validate_candidate(
            ROOT / "nanobot" / "skills" / BASE_SKILL,
            candidate,
            task_id=task_id,
        )
        revision["security_smoke"] = checks
        if not checks["valid"]:
            raise ValueError("candidate failed pre-test security smoke: " + "; ".join(checks["errors"]))
        analysis_id = str(revision.get("analysis_id") or "")
        if not analysis_id:
            intervention_checks = {
                "valid": False,
                "errors": [
                    "revision has no reason-category intervention contract; regenerate the candidate"
                ],
                "probe_results": [],
            }
            revision["intervention_validation"] = intervention_checks
            self.store.write(task)
            raise ValueError(
                "candidate failed intervention probes: "
                + "; ".join(intervention_checks["errors"])
            )
        analysis = self._require_analysis(task, analysis_id)
        categories = self._revision_categories(analysis, revision)
        evidence = self._load_task_evidence(task_id, analysis)
        intervention_checks = self._validate_interventions(
            self._ensure_intervention_baseline(task_id, revision_id),
            candidate,
            categories,
            evidence,
        )
        revision["intervention_validation"] = intervention_checks
        self.store.write(task)
        if not intervention_checks["valid"]:
            raise ValueError(
                "candidate failed intervention probes: "
                + "; ".join(intervention_checks["errors"])
            )
        revision["status"] = "testing"
        revision.pop("test_error", None)
        revision["test_results"] = []
        revision["test_scope"] = self._test_scope(task, revision)
        task["status"] = "testing"
        task["phase"] = "testing"
        task["cancel_requested"] = False
        self.store.write(task, clear_cancel_requested=True)
        self._emit(
            task_id,
            phase="testing",
            kind="stage",
            status="started",
            label="Regression testing started",
        )
        self._start_thread(task_id, self._run_tests, task_id, revision_id)
        return self._require_task(task_id)

    def apply(self, task_id: str, revision_id: str) -> dict[str, Any]:
        task = self._require_task(task_id)
        revision = self._require_revision(task, revision_id)
        recommendation = revision.get("recommendation") or {}
        if revision.get("status") != "tested" or not recommendation.get("recommended"):
            raise ValueError("only a tested and recommended revision can be applied")
        candidate = self._candidate_path(task_id, revision_id)
        if _tree_digest(candidate) != revision.get("candidate_digest"):
            raise ValueError("candidate digest changed after review")

        config = load_config()
        destination = config.workspace_path / "skills" / DERIVED_SKILL
        destination.parent.mkdir(parents=True, exist_ok=True)
        staged = destination.parent / f".{DERIVED_SKILL}-{uuid.uuid4().hex[:8]}.tmp"
        shutil.copytree(candidate, staged)
        backup = None
        if destination.exists():
            backup = self.store.root / "backups" / f"{DERIVED_SKILL}-{uuid.uuid4().hex[:8]}"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(destination, backup)
        os.replace(staged, destination)
        disabled = set(config.agents.defaults.disabled_skills)
        disabled.add(BASE_SKILL)
        disabled.discard(DERIVED_SKILL)
        config.agents.defaults.disabled_skills = sorted(disabled)
        save_config(config)
        revision["applied_at"] = _now()
        revision["installed_path"] = str(destination)
        revision["backup_path"] = str(backup) if backup else None
        task["status"] = "applied"
        task["applied_revision_id"] = revision_id
        return self.store.write(task)

    def switch_back(self, task_id: str) -> dict[str, Any]:
        task = self._require_task(task_id)
        config = load_config()
        disabled = set(config.agents.defaults.disabled_skills)
        disabled.discard(BASE_SKILL)
        disabled.add(DERIVED_SKILL)
        config.agents.defaults.disabled_skills = sorted(disabled)
        save_config(config)
        task["status"] = "switched_back"
        task["switched_back_at"] = _now()
        return self.store.write(task)

    def _run_and_cases(self, run_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        local = self.evaluations.get(run_id)
        linked_ids = {str(value) for value in (local or {}).get("dataset_run_ids") or []}
        remote_payload = (
            self.results.list_runs_by_ids(linked_ids)
            if linked_ids
            else self.results.list_runs(limit=50)
        )
        if remote_payload.get("refreshing") and not remote_payload.get("runs"):
            raise ValueError("Langfuse evaluation results are still loading; retry shortly")
        remote_runs = list(remote_payload.get("runs") or [])
        if local is None:
            remote = next(
                (row for row in remote_runs if str(row.get("dataset_run_id") or "") == run_id),
                None,
            )
            if remote is None:
                raise KeyError(run_id)
            return remote, list(remote.get("cases") or [])

        token = str(local.get("resume_token") or local.get("job_id") or "")
        suffix = f"-job-{token}" if token else ""
        remote_cases: dict[str, dict[str, Any]] = {}
        for remote in remote_runs:
            if str(remote.get("dataset_run_id") or "") not in linked_ids and not (
                suffix and str(remote.get("name") or "").endswith(suffix)
            ):
                continue
            for case in remote.get("cases") or []:
                remote_cases[_case_key(case)] = case
        merged = []
        for case in self.evaluations.cases(run_id):
            linked = remote_cases.get(_case_key(case))
            merged.append({**case, **linked} if linked else case)
        return local, merged

    def _optimizer_runtime(self, preset_name: str) -> tuple[Any, Any]:
        if not preset_name:
            raise ValueError("optimizer_preset is required")
        config = resolve_config_env_vars(load_config())
        if preset_name == "default" or preset_name not in config.model_presets:
            raise ValueError(f"optimizer preset {preset_name!r} is not a named model preset")
        try:
            preset = config.resolve_preset(preset_name)
            provider = make_provider(config, preset_name=preset_name)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"optimizer preset {preset_name!r} is unavailable: {exc}") from exc
        return preset, provider

    def _run_analysis(
        self,
        task_id: str,
        selected: list[dict[str, Any]],
        parent_analysis_id: str | None,
    ) -> None:
        try:
            task = self._require_task(task_id)
            _run, all_cases = self._run_and_cases(str(task["source_run_id"]))
            comparisons = self._resource_comparisons(selected, all_cases, float(task["threshold"]))
            evidence: list[dict[str, Any]] = []
            for index, case in enumerate(selected, start=1):
                self._check_cancelled(task_id)
                case_id = str(case.get("case_id") or "")
                self._emit(
                    task_id,
                    phase="collecting_evidence",
                    kind="case",
                    status="running",
                    label=f"Collecting Case {index}/{len(selected)}",
                    case_id=case_id,
                    trace_url=str(case.get("trace_url") or "") or None,
                )
                row = self._build_evidence(task, case)
                row["evidence_id"] = f"ev-{hashlib.sha256(_case_key(case).encode()).hexdigest()[:12]}"
                row["observations"] = self._deterministic_observations(row)
                row["resource_comparison"] = comparisons.get(_case_key(case), {})
                evidence.append(row)
                self._emit(
                    task_id,
                    phase="collecting_evidence",
                    kind="case",
                    status="completed",
                    label="Evidence frozen",
                    case_id=case_id,
                )
            evidence_payload = {row["evidence_id"]: row for row in evidence}
            evidence_digest = self._payload_digest(evidence_payload)
            task_root = self.store.task_root(task_id)
            _json_write(task_root / f"evidence-{evidence_digest[:16]}.json", evidence_payload)
            task = self._require_task(task_id)
            task["status"] = "analyzing"
            task["phase"] = "analyzing"
            task["evidence_digest"] = evidence_digest
            self.store.write(task)
            self._emit(
                task_id,
                phase="analyzing",
                kind="stage",
                status="started",
                label="Analyzing frozen evidence",
                detail=f"{len(evidence)} Cases; digest {evidence_digest[:12]}",
            )
            analysis = asyncio.run(self._analyze_evidence(task_id, task, evidence))
            analysis_id = f"a{len(task.get('analyses') or []) + 1}"
            analysis.update({
                "analysis_id": analysis_id,
                "parent_analysis_id": parent_analysis_id,
                "evidence_digest": evidence_digest,
                "created_at": _now(),
                "case_observations": [{
                    "case_id": row["case_id"],
                    "score": row.get("score"),
                    "evidence_id": row["evidence_id"],
                    "resource_comparison": row.get("resource_comparison") or {},
                    "trace_url": next(
                        (
                            case.get("trace_url")
                            for case in task.get("selected_cases") or []
                            if str(case.get("case_id") or "") == str(row["case_id"])
                        ),
                        None,
                    ),
                } for row in evidence],
            })
            analysis["digest"] = self._payload_digest(analysis)
            _json_write(task_root / f"analysis-{analysis_id}.json", analysis)
            task = self._require_task(task_id)
            task.setdefault("analyses", []).append(analysis)
            task["active_analysis_id"] = analysis_id
            task.pop("analysis_checkpoint", None)
            task["status"] = "analysis_ready"
            task["phase"] = "analyzing"
            task["cancel_requested"] = False
            task["error"] = None
            self.store.write(task)
            self._emit(
                task_id,
                phase="analyzing",
                kind="stage",
                status="completed",
                label=f"Analysis ready with {len(analysis['findings'])} findings",
            )
        except asyncio.CancelledError:
            self._finish_cancelled(task_id)
        except Exception as exc:
            self._finish_failed(task_id, "analyzing", exc)

    def _category_retry_source(self, task: dict[str, Any]) -> dict[str, Any] | None:
        checkpoint = task.get("analysis_checkpoint") or {}
        evidence_digest = str(checkpoint.get("evidence_digest") or "")
        if checkpoint.get("findings_complete") and evidence_digest:
            path = self.store.task_root(str(task["task_id"])) / (
                f"analysis-checkpoint-{evidence_digest[:16]}.json"
            )
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                payload = None
            if (
                isinstance(payload, dict)
                and payload.get("findings")
                and str(payload.get("evidence_digest") or "") == evidence_digest
            ):
                return payload
        active_id = str(task.get("active_analysis_id") or "")
        analysis = next(
            (
                row
                for row in task.get("analyses") or []
                if str(row.get("analysis_id") or "") == active_id
            ),
            None,
        )
        if analysis and analysis.get("findings") and not analysis.get("categories"):
            return dict(analysis)
        return None

    def _run_category_retry(self, task_id: str, source: dict[str, Any]) -> None:
        try:
            task = self._require_task(task_id)
            preset, provider = self._optimizer_runtime(str(task["optimizer_model"]))
            findings = [dict(row) for row in source.get("findings") or []]
            categories, category_usage = asyncio.run(
                self._synthesize_categories(task_id, preset, provider, findings)
            )
            clusters = [
                {
                    "root_cause": row["root_cause"],
                    "fix_owner": row["fix_owner"],
                    "finding_ids": row["finding_ids"],
                    "case_ids": row["case_ids"],
                }
                for row in categories
            ]
            usage: Counter[str] = Counter()
            for key, value in (source.get("usage") or {}).items():
                if isinstance(value, (int, float)):
                    usage[str(key)] += int(value)
            usage.update(category_usage)
            task = self._require_task(task_id)
            analysis_id = f"a{len(task.get('analyses') or []) + 1}"
            parent_analysis_id = str(source.get("analysis_id") or task.get("active_analysis_id") or "") or None
            summary = str(source.get("summary") or "").strip()
            if not summary:
                summary = " ".join(
                    str(value).strip()
                    for value in source.get("summaries") or []
                    if str(value).strip()
                )
            analysis = {
                "analysis_id": analysis_id,
                "parent_analysis_id": parent_analysis_id,
                "evidence_digest": str(source.get("evidence_digest") or task.get("evidence_digest") or ""),
                "digest": "",
                "summary": summary,
                "findings": findings,
                "categories": categories,
                "clusters": clusters,
                "usage": dict(usage),
                "batch_count": int(source.get("batch_count") or 0),
                "created_at": _now(),
                "case_observations": list(source.get("case_observations") or []),
                "resumed_from_findings": True,
            }
            analysis["digest"] = self._payload_digest(analysis)
            _json_write(
                self.store.task_root(task_id) / f"analysis-{analysis_id}.json",
                analysis,
            )
            task.setdefault("analyses", []).append(analysis)
            task["active_analysis_id"] = analysis_id
            task.pop("analysis_checkpoint", None)
            task["status"] = "analysis_ready"
            task["phase"] = "analyzing"
            task["cancel_requested"] = False
            task["error"] = None
            self.store.write(task)
            self._emit(
                task_id,
                phase="analyzing",
                kind="stage",
                status="completed",
                label=f"Reason categorization ready with {len(categories)} categories",
                detail="Reused existing findings; Evidence and batch analysis were not repeated",
            )
        except asyncio.CancelledError:
            self._finish_cancelled(task_id)
        except Exception as exc:
            self._finish_failed(task_id, "analyzing", exc)

    async def _analyze_evidence(
        self,
        task_id: str,
        task: dict[str, Any],
        evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        preset, provider = self._optimizer_runtime(str(task["optimizer_model"]))
        batches = self._batch_evidence(evidence)
        findings: list[dict[str, Any]] = []
        summaries: list[str] = []
        usage: Counter[str] = Counter()
        for index, batch in enumerate(batches, start=1):
            self._check_cancelled(task_id)
            self._emit(
                task_id,
                phase="analyzing",
                kind="model",
                status="started",
                label=f"Analyzing batch {index}/{len(batches)}",
                detail=f"{len(batch)} Evidence items",
            )
            prompt = {
                "task": "Diagnose why these evaluation Cases scored poorly. Do not edit a Skill.",
                "rules": [
                    "Cite only supplied evidence_id values and case_id values.",
                    "Token or latency is causal only when the supplied same-model benchmark comparison supports it.",
                    "Separate Skill, runtime, provider, model, benchmark/gold, asset, and evaluator ownership.",
                    "Set should_modify_skill true only when the finding should be preselected for Skill editing; omission means false.",
                    "Return strict JSON matching the supplied schema; do not include reasoning outside JSON.",
                ],
                "schema": AnalysisResponse.model_json_schema(),
                "evidence": batch,
            }
            response = await provider.chat_with_retry(
                messages=[
                    {"role": "system", "content": "You are an evaluation root-cause analyst. Output strict JSON only."},
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
                model=preset.model,
                max_tokens=preset.max_tokens,
                temperature=preset.temperature,
                reasoning_effort=preset.reasoning_effort,
            )
            self._check_cancelled(task_id)
            if response.finish_reason == "error":
                raise RuntimeError(response.content or "analysis model failed")
            for key, value in (response.usage or {}).items():
                if isinstance(value, (int, float)):
                    usage[str(key)] += int(value)
            try:
                parsed = json_repair.loads(response.content or "")
                model = AnalysisResponse.model_validate(parsed)
            except (ValueError, ValidationError) as exc:
                self._emit(
                    task_id,
                    phase="analyzing",
                    kind="validation",
                    status="failed",
                    label=f"Batch {index} schema validation failed",
                    detail=str(exc),
                )
                raise ValueError(f"analysis response failed schema validation: {exc}") from exc
            allowed_cases = {str(row["case_id"]) for row in batch}
            allowed_refs = {str(row["evidence_id"]) for row in batch}
            for finding in model.findings:
                if not set(finding.case_ids) <= allowed_cases:
                    raise ValueError("analysis referenced a Case outside frozen Evidence")
                if not set(finding.evidence_refs) <= allowed_refs:
                    raise ValueError("analysis referenced Evidence outside the frozen set")
                findings.append(finding.model_dump())
            summaries.append(model.summary)
            self._emit(
                task_id,
                phase="analyzing",
                kind="validation",
                status="completed",
                label=f"Validated batch {index} analysis schema",
            )
            self._emit(
                task_id,
                phase="analyzing",
                kind="model",
                status="completed",
                label=f"Analyzed batch {index}/{len(batches)}",
                usage={
                    key: int(value)
                    for key, value in response.usage.items()
                    if isinstance(value, (int, float))
                },
            )
        if not findings:
            raise ValueError("analysis returned no findings")
        for index, finding in enumerate(findings, start=1):
            finding["finding_id"] = f"f{index}"
            if finding["fix_owner"] not in {"skill", "mixed"}:
                finding["should_modify_skill"] = False
        evidence_digest = str(task.get("evidence_digest") or "")
        if evidence_digest:
            checkpoint = {
                "evidence_digest": evidence_digest,
                "summary": " ".join(value.strip() for value in summaries if value.strip()),
                "summaries": summaries,
                "findings": findings,
                "usage": dict(usage),
                "batch_count": len(batches),
                "findings_complete": True,
            }
            _json_write(
                self.store.task_root(task_id) /
                f"analysis-checkpoint-{evidence_digest[:16]}.json",
                checkpoint,
            )
            checkpoint_task = self._require_task(task_id)
            checkpoint_task["analysis_checkpoint"] = {
                "evidence_digest": evidence_digest,
                "finding_count": len(findings),
                "batch_count": len(batches),
                "findings_complete": True,
            }
            self.store.write(checkpoint_task)
        categories, category_usage = await self._synthesize_categories(
            task_id,
            preset,
            provider,
            findings,
        )
        usage.update(category_usage)
        clusters = [
            {
                "root_cause": row["root_cause"],
                "fix_owner": row["fix_owner"],
                "finding_ids": row["finding_ids"],
                "case_ids": row["case_ids"],
            }
            for row in categories
        ]
        self._emit(
            task_id,
            phase="analyzing",
            kind="stage",
            status="completed",
            label=f"Synthesized {len(categories)} cross-batch reason categories",
        )
        return {
            "summary": " ".join(value.strip() for value in summaries if value.strip()),
            "findings": findings,
            "categories": categories,
            "clusters": clusters,
            "usage": dict(usage),
            "batch_count": len(batches),
        }

    async def _synthesize_categories(
        self,
        task_id: str,
        preset: Any,
        provider: Any,
        findings: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], Counter[str]]:
        self._check_cancelled(task_id)
        self._emit(
            task_id,
            phase="analyzing",
            kind="model",
            status="started",
            label="Synthesizing cross-batch reason categories",
            detail=f"{len(findings)} validated findings",
        )
        owner_groups: dict[str, list[dict[str, Any]]] = {}
        for finding in findings:
            owner_groups.setdefault(str(finding.get("fix_owner") or "inconclusive"), []).append(finding)
        all_findings_by_id = {str(row["finding_id"]): row for row in findings}
        assigned: list[str] = []
        categories: list[dict[str, Any]] = []
        category_usage: Counter[str] = Counter()
        output_name = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,79}$")
        for group_index, (owner, group_findings) in enumerate(owner_groups.items(), start=1):
            self._check_cancelled(task_id)
            self._emit(
                task_id,
                phase="analyzing",
                kind="model",
                status="started",
                label=f"Categorizing owner group {group_index}/{len(owner_groups)}",
                detail=f"{owner}: {len(group_findings)} findings",
            )
            prompt = {
                "task": (
                    "Group these same-owner findings into transferable Skill-repair categories and "
                    "define one observable intervention contract for each category. Do not edit a Skill."
                ),
                "owner": owner,
                "rules": [
                    "Assign every finding_id exactly once and cite only supplied finding_id values.",
                    "Every category must keep the supplied fix_owner.",
                    "Use script_required for deterministic Office inspection, extraction, enumeration, normalization, or counting failures.",
                    "Use workflow_required only when the repair is a concise source-grounding or answer-completeness workflow.",
                    "Use not_skill_repairable for runtime, provider, model, evaluator, benchmark, or asset failures.",
                    "For script_required, entrypoint must be a generic path under scripts/ and required_outputs must be stable JSON field names.",
                    "Chart probes must expose series, chart_fill, plot_fill, text_colors, and gridline_colors.",
                    "Formula probes must expose requested_cells and formulas; data-quality probes must expose date_formats, parse_failures, duplicates, label_distribution, and analysis_population.",
                    "For workflow_required, entrypoint must be SKILL.md and final_answer_check must be explicit.",
                    "Never include Case IDs, gold answers, or benchmark-specific answers in intervention text.",
                    "Return strict JSON matching the supplied schema.",
                ],
                "schema": CategorySynthesisResponse.model_json_schema(),
                "findings": group_findings,
            }
            response = await provider.chat_with_retry(
                messages=[
                    {
                        "role": "system",
                        "content": "You design causal, transferable Skill interventions. Output strict JSON only.",
                    },
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
                model=preset.model,
                max_tokens=preset.max_tokens,
                temperature=preset.temperature,
                reasoning_effort=preset.reasoning_effort,
            )
            self._check_cancelled(task_id)
            if response.finish_reason == "error":
                raise RuntimeError(response.content or "category synthesis model failed")
            try:
                parsed = json_repair.loads(response.content or "")
                model = CategorySynthesisResponse.model_validate(parsed)
            except (ValueError, ValidationError) as exc:
                self._emit(
                    task_id,
                    phase="analyzing",
                    kind="validation",
                    status="failed",
                    label=f"Owner group {group_index} schema validation failed",
                    detail=str(exc),
                )
                raise ValueError(f"reason category response failed schema validation: {exc}") from exc
            findings_by_id = {str(row["finding_id"]): row for row in group_findings}
            group_assigned: list[str] = []
            for category_model in model.categories:
                category = category_model.model_dump()
                finding_ids = [str(value) for value in category["finding_ids"]]
                unknown = set(finding_ids) - set(findings_by_id)
                if unknown:
                    raise ValueError(
                        "category referenced unknown findings: " + ", ".join(sorted(unknown))
                    )
                owners = {str(findings_by_id[value]["fix_owner"]) for value in finding_ids}
                if owners != {owner} or str(category["fix_owner"]) != owner:
                    raise ValueError("category cannot change or merge finding owners")
                assigned.extend(finding_ids)
                group_assigned.extend(finding_ids)
                case_ids = sorted({
                    str(case_id)
                    for finding_id in finding_ids
                    for case_id in findings_by_id[finding_id].get("case_ids") or []
                })
                intervention = category["intervention"]
                mode = str(intervention["repair_mode"])
                if owner not in {"skill", "mixed"}:
                    mode = "not_skill_repairable"
                    intervention["repair_mode"] = mode
                    category["should_modify_skill"] = False
                if mode == "script_required":
                    entrypoint = str(intervention["entrypoint"])
                    if not entrypoint.startswith("scripts/") or not entrypoint.endswith(".py"):
                        raise ValueError(
                            "script_required category entrypoint must be a Python path under scripts/"
                        )
                    intervention["required_outputs"] = list(dict.fromkeys([
                        *intervention["required_outputs"],
                        *self._mandatory_probe_fields(category),
                    ]))
                    if not intervention["required_outputs"] or not all(
                        output_name.fullmatch(str(value))
                        for value in intervention["required_outputs"]
                    ):
                        raise ValueError(
                            "script_required category must declare stable JSON output fields"
                        )
                elif mode == "workflow_required":
                    intervention["entrypoint"] = "SKILL.md"
                category.update({
                    "category_id": f"c{len(categories) + 1}",
                    "case_ids": case_ids,
                    "intervention": intervention,
                })
                categories.append(category)
            if len(group_assigned) != len(set(group_assigned)):
                raise ValueError("a finding was assigned to more than one reason category")
            if set(group_assigned) != set(findings_by_id):
                missing = sorted(set(findings_by_id) - set(group_assigned))
                raise ValueError("reason categories omitted findings: " + ", ".join(missing))
            for key, value in (response.usage or {}).items():
                if isinstance(value, (int, float)):
                    category_usage[str(key)] += int(value)
            self._emit(
                task_id,
                phase="analyzing",
                kind="model",
                status="completed",
                label=f"Categorized owner group {group_index}/{len(owner_groups)}",
                usage={
                    key: int(value)
                    for key, value in (response.usage or {}).items()
                    if isinstance(value, (int, float))
                },
            )
        if len(assigned) != len(set(assigned)):
            raise ValueError("a finding was assigned to more than one reason category")
        if set(assigned) != set(all_findings_by_id):
            missing = sorted(set(all_findings_by_id) - set(assigned))
            raise ValueError("reason categories omitted findings: " + ", ".join(missing))
        self._emit(
            task_id,
            phase="analyzing",
            kind="validation",
            status="completed",
            label="Validated cross-batch reason categories",
            usage=dict(category_usage),
        )
        return categories, category_usage

    @staticmethod
    def _mandatory_probe_fields(category: dict[str, Any]) -> list[str]:
        intervention = category.get("intervention") or {}
        text = " ".join(
            str(value)
            for value in (
                category.get("title"),
                category.get("root_cause"),
                intervention.get("trigger"),
                intervention.get("required_action"),
                *(intervention.get("required_outputs") or []),
            )
            if value
        ).lower()
        required: list[str] = []
        for keywords, fields in _PROBE_OUTPUT_RULES:
            if any(keyword in text for keyword in keywords):
                required.extend(fields)
        return list(dict.fromkeys(required))

    @staticmethod
    def _batch_evidence(evidence: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_chars = 0
        for row in evidence:
            row = SkillEvolutionService._analysis_evidence_view(row)
            size = len(json.dumps(row, ensure_ascii=False))
            if current and current_chars + size > _ANALYSIS_BATCH_CHARS:
                batches.append(current)
                current = []
                current_chars = 0
            current.append(row)
            current_chars += size
        if current:
            batches.append(current)
        return batches

    @staticmethod
    def _analysis_evidence_view(evidence: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(evidence, ensure_ascii=False)) <= _ANALYSIS_BATCH_CHARS:
            return evidence
        trace = evidence.get("trace") if isinstance(evidence.get("trace"), dict) else {}
        compact = dict(evidence)
        compact["trace"] = {
            "id": trace.get("id"),
            "name": trace.get("name"),
            "status": trace.get("status"),
            "status_message": trace.get("status_message"),
            "score_comments": trace.get("score_comments"),
            "unavailable": trace.get("unavailable"),
            "compacted_for_analysis": True,
        }
        return compact

    @staticmethod
    def _payload_digest(payload: Any) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    @staticmethod
    def _metric_value(value: Any, names: tuple[str, ...]) -> float | None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key).lower() in names and isinstance(nested, (int, float)):
                    return float(nested)
            for nested in value.values():
                found = SkillEvolutionService._metric_value(nested, names)
                if found is not None:
                    return found
        return None

    def _resource_comparisons(
        self,
        selected: list[dict[str, Any]],
        all_cases: list[dict[str, Any]],
        threshold: float,
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for case in selected:
            peers = [
                row for row in all_cases
                if row.get("model_preset") == case.get("model_preset")
                and row.get("benchmark") == case.get("benchmark")
                and (_numeric_score(row) or 0) >= threshold
            ]
            row: dict[str, Any] = {"peer_count": len(peers), "basis": "same model and benchmark high-score Cases"}
            for label, source, names in (
                ("tokens", "usage", ("total_tokens", "tokens", "total")),
                ("latency_ms", "metrics", ("latency_ms", "duration_ms", "latency")),
            ):
                current = self._metric_value(case.get(source), names)
                peer_values = [self._metric_value(peer.get(source), names) for peer in peers]
                numeric_peers = [value for value in peer_values if value is not None]
                if current is not None and numeric_peers:
                    baseline = median(numeric_peers)
                    row[label] = {
                        "case": current,
                        "high_score_median": baseline,
                        "ratio": round(current / baseline, 3) if baseline else None,
                    }
            result[_case_key(case)] = row
        return result

    @staticmethod
    def _deterministic_observations(evidence: dict[str, Any]) -> dict[str, Any]:
        trace = evidence.get("trace") if isinstance(evidence.get("trace"), dict) else {}
        observations = trace.get("observations") if isinstance(trace.get("observations"), list) else []
        tool_names: list[str] = []
        errors: list[str] = []
        validation_seen = False
        for row in observations:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or row.get("type") or "")
            lowered = name.lower()
            if name:
                tool_names.append(name)
            if "validat" in lowered or "inspect" in lowered or "view" in lowered:
                validation_seen = True
            error = row.get("error") or row.get("status_message")
            if error:
                errors.append(str(error)[:300])
        repeated = [name for name, count in Counter(tool_names).items() if count >= 3]
        return {
            "tool_sequence": tool_names[:100],
            "repeated_tools": repeated,
            "tool_errors": errors[:20],
            "validation_observed": validation_seen,
            "stop_reason": trace.get("status") or trace.get("status_message"),
            "artifact_state": trace.get("output"),
        }

    def _freeze_local_reference_assets(
        self,
        task_id: str,
        task: dict[str, Any],
        analysis: dict[str, Any],
        evidence: dict[str, Any],
        allowed_ids: set[str],
    ) -> dict[str, Any]:
        """Attach local benchmark assets without recollecting or reanalyzing Evidence."""
        profile = str(task.get("source_profile") or "")
        if not profile:
            return evidence
        frozen_root = (
            self.store.task_root(task_id)
            / "frozen-assets"
            / str(analysis.get("evidence_digest") or "unknown")[:16]
        )
        sidecar: dict[str, Any] = {
            "schema_version": 1,
            "evidence_digest": analysis.get("evidence_digest"),
            "cases": {},
        }
        manifests: dict[str, dict[str, dict[str, Any]]] = {}
        for evidence_id in sorted(allowed_ids):
            row = evidence.get(evidence_id)
            if not isinstance(row, dict):
                continue
            case_id = str(row.get("case_id") or "")
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            benchmark = str(metadata.get("benchmark") or "ocb")
            if not case_id:
                continue
            manifest = manifests.setdefault(benchmark, _manifest_rows(profile, benchmark))
            source = manifest.get(case_id) or {}
            source_input = source.get("input") if isinstance(source.get("input"), dict) else {}
            reference_paths = list(source_input.get("reference_paths") or [])
            reference_hashes = list(source_input.get("reference_sha256") or [])
            frozen_paths: list[str] = []
            frozen_hashes: list[str] = []
            for index, value in enumerate(reference_paths):
                source_path = Path(str(value)).expanduser().resolve()
                if not source_path.is_file():
                    continue
                expected_hash = str(reference_hashes[index] or "") if index < len(reference_hashes) else ""
                actual_hash = _file_digest(source_path)
                if expected_hash and actual_hash != expected_hash:
                    continue
                safe_case_id = re.sub(r"[^A-Za-z0-9._-]+", "_", case_id) or "case"
                case_root = frozen_root / safe_case_id
                case_root.mkdir(parents=True, exist_ok=True)
                destination = case_root / f"{index + 1}-{source_path.name}"
                if not destination.is_file() or _file_digest(destination) != actual_hash:
                    shutil.copy2(source_path, destination)
                frozen_paths.append(str(destination.resolve()))
                frozen_hashes.append(actual_hash)
            if frozen_paths:
                row["reference_paths"] = frozen_paths
                row["reference_sha256"] = frozen_hashes
                sidecar["cases"][case_id] = {
                    "evidence_id": evidence_id,
                    "reference_paths": frozen_paths,
                    "reference_sha256": frozen_hashes,
                }
        sidecar_path = self.store.task_root(task_id) / (
            f"frozen-assets-{str(analysis.get('evidence_digest') or 'unknown')[:16]}.json"
        )
        _json_write(sidecar_path, sidecar)
        return evidence

    @staticmethod
    def _missing_script_probe_assets(
        categories: list[dict[str, Any]],
        evidence: dict[str, Any],
    ) -> list[str]:
        missing: list[str] = []
        for category in categories:
            intervention = category.get("intervention") or {}
            if intervention.get("repair_mode") != "script_required":
                continue
            case_ids = {str(value) for value in category.get("case_ids") or []}
            available = any(
                isinstance(row, dict)
                and str(row.get("case_id") or "") in case_ids
                and any(Path(str(path)).is_file() for path in row.get("reference_paths") or [])
                for row in evidence.values()
            )
            if not available:
                missing.append(str(category.get("category_id") or "unknown"))
        return missing

    def _run_editing(
        self,
        task_id: str,
        revision_id: str,
        analysis: dict[str, Any],
        findings: list[dict[str, Any]],
        categories: list[dict[str, Any]] | None = None,
        parent_revision_id: str | None = None,
        source_candidate: Path | None = None,
    ) -> None:
        categories = list(categories or [])
        base = ROOT / "nanobot" / "skills" / BASE_SKILL
        source = source_candidate or base
        revision_root = self.store.revision_root(task_id, revision_id)
        candidate = revision_root / "candidate" / DERIVED_SKILL
        baseline = revision_root / "baseline" / DERIVED_SKILL
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, candidate, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            self._normalize_derived_metadata(candidate)
            shutil.copytree(candidate, baseline, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            intervention_baseline = self._ensure_intervention_baseline(task_id, revision_id)
            initial_digest = _tree_digest(candidate)
            evidence_path = self.store.task_root(task_id) / (
                f"evidence-{str(analysis['evidence_digest'])[:16]}.json"
            )
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            allowed_ids = {
                str(value)
                for finding in findings
                for value in finding.get("evidence_refs") or []
            }
            task = self._require_task(task_id)
            evidence = self._freeze_local_reference_assets(
                task_id,
                task,
                analysis,
                evidence,
                allowed_ids,
            )
            missing_asset_categories = self._missing_script_probe_assets(categories, evidence)
            if missing_asset_categories:
                raise ValueError(
                    "local frozen assets are unavailable for script categories: "
                    + ", ".join(missing_asset_categories)
                )
            target_case_ids = sorted({
                str(case_id)
                for finding in findings
                for case_id in finding.get("case_ids") or []
            })
            category_ids = [str(row.get("category_id") or "") for row in categories]
            interventions = [dict(row.get("intervention") or {}) for row in categories]

            def emit(**event: Any) -> None:
                self._emit(task_id, **event)

            def cancelled() -> bool:
                return self._is_cancelled(task_id)

            registry = ToolRegistry()
            common = (candidate, emit)
            registry.register(ListSkillFilesTool(*common, cancelled=cancelled))
            registry.register(ReadSkillFileTool(*common, cancelled=cancelled))
            registry.register(ReadEvolutionEvidenceTool(
                *common,
                cancelled=cancelled,
                evidence=evidence,
                allowed_ids=allowed_ids,
            ))
            registry.register(ApplySkillPatchTool(*common, cancelled=cancelled))
            registry.register(WriteSkillFileTool(*common, cancelled=cancelled))
            registry.register(DeleteSkillFileTool(*common, cancelled=cancelled))
            registry.register(ValidateSkillCandidateTool(
                *common,
                cancelled=cancelled,
                validate=lambda: self._validate_revision_candidate(
                    base,
                    intervention_baseline,
                    candidate,
                    categories,
                    evidence,
                    task_id=task_id,
                ),
            ))
            preset, provider = self._optimizer_runtime(str(task["optimizer_model"]))
            config = resolve_config_env_vars(load_config())
            maintainer = (
                ROOT / "nanobot" / "evaluations" / "prompts" / "skill-maintainer-v1.md"
            ).read_text(encoding="utf-8")
            parent_feedback: dict[str, Any] | None = None
            if parent_revision_id:
                parent = self._require_revision(task, parent_revision_id)
                feedback_rows = self._revision_feedback_rows(parent)
                feedback_evidence = {
                    str(row["feedback_id"]): row
                    for row in feedback_rows
                }
                evidence.update(feedback_evidence)
                allowed_ids.update(feedback_evidence)
                parent_feedback = {
                    "validation": parent.get("validation"),
                    "intervention_validation": parent.get("intervention_validation"),
                    "recommendation": parent.get("recommendation"),
                    "feedback_ids": sorted(feedback_evidence),
                }
            prompt = {
                "task": "Modify the isolated candidate Skill to address the approved findings.",
                "analysis_id": analysis["analysis_id"],
                "analysis_digest": analysis["digest"],
                "approved_findings": findings,
                "approved_categories": categories,
                "approved_evidence_ids": sorted(allowed_ids),
                "previous_revision_feedback": parent_feedback,
                "intervention_manifest": {
                    "path": "references/evolution-interventions.json",
                    "schema": InterventionManifest.model_json_schema(),
                    "rules": [
                        "Include exactly one implementation for every approved category.",
                        "script_required implementations must change a Python script, reference it directly from SKILL.md, and define an asset-backed JSON probe.",
                        "workflow_required implementations must place a concise trigger/action/final-check rule directly in SKILL.md.",
                        "Use {asset} for one frozen input asset or {assets} to expand all frozen assets for one Evidence item.",
                    ],
                },
                "completion": (
                    "Re-read every changed file, write the intervention manifest, and call "
                    "validate_skill_candidate before finishing."
                ),
            }
            hook = CompositeHook([SkillEvolutionAgentHook(emit, cancelled)])
            agent_workspace = revision_root / "agent-workspace"
            agent_workspace.mkdir(parents=True, exist_ok=True)
            result = asyncio.run(AgentRunner(provider).run(AgentRunSpec(
                initial_messages=[
                    {"role": "system", "content": maintainer},
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
                tools=registry,
                model=preset.model,
                max_iterations=min(
                    config.agents.defaults.max_tool_iterations,
                    _SKILL_EDITOR_MAX_ITERATIONS,
                ),
                max_tool_result_chars=config.agents.defaults.max_tool_result_chars,
                temperature=preset.temperature,
                max_tokens=preset.max_tokens,
                reasoning_effort=preset.reasoning_effort,
                context_window_tokens=preset.context_window_tokens,
                hook=hook,
                fail_on_tool_error=False,
                concurrent_tools=False,
                workspace=agent_workspace,
                session_key=f"skill-evolution:{task_id}:{revision_id}",
                actor="skill-maintainer",
                task_id=task_id,
                total_token_budget=None,
                max_tool_calls=_skill_editor_tool_call_budget(categories),
                llm_timeout_s=None,
            )))
            self._check_cancelled(task_id)
            controller_validatable_stop = result.stop_reason in {"completed", "max_iterations"}
            if not controller_validatable_stop or result.error:
                raise ValueError(
                    f"editing Agent stopped with {result.stop_reason}: "
                    f"{result.error or 'incomplete run'}"
                )
            checks = self._validate_candidate(base, candidate, task_id=task_id)
            intervention_checks = self._validate_interventions(
                intervention_baseline,
                candidate,
                categories,
                evidence,
            )
            self._emit(
                task_id,
                phase="validating",
                kind="validation",
                status="completed" if checks["valid"] else "failed",
                label="Controller validation completed",
                detail="; ".join(checks["errors"]),
            )
            if not checks["valid"]:
                raise ValueError("candidate validation failed: " + "; ".join(checks["errors"]))
            digest = _tree_digest(candidate)
            diff = self._candidate_diff(baseline, candidate, limit=None)
            if digest == initial_digest or not diff.strip():
                raise ValueError("editing Agent produced no candidate changes")
            if len(diff.encode()) > 200_000:
                raise ValueError("candidate diff is too large")
            changed_paths = self._changed_paths(baseline, candidate)
            revision = {
                "revision_id": revision_id,
                "parent_revision_id": parent_revision_id,
                "analysis_id": analysis["analysis_id"],
                "analysis_digest": analysis["digest"],
                "finding_ids": [row["finding_id"] for row in findings],
                "category_ids": category_ids,
                "target_case_ids": target_case_ids,
                "interventions": interventions,
                "status": "ready_for_review",
                "summary": (
                    result.final_content or "Candidate Skill updated"
                    if result.stop_reason == "completed"
                    else "Candidate Skill updated and accepted by controller validation after reaching the editing iteration limit."
                ),
                "rationale": analysis.get("summary") or "",
                "changed_paths": changed_paths,
                "candidate_digest": digest,
                "diff": diff,
                "validation": checks,
                "intervention_validation": intervention_checks,
                "agent": {
                    "tools_used": result.tools_used,
                    "usage": result.usage,
                    "stop_reason": result.stop_reason,
                },
                "created_at": _now(),
                "test_results": [],
            }
            task = self._require_task(task_id)
            task.setdefault("revisions", []).append(revision)
            task["active_revision_id"] = revision_id
            task["status"] = "ready_for_review"
            task["phase"] = "validating"
            task["cancel_requested"] = False
            task["error"] = None
            self.store.write(task)
            self._emit(
                task_id,
                phase="validating",
                kind="stage",
                status="completed",
                label=f"Candidate ready with {len(changed_paths)} changed files",
            )
        except asyncio.CancelledError:
            self._record_incomplete_revision(
                task_id,
                revision_id,
                analysis,
                findings,
                categories,
                parent_revision_id,
                candidate,
                "cancelled",
                "Cancelled by user",
            )
            self._finish_cancelled(task_id)
        except Exception as exc:
            self._record_incomplete_revision(
                task_id,
                revision_id,
                analysis,
                findings,
                categories,
                parent_revision_id,
                candidate,
                "failed",
                str(exc),
            )
            self._finish_failed(task_id, "editing", exc)

    def _record_incomplete_revision(
        self,
        task_id: str,
        revision_id: str,
        analysis: dict[str, Any],
        findings: list[dict[str, Any]],
        categories: list[dict[str, Any]],
        parent_revision_id: str | None,
        candidate: Path,
        status: str,
        error: str,
    ) -> None:
        task = self._require_task(task_id)
        if any(row.get("revision_id") == revision_id for row in task.get("revisions") or []):
            return
        revision = {
            "revision_id": revision_id,
            "parent_revision_id": parent_revision_id,
            "analysis_id": analysis.get("analysis_id"),
            "analysis_digest": analysis.get("digest"),
            "finding_ids": [row.get("finding_id") for row in findings],
            "category_ids": [row.get("category_id") for row in categories],
            "target_case_ids": sorted({
                str(case_id)
                for row in categories or findings
                for case_id in row.get("case_ids") or []
            }),
            "interventions": [dict(row.get("intervention") or {}) for row in categories],
            "status": status,
            "summary": "Editing run did not produce a publishable revision",
            "rationale": analysis.get("summary") or "",
            "error": str(_redact(error))[:_DETAIL_LIMIT],
            "changed_paths": [],
            "candidate_digest": _tree_digest(candidate) if candidate.is_dir() else None,
            "candidate_retained_for_audit": candidate.is_dir(),
            "diff": "",
            "validation": {"valid": False, "errors": [str(_redact(error))[:_DETAIL_LIMIT]]},
            "created_at": _now(),
            "test_results": [],
        }
        task.setdefault("revisions", []).append(revision)
        task["active_revision_id"] = revision_id
        self.store.write(task)

    @staticmethod
    def _changed_paths(base: Path, candidate: Path) -> list[str]:
        paths = {
            path.relative_to(root).as_posix()
            for root in (base, candidate)
            for path in root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        }
        changed = []
        for relative in sorted(paths):
            before = base / relative
            after = candidate / relative
            if not before.is_file() or not after.is_file() or before.read_bytes() != after.read_bytes():
                changed.append(relative)
        return changed

    def _build_evidence(self, task: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
        case_id = str(case.get("case_id") or "")
        manifest = _manifest_rows(task["source_profile"], str(case.get("benchmark") or "ocb"))
        source = manifest.get(case_id) or {}
        trace = self._trace_evidence(str(case.get("trace_url") or ""))
        checkpoint = self._checkpoint_output(task["source_run_id"], case)
        prompt = (source.get("input") or {}).get("prompt")
        expected_output = source.get("expected_output")
        if not prompt or not expected_output:
            raise ValueError(f"Case {case_id} lacks prompt or gold evidence")
        return _redact({
            "case_id": case_id,
            "model_preset": case.get("model_preset"),
            "score": _numeric_score(case),
            "scores": case.get("scores"),
            "prompt": prompt,
            "expected_output": expected_output,
            "reference_paths": list((source.get("input") or {}).get("reference_paths") or []),
            "metadata": source.get("metadata"),
            "baseline_output": checkpoint or trace.get("output"),
            "judge_reasoning": trace.get("score_comments"),
            "trace": trace,
            "usage": case.get("usage"),
            "metrics": case.get("metrics"),
        })

    def _trace_evidence(self, trace_url: str) -> dict[str, Any]:
        trace_id = trace_url.rstrip("/").split("/")[-1] if "/traces/" in trace_url else ""
        if not trace_id:
            return {}
        runtime = None
        try:
            from nanobot.runtime.langfuse import LangfuseRuntime

            config = resolve_config_env_vars(load_config()).observability.langfuse
            runtime = LangfuseRuntime(config)
            trace = runtime.client.api.trace.get(trace_id, fields="core,io,scores,observations,metrics")
            payload = trace.model_dump(mode="json", by_alias=True)
            comments = [
                str(score.get("comment"))
                for score in payload.get("scores") or []
                if score.get("comment")
            ]
            return {**payload, "score_comments": comments}
        except Exception as exc:
            return {"unavailable": str(exc)[:300]}
        finally:
            if runtime is not None:
                runtime.shutdown()

    def _checkpoint_output(self, run_id: str, case: dict[str, Any]) -> str | None:
        job = self.evaluations.get(run_id)
        if not job:
            return None
        token = str(job.get("resume_token") or job.get("job_id") or "")
        root = benchmark_cache_root() / "runs" / str(job.get("profile") or "") / "jobs" / token
        for path in root.glob(
            f"case-results/{case.get('benchmark')}/{case.get('skill')}/{case.get('model_preset')}/*.json"
        ):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if str(payload.get("case_id") or "") == str(case.get("case_id") or ""):
                return str(payload.get("content") or "")
        return None

    @staticmethod
    def _normalize_derived_metadata(candidate: Path) -> None:
        manifest_path = candidate / "skill.yaml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest["name"] = DERIVED_SKILL
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        skill_path = candidate / "SKILL.md"
        text = skill_path.read_text(encoding="utf-8")
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end >= 0:
                front = yaml.safe_load(text[4:end]) or {}
                front = {
                    "name": DERIVED_SKILL,
                    "description": str(front.get("description") or "Derived OfficeCLI Skill"),
                }
                text = "---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n" + text[end + 5 :]
        else:
            description = (
                "Create, inspect, validate, and modify Word, Excel, and PowerPoint files "
                "with the pinned OfficeCLI workflow. Use for Office document tasks and OCB cases."
            )
            text = (
                "---\n"
                + yaml.safe_dump(
                    {"name": DERIVED_SKILL, "description": description},
                    sort_keys=False,
                )
                + "---\n\n"
                + text.lstrip()
            )
        skill_path.write_text(text, encoding="utf-8")

    def _validate_candidate(
        self,
        base: Path,
        candidate: Path,
        *,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        errors: list[str] = []
        allowed_roots = {"SKILL.md", "skill.yaml", "scripts", "references", "assets"}
        total_size = 0
        file_count = 0
        text_files: list[tuple[str, str]] = []
        try:
            candidate.resolve().relative_to(self.store.root)
        except ValueError:
            # Unit tests validate temporary candidates outside the Store; runtime candidates
            # are always checked by their explicit revision path before this method is called.
            if task_id is not None:
                errors.append("candidate directory is outside the evolution Store")
        if not (candidate / "SKILL.md").is_file() or not (candidate / "skill.yaml").is_file():
            errors.append("SKILL.md and skill.yaml are required")
        try:
            skill_text = (candidate / "SKILL.md").read_text(encoding="utf-8")
            if not skill_text.startswith("---\n") or "\n---\n" not in skill_text[4:]:
                raise ValueError("YAML frontmatter is required")
            end = skill_text.find("\n---\n", 4)
            frontmatter = yaml.safe_load(skill_text[4:end]) or {}
            if frontmatter.get("name") != DERIVED_SKILL or not frontmatter.get("description"):
                raise ValueError("frontmatter name and description are invalid")
            if set(frontmatter) != {"name", "description"}:
                raise ValueError("frontmatter may contain only name and description")
        except Exception as exc:
            errors.append(f"invalid SKILL.md: {exc}")
        try:
            base_manifest = yaml.safe_load((base / "skill.yaml").read_text(encoding="utf-8"))
            candidate_manifest = yaml.safe_load((candidate / "skill.yaml").read_text(encoding="utf-8"))
            expected = {**base_manifest, "name": DERIVED_SKILL}
            if candidate_manifest != expected:
                errors.append("skill.yaml changed outside the derived Skill name")
        except Exception as exc:
            errors.append(f"invalid skill.yaml: {exc}")
        base_contract = base / "references" / "officecli-runtime.json"
        candidate_contract = candidate / "references" / "officecli-runtime.json"
        if not candidate_contract.is_file() or candidate_contract.read_bytes() != base_contract.read_bytes():
            errors.append("OfficeCLI provider contract must remain frozen")
        for path in candidate.rglob("*"):
            relative = path.relative_to(candidate)
            if relative.parts and relative.parts[0] not in allowed_roots:
                errors.append(f"path is outside the allowed Skill surface: {relative}")
            if path.is_symlink():
                errors.append(f"symbolic links are not allowed: {relative}")
                continue
            if not path.is_file():
                continue
            file_count += 1
            try:
                size = path.stat().st_size
            except OSError as exc:
                errors.append(str(exc))
                continue
            total_size += size
            if size > 500_000:
                errors.append(f"file is too large: {relative}")
            try:
                text_files.append((relative.as_posix(), path.read_text(encoding="utf-8")))
            except UnicodeDecodeError:
                if relative.parts[0] != "assets":
                    errors.append(f"non-text file is allowed only under assets/: {relative}")
            if path.is_file() and path.suffix == ".py":
                try:
                    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                except (OSError, SyntaxError, UnicodeError) as exc:
                    errors.append(str(exc))
        if file_count > 300:
            errors.append("candidate contains too many files")
        if total_size > 5_000_000:
            errors.append("candidate directory is too large")
        credential_pattern = re.compile(
            r"(?i)(?:api[_-]?key|authorization|password|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_./+:-]{12,}"
        )
        for relative, text in text_files:
            if credential_pattern.search(text) or re.search(r"sk-[A-Za-z0-9_-]{12,}", text):
                errors.append(f"possible credential in {relative}")
        if task_id:
            task = self.store.read(task_id) or {}
            case_ids = [
                str(row.get("case_id") or "")
                for row in task.get("selected_cases") or []
                if len(str(row.get("case_id") or "")) >= 6
            ]
            evidence_path = next(
                self.store.task_root(task_id).glob(
                    f"evidence-{str(task.get('evidence_digest') or '')[:16]}*.json"
                ),
                None,
            )
            gold_fragments: list[str] = []
            if evidence_path:
                try:
                    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
                    for row in evidence.values():
                        gold = row.get("expected_output")
                        if isinstance(gold, str) and len(gold.strip()) >= 80:
                            gold_fragments.append(gold.strip()[:160])
                except (OSError, json.JSONDecodeError):
                    errors.append("frozen Evidence is unreadable")
            for relative, text in text_files:
                if any(case_id in text for case_id in case_ids):
                    errors.append(f"selected Case ID leaked into {relative}")
                if any(fragment in text for fragment in gold_fragments):
                    errors.append(f"gold answer text leaked into {relative}")
        python_files = [str(candidate / relative) for relative, _text in text_files if relative.endswith(".py")]
        if python_files:
            completed = subprocess.run(
                [sys.executable, "-m", "ruff", "check", "--output-format", "concise", *python_files],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            if completed.returncode:
                errors.append("ruff failed: " + (completed.stdout or completed.stderr)[-1000:])
        try:
            loader = SkillsLoader(candidate.parent / ".workspace", builtin_skills_dir=candidate.parent)
            status = loader.get_skill_status(DERIVED_SKILL)
            if not status.get("valid"):
                errors.append("SkillsLoader rejected candidate manifest")
            if not status.get("available"):
                reasons = "; ".join(
                    str(row.get("message") or row.get("code") or "unavailable")
                    for row in status.get("reasons") or []
                )
                errors.append("SkillsLoader reports candidate unavailable: " + reasons)
            if not any(row.get("name") == DERIVED_SKILL for row in loader.list_skills(False)):
                errors.append("SkillsLoader cannot discover candidate")
        except Exception as exc:
            errors.append(f"SkillsLoader validation failed: {exc}")
        return {"valid": not errors, "errors": errors}

    def _validate_revision_candidate(
        self,
        base: Path,
        baseline: Path,
        candidate: Path,
        categories: list[dict[str, Any]],
        evidence: dict[str, Any],
        *,
        task_id: str,
    ) -> dict[str, Any]:
        candidate_checks = self._validate_candidate(base, candidate, task_id=task_id)
        intervention_checks = self._validate_interventions(
            baseline,
            candidate,
            categories,
            evidence,
        )
        errors = list(candidate_checks["errors"]) + list(intervention_checks["errors"])
        return {
            "valid": not errors,
            "errors": errors,
            "candidate": candidate_checks,
            "interventions": intervention_checks,
        }

    def _validate_interventions(
        self,
        baseline: Path,
        candidate: Path,
        categories: list[dict[str, Any]],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        errors: list[str] = []
        probe_results: list[dict[str, Any]] = []
        if not categories:
            return {
                "valid": False,
                "errors": [
                    "revision has no reason-category intervention contract; regenerate the candidate"
                ],
                "probe_results": [],
            }
        manifest_path = candidate / "references" / "evolution-interventions.json"
        try:
            manifest = InterventionManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return {
                "valid": False,
                "errors": ["references/evolution-interventions.json is required"],
                "probe_results": [],
            }
        except (OSError, ValidationError, ValueError) as exc:
            return {
                "valid": False,
                "errors": [f"invalid intervention manifest: {exc}"],
                "probe_results": [],
            }
        category_by_id = {str(row.get("category_id") or ""): row for row in categories}
        implementation_by_id = {row.category_id: row for row in manifest.interventions}
        if len(implementation_by_id) != len(manifest.interventions):
            errors.append("intervention manifest contains duplicate category_id values")
        if set(implementation_by_id) != set(category_by_id):
            errors.append("intervention manifest must cover exactly the selected reason categories")
        changed_paths = set(self._changed_paths(baseline, candidate))
        skill_text = (candidate / "SKILL.md").read_text(encoding="utf-8")
        for category_id, category in category_by_id.items():
            implementation = implementation_by_id.get(category_id)
            if implementation is None:
                continue
            contract = category.get("intervention") or {}
            expected_mode = str(contract.get("repair_mode") or "")
            if implementation.repair_mode != expected_mode:
                errors.append(f"{category_id}: repair_mode does not match the approved contract")
            missing_changes = set(implementation.changed_paths) - changed_paths
            if missing_changes:
                errors.append(
                    f"{category_id}: manifest cites unchanged paths: "
                    + ", ".join(sorted(missing_changes))
                )
            if implementation.skill_marker not in skill_text:
                errors.append(f"{category_id}: SKILL.md is missing the declared activation marker")
            if expected_mode == "workflow_required":
                if implementation.entrypoint != "SKILL.md" or implementation.probe is not None:
                    errors.append(
                        f"{category_id}: workflow_required must use SKILL.md without a script probe"
                    )
                if "SKILL.md" not in changed_paths or "SKILL.md" not in implementation.changed_paths:
                    errors.append(
                        f"{category_id}: workflow_required must change and cite SKILL.md"
                    )
                continue
            if expected_mode != "script_required":
                errors.append(f"{category_id}: selected category is not Skill-repairable")
                continue
            entrypoint = implementation.entrypoint
            if entrypoint != str(contract.get("entrypoint") or ""):
                errors.append(f"{category_id}: script entrypoint differs from the approved contract")
            if entrypoint not in skill_text:
                errors.append(f"{category_id}: SKILL.md does not reference the declared entrypoint")
            if not entrypoint.startswith("scripts/") or not entrypoint.endswith(".py"):
                errors.append(f"{category_id}: script entrypoint must be a Python file under scripts/")
                continue
            if entrypoint not in changed_paths or not (candidate / entrypoint).is_file():
                errors.append(f"{category_id}: script_required must add or modify {entrypoint}")
                continue
            probe = implementation.probe
            if probe is None:
                errors.append(f"{category_id}: script_required intervention is missing a probe")
                continue
            required_outputs = {str(value) for value in contract.get("required_outputs") or []}
            if not required_outputs <= set(probe.required_json_fields):
                errors.append(f"{category_id}: probe does not validate every required output field")
                continue
            category_evidence = [
                row
                for row in evidence.values()
                if isinstance(row, dict)
                and str(row.get("case_id") or "") in set(category.get("case_ids") or [])
                and row.get("reference_paths")
            ]
            if not category_evidence:
                errors.append(f"{category_id}: no frozen input assets are available for its probe")
                continue
            category_results, category_errors = self._run_intervention_probes(
                candidate,
                category_id,
                entrypoint,
                probe,
                category_evidence,
                mandatory_fields=self._mandatory_probe_fields(category),
            )
            probe_results.extend(category_results)
            errors.extend(category_errors)
        return {"valid": not errors, "errors": errors, "probe_results": probe_results}

    def _run_intervention_probes(
        self,
        candidate: Path,
        category_id: str,
        entrypoint: str,
        probe: InterventionProbe,
        evidence_rows: list[dict[str, Any]],
        *,
        mandatory_fields: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        launcher = SandboxLauncher()
        for evidence in evidence_rows:
            source_paths = [
                Path(str(value)).expanduser().resolve()
                for value in evidence.get("reference_paths") or []
                if Path(str(value)).expanduser().is_file()
            ]
            if not source_paths:
                errors.append(
                    f"{category_id}: Case {evidence.get('case_id')} has no readable frozen assets"
                )
                continue
            with tempfile.TemporaryDirectory(prefix="nanobot-evolution-probe-") as temp_dir:
                root = Path(temp_dir)
                probe_candidate = root / "candidate"
                shutil.copytree(candidate, probe_candidate)
                assets_dir = root / "assets"
                assets_dir.mkdir()
                assets: list[Path] = []
                for index, source in enumerate(source_paths, start=1):
                    destination = assets_dir / f"{index}-{source.name}"
                    shutil.copy2(source, destination)
                    assets.append(destination)
                invocations: list[list[str]] = []
                if "{asset}" in probe.args:
                    for asset in assets:
                        invocations.append([
                            str(asset) if value == "{asset}" else value
                            for value in probe.args
                        ])
                else:
                    expanded: list[str] = []
                    for value in probe.args:
                        if value == "{assets}":
                            expanded.extend(str(asset) for asset in assets)
                        else:
                            expanded.append(value)
                    if not any(value == "{assets}" for value in probe.args):
                        errors.append(f"{category_id}: probe args must contain {{asset}} or {{assets}}")
                        continue
                    invocations.append(expanded)
                for invocation in invocations:
                    argv = (sys.executable, str(probe_candidate / entrypoint), *invocation)
                    env = {
                        "PATH": os.environ.get("PATH", ""),
                        "PYTHONIOENCODING": "utf-8",
                        "PYTHONDONTWRITEBYTECODE": "1",
                    }
                    try:
                        launch = launcher.prepare_argv(
                            argv=argv,
                            command_text=None,
                            workspace=root,
                            cwd=probe_candidate,
                            env=env,
                            mode=SandboxMode.READ_ONLY,
                        )
                        completed = subprocess.run(
                            launch.argv,
                            cwd=launch.cwd,
                            env=launch.env,
                            capture_output=True,
                            text=True,
                            timeout=probe.timeout_seconds,
                            check=False,
                        )
                    except (OSError, subprocess.TimeoutExpired, SandboxUnavailableError) as exc:
                        errors.append(f"{category_id}: probe execution failed: {exc}")
                        continue
                    result = {
                        "category_id": category_id,
                        "case_id": str(evidence.get("case_id") or ""),
                        "assets": [path.name for path in assets],
                        "returncode": completed.returncode,
                    }
                    if completed.returncode:
                        result["error"] = completed.stderr[-1000:]
                        results.append(result)
                        errors.append(
                            f"{category_id}: probe exited {completed.returncode} for Case "
                            f"{evidence.get('case_id')}"
                        )
                        continue
                    try:
                        payload = json.loads(completed.stdout)
                    except json.JSONDecodeError as exc:
                        result["error"] = f"stdout is not JSON: {exc}"
                        results.append(result)
                        errors.append(f"{category_id}: probe stdout must be one JSON value")
                        continue
                    missing = [
                        field
                        for field in probe.required_json_fields
                        if not self._json_field_present(payload, field)
                    ]
                    semantic_errors = self._probe_semantic_errors(
                        payload,
                        mandatory_fields or [],
                    )
                    result.update({
                        "valid": not missing and not semantic_errors,
                        "missing_fields": missing,
                        "semantic_errors": semantic_errors,
                    })
                    results.append(result)
                    if missing:
                        errors.append(
                            f"{category_id}: probe missing JSON fields: " + ", ".join(missing)
                        )
                    if semantic_errors:
                        errors.extend(
                            f"{category_id}: {message}" for message in semantic_errors
                        )
        return results, errors

    @staticmethod
    def _json_field_present(payload: Any, field: str) -> bool:
        current = payload
        for part in field.split("."):
            if not isinstance(current, dict) or part not in current:
                return False
            current = current[part]
        return current not in (None, "", [], {})

    @staticmethod
    def _probe_semantic_errors(payload: Any, mandatory_fields: list[str]) -> list[str]:
        errors: list[str] = []
        required = set(mandatory_fields)
        if {"requested_cells", "formulas"} <= required and isinstance(payload, dict):
            requested = payload.get("requested_cells")
            formulas = payload.get("formulas")
            if not isinstance(requested, list) or not all(isinstance(value, str) for value in requested):
                errors.append("requested_cells must be a list of cell addresses")
            elif not isinstance(formulas, dict):
                errors.append("formulas must map every requested cell to its effective formula")
            else:
                requested_set = set(requested)
                if set(formulas) != requested_set:
                    errors.append("formulas must cover exactly every requested cell")
                if any(
                    not isinstance(value, str) or not value.startswith("=")
                    for value in formulas.values()
                ):
                    errors.append("every effective formula must be a non-empty formula string")
        return errors

    @staticmethod
    def _candidate_diff(base: Path, candidate: Path, *, limit: int | None = 200_000) -> str:
        chunks: list[str] = []
        paths = sorted(
            {path.relative_to(base) for path in base.rglob("*") if path.is_file()}
            | {path.relative_to(candidate) for path in candidate.rglob("*") if path.is_file()}
        )
        for relative in paths:
            try:
                before = (base / relative).read_text(encoding="utf-8").splitlines()
                after = (candidate / relative).read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            if before == after:
                continue
            chunks.extend(difflib.unified_diff(before, after, f"a/{relative}", f"b/{relative}", lineterm=""))
        result = "\n".join(chunks)
        return result if limit is None else result[:limit]

    def _run_tests(self, task_id: str, revision_id: str) -> None:
        task = self._require_task(task_id)
        revision = self._require_revision(task, revision_id)
        candidate = self._candidate_path(task_id, revision_id)
        results: list[dict[str, Any]] = []
        test_scope = list(revision.get("test_scope") or self._test_scope(task, revision))
        try:
            for index, case in enumerate(test_scope, start=1):
                self._check_cancelled(task_id)
                self._emit(
                    task_id,
                    phase="testing",
                    kind="case",
                    status="started",
                    label=f"Testing Case {index}/{len(test_scope)}",
                    case_id=str(case.get("case_id") or ""),
                )
                progress = self.store.revision_root(task_id, revision_id) / f"test-{index}.jsonl"
                progress.unlink(missing_ok=True)
                state = self.store.revision_root(task_id, revision_id) / f"state-{index}.json"
                token = f"evolve-{task_id}-{revision_id}-{index}"
                _json_write(state, {"job_id": token, "resume_token": token, "resume_count": 0})
                command = [
                    sys.executable, "-m", "nanobot", "benchmark", "run",
                    "--profile", task["source_profile"],
                    "--benchmark", str(case["benchmark"]),
                    "--skill", DERIVED_SKILL,
                    "--model-preset", str(case["model_preset"]),
                    "--ocb-sample", "211",
                    "--resume-state", str(state),
                    "--resume-token", token,
                    "--rerun-benchmark", str(case["benchmark"]),
                    "--rerun-skill", DERIVED_SKILL,
                    "--rerun-model-preset", str(case["model_preset"]),
                    "--rerun-case", str(case["case_id"]),
                    "--skill-override-dir", str(candidate),
                    "--run-name-suffix", f"{task_id}-{revision_id}-{index}",
                ]
                env = os.environ.copy()
                env["NANOBOT_EVALUATION_PROGRESS_LOG"] = str(progress)
                stdout_path = self.store.revision_root(task_id, revision_id) / f"test-{index}.out"
                stderr_path = self.store.revision_root(task_id, revision_id) / f"test-{index}.err"
                with stdout_path.open("w+", encoding="utf-8") as stdout_file, stderr_path.open(
                    "w+", encoding="utf-8"
                ) as stderr_file:
                    process = subprocess.Popen(
                        command,
                        cwd=ROOT,
                        env=env,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        text=True,
                    )
                    while process.poll() is None:
                        if self._is_cancelled(task_id):
                            process.terminate()
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait()
                            raise asyncio.CancelledError()
                        time.sleep(0.25)
                    stdout_file.seek(0)
                    stderr_file.seek(0)
                    stdout = stdout_file.read()
                    stderr = stderr_file.read()
                completed = subprocess.CompletedProcess(
                    command,
                    int(process.returncode or 0),
                    stdout,
                    stderr,
                )
                scores: dict[str, Any] = {}
                trace_url = None
                if progress.is_file():
                    for line in progress.read_text(encoding="utf-8").splitlines():
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if event.get("event") == "case_reconciled" and event.get("scores"):
                            scores = dict(event["scores"])
                            trace_url = event.get("trace_url")
                if trace_url and _numeric_score({"scores": scores}) is None:
                    scores = self._wait_for_judge_scores(trace_url, scores)
                evolved_score = _numeric_score({"scores": scores})
                baseline_score = case.get("baseline_score")
                test_payload = self._test_case_payload(task, token, case)
                trace_evidence = self._trace_evidence(trace_url) if trace_url else {}
                observations = self._deterministic_observations({"trace": trace_evidence})
                candidate_output = str(test_payload.get("content") or trace_evidence.get("output") or "")
                intervention_feedback = self._intervention_feedback(
                    revision,
                    case,
                    trace_evidence,
                    candidate_output,
                )
                evolved_usage = None
                evolved_metrics = None
                if progress.is_file():
                    for line in progress.read_text(encoding="utf-8").splitlines():
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if event.get("event") == "case_reconciled":
                            evolved_usage = event.get("usage") or evolved_usage
                            evolved_metrics = event.get("metrics") or evolved_metrics
                results.append({
                    **case,
                    "baseline_score": baseline_score,
                    "evolved_score": evolved_score,
                    "delta": (
                        evolved_score - float(baseline_score)
                        if evolved_score is not None and baseline_score is not None else None
                    ),
                    "status": "completed" if completed.returncode == 0 else "failed",
                    "trace_url": trace_url,
                    "baseline_usage": case.get("baseline_usage"),
                    "evolved_usage": evolved_usage,
                    "baseline_metrics": case.get("baseline_metrics"),
                    "evolved_metrics": evolved_metrics,
                    "candidate_output": str(_redact(candidate_output))[:20_000],
                    "judge_reasoning": [
                        str(_redact(value))[:2000]
                        for value in trace_evidence.get("score_comments") or []
                    ][:20],
                    "tool_sequence": observations.get("tool_sequence") or [],
                    "tool_errors": observations.get("tool_errors") or [],
                    "stop_reason": test_payload.get("stop_reason") or observations.get("stop_reason"),
                    "intervention_feedback": intervention_feedback,
                    "error": completed.stderr[-1000:] if completed.returncode else None,
                })
                revision["test_results"] = results
                self.store.write(task)
                self._emit(
                    task_id,
                    phase="testing",
                    kind="case",
                    status="completed" if completed.returncode == 0 else "failed",
                    label="Case regression completed",
                    case_id=str(case.get("case_id") or ""),
                    trace_url=trace_url,
                )
            revision["recommendation"] = self._test_recommendation(results)
            all_scored = revision["recommendation"]["all_target_cases_scored"]
            revision["status"] = "tested" if all_scored else "test_failed"
            task["status"] = "tested" if revision["status"] == "tested" else "test_failed"
            task["cancel_requested"] = False
            self._emit(
                task_id,
                phase="testing",
                kind="stage",
                status="completed" if revision["status"] == "tested" else "failed",
                label="Regression testing completed",
            )
        except asyncio.CancelledError:
            revision["status"] = "test_failed"
            revision["test_error"] = "Cancelled by user"
            self.store.write(task)
            self._finish_cancelled(task_id)
            return
        except Exception as exc:
            revision["status"] = "test_failed"
            revision["test_error"] = str(exc)[:1000]
            task["status"] = "test_failed"
            self._emit(
                task_id,
                phase="testing",
                kind="error",
                status="failed",
                label="Regression testing failed",
                detail=str(exc),
            )
        self.store.write(task)

    def _test_scope(
        self,
        task: dict[str, Any],
        revision: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        revision = revision or {}
        target_case_ids = set(self._target_case_ids(task, revision))
        if not target_case_ids:
            raise ValueError("revision has no Finding-linked target Cases")
        selected = [
            {**row, "scope": "target"}
            for row in task.get("selected_cases") or []
            if str(row.get("case_id") or "") in target_case_ids
        ]
        category_ids_by_case = self._category_ids_by_case(task, revision)
        for row in selected:
            row["category_ids"] = category_ids_by_case.get(str(row.get("case_id") or ""), [])
        try:
            _run, cases = self._run_and_cases(str(task["source_run_id"]))
        except Exception:
            return selected
        for row in selected:
            matching = next(
                (case for case in cases if _case_key(case) == row.get("case_key")),
                None,
            )
            if matching:
                row["baseline_usage"] = matching.get("usage")
                row["baseline_metrics"] = matching.get("metrics")
        if len(selected) != len(target_case_ids):
            missing = sorted(target_case_ids - {str(row.get("case_id") or "") for row in selected})
            raise ValueError("target Cases are no longer available: " + ", ".join(missing))
        return selected

    def _load_task_evidence(
        self,
        task_id: str,
        analysis: dict[str, Any],
    ) -> dict[str, Any]:
        digest = str(analysis.get("evidence_digest") or "")[:16]
        path = next(self.store.task_root(task_id).glob(f"evidence-{digest}*.json"), None)
        if path is None:
            raise ValueError("frozen Evidence is unavailable")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"frozen Evidence is unreadable: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("frozen Evidence must be an object")
        return self._restore_frozen_reference_assets(task_id, analysis, payload)

    def _restore_frozen_reference_assets(
        self,
        task_id: str,
        analysis: dict[str, Any],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        digest = str(analysis.get("evidence_digest") or "")[:16]
        sidecar_path = self.store.task_root(task_id) / f"frozen-assets-{digest}.json"
        if not sidecar_path.is_file():
            return evidence
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"frozen asset sidecar is unreadable: {exc}") from exc
        if not isinstance(sidecar, dict) or not isinstance(sidecar.get("cases"), dict):
            raise ValueError("frozen asset sidecar must contain a cases object")
        if str(sidecar.get("evidence_digest") or "") != str(analysis.get("evidence_digest") or ""):
            raise ValueError("frozen asset sidecar does not match the active Evidence digest")
        for case in sidecar["cases"].values():
            if not isinstance(case, dict):
                continue
            evidence_id = str(case.get("evidence_id") or "")
            row = evidence.get(evidence_id)
            if not isinstance(row, dict):
                continue
            paths = [Path(str(value)).expanduser().resolve() for value in case.get("reference_paths") or []]
            hashes = [str(value) for value in case.get("reference_sha256") or []]
            if len(paths) != len(hashes):
                raise ValueError(f"frozen assets for {evidence_id} have mismatched paths and hashes")
            for path, expected_hash in zip(paths, hashes, strict=True):
                if not path.is_file() or _file_digest(path) != expected_hash:
                    raise ValueError(f"frozen asset failed integrity validation: {path.name}")
            if paths:
                row["reference_paths"] = [str(path) for path in paths]
                row["reference_sha256"] = hashes
        return evidence

    def _ensure_intervention_baseline(self, task_id: str, revision_id: str) -> Path:
        baseline = (
            self.store.revision_root(task_id, revision_id)
            / "intervention-baseline"
            / DERIVED_SKILL
        )
        if baseline.is_dir():
            return baseline
        baseline.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            ROOT / "nanobot" / "skills" / BASE_SKILL,
            baseline,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        self._normalize_derived_metadata(baseline)
        return baseline

    @staticmethod
    def _revision_categories(
        analysis: dict[str, Any],
        revision: dict[str, Any],
    ) -> list[dict[str, Any]]:
        category_ids = {str(value) for value in revision.get("category_ids") or []}
        return [
            row
            for row in analysis.get("categories") or []
            if str(row.get("category_id") or "") in category_ids
        ]

    def _target_case_ids(
        self,
        task: dict[str, Any],
        revision: dict[str, Any],
    ) -> list[str]:
        stored = {str(value) for value in revision.get("target_case_ids") or [] if str(value)}
        if stored:
            return sorted(stored)
        analysis_id = str(revision.get("analysis_id") or task.get("active_analysis_id") or "")
        analysis = self._require_analysis(task, analysis_id)
        categories = self._revision_categories(analysis, revision)
        if categories:
            return sorted({
                str(case_id)
                for category in categories
                for case_id in category.get("case_ids") or []
            })
        finding_ids = {str(value) for value in revision.get("finding_ids") or []}
        return sorted({
            str(case_id)
            for finding in analysis.get("findings") or []
            if str(finding.get("finding_id") or "") in finding_ids
            for case_id in finding.get("case_ids") or []
        })

    def _category_ids_by_case(
        self,
        task: dict[str, Any],
        revision: dict[str, Any],
    ) -> dict[str, list[str]]:
        analysis_id = str(revision.get("analysis_id") or task.get("active_analysis_id") or "")
        analysis = self._require_analysis(task, analysis_id)
        result: dict[str, list[str]] = {}
        for category in self._revision_categories(analysis, revision):
            category_id = str(category.get("category_id") or "")
            for case_id in category.get("case_ids") or []:
                result.setdefault(str(case_id), []).append(category_id)
        return {key: sorted(set(value)) for key, value in result.items()}

    @staticmethod
    def _category_test_summaries(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[float]] = {}
        for row in results:
            delta = row.get("delta")
            if delta is None:
                continue
            for category_id in row.get("category_ids") or []:
                grouped.setdefault(str(category_id), []).append(float(delta))
        return [
            {
                "category_id": category_id,
                "case_count": len(values),
                "mean_delta": round(sum(values) / len(values), 6),
                "improved_cases": sum(1 for value in values if value > 0),
                "unchanged_cases": sum(1 for value in values if value == 0),
                "regressed_cases": sum(1 for value in values if value < 0),
            }
            for category_id, values in sorted(grouped.items())
        ]

    def _test_recommendation(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        deltas = [float(row["delta"]) for row in results if row.get("delta") is not None]
        all_scored = bool(results) and all(
            row.get("status") == "completed" and row.get("delta") is not None
            for row in results
        )
        cost_changes = []
        for row in results:
            baseline_tokens = self._metric_value(
                row.get("baseline_usage"), ("total_tokens", "tokens", "total")
            )
            evolved_tokens = self._metric_value(
                row.get("evolved_usage"), ("total_tokens", "tokens", "total")
            )
            if baseline_tokens is not None and evolved_tokens is not None:
                cost_changes.append(evolved_tokens - baseline_tokens)
        mean_delta = round(sum(deltas) / len(deltas), 6) if deltas else None
        return {
            "recommended": bool(all_scored and mean_delta is not None and mean_delta > 0),
            "all_target_cases_scored": all_scored,
            "mean_delta": mean_delta,
            "improved_cases": sum(1 for value in deltas if value > 0),
            "unchanged_cases": sum(1 for value in deltas if value == 0),
            "regressed_cases": sum(1 for value in deltas if value < 0),
            "category_summaries": self._category_test_summaries(results),
            "mean_token_change": (
                round(sum(cost_changes) / len(cost_changes), 2) if cost_changes else None
            ),
            "disclaimer": (
                "Only Finding-linked target Cases were rerun once against historical baseline "
                "scores; this is directional evidence, not paired A/B or a no-regression guarantee."
            ),
        }

    @staticmethod
    def _test_case_payload(
        task: dict[str, Any],
        token: str,
        case: dict[str, Any],
    ) -> dict[str, Any]:
        root = (
            benchmark_cache_root()
            / "runs"
            / str(task.get("source_profile") or "")
            / "jobs"
            / token
            / "case-results"
            / str(case.get("benchmark") or "")
            / DERIVED_SKILL
            / str(case.get("model_preset") or "")
        )
        for path in root.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if str(payload.get("case_id") or "") == str(case.get("case_id") or ""):
                return payload
        return {}

    def _intervention_feedback(
        self,
        revision: dict[str, Any],
        case: dict[str, Any],
        trace: dict[str, Any],
        candidate_output: str,
    ) -> list[dict[str, Any]]:
        category_ids = {str(value) for value in case.get("category_ids") or []}
        contracts = {
            str(category_id): intervention
            for category_id, intervention in zip(
                revision.get("category_ids") or [],
                revision.get("interventions") or [],
                strict=False,
            )
        }
        trace_text = json.dumps(trace, ensure_ascii=False).lower()
        output_text = candidate_output.lower()
        rows = []
        for category_id in sorted(category_ids):
            contract = contracts.get(category_id) or {}
            entrypoint = str(contract.get("entrypoint") or "")
            required_outputs = [str(value) for value in contract.get("required_outputs") or []]
            rows.append({
                "category_id": category_id,
                "repair_mode": contract.get("repair_mode"),
                "entrypoint": entrypoint,
                "entrypoint_observed": bool(entrypoint and entrypoint.lower() in trace_text),
                "required_outputs": required_outputs,
                "required_outputs_observed": [
                    value
                    for value in required_outputs
                    if value.lower() in trace_text or value.lower() in output_text
                ],
                "final_answer_check": contract.get("final_answer_check") or [],
            })
        return rows

    @staticmethod
    def _revision_feedback_rows(revision: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for result in revision.get("test_results") or []:
            if result.get("status") == "completed" and (result.get("delta") or 0) > 0:
                continue
            case_id = str(result.get("case_id") or "")
            rows.append({
                "feedback_id": f"test-{revision.get('revision_id')}-{case_id}",
                "case_id": case_id,
                "baseline_score": result.get("baseline_score"),
                "candidate_score": result.get("evolved_score"),
                "delta": result.get("delta"),
                "candidate_output": result.get("candidate_output"),
                "judge_reasoning": result.get("judge_reasoning") or [],
                "tool_sequence": result.get("tool_sequence") or [],
                "tool_errors": result.get("tool_errors") or [],
                "stop_reason": result.get("stop_reason"),
                "intervention_feedback": result.get("intervention_feedback") or [],
                "trace_url": result.get("trace_url"),
            })
        return rows

    def _wait_for_judge_scores(
        self,
        trace_url: str,
        initial: dict[str, Any],
    ) -> dict[str, Any]:
        scores = dict(initial)
        for attempt in range(20):
            trace = self._trace_evidence(trace_url)
            raw_scores = trace.get("scores") if isinstance(trace, dict) else None
            if isinstance(raw_scores, list):
                for score in raw_scores:
                    if not isinstance(score, dict):
                        continue
                    name = str(score.get("name") or "")
                    value = score.get("value")
                    if name and isinstance(value, (int, float, bool)):
                        scores["mybot_score" if name == "mybot-ocb-judge-v1" else name] = value
            if _numeric_score({"scores": scores}) is not None:
                return scores
            if attempt < 19:
                time.sleep(1)
        return scores

    @staticmethod
    def _case_summary(case: dict[str, Any]) -> dict[str, Any]:
        return {
            "case_key": _case_key(case),
            "case_id": str(case.get("case_id") or ""),
            "benchmark": str(case.get("benchmark") or ""),
            "skill": str(case.get("skill") or ""),
            "model_preset": str(case.get("model_preset") or ""),
            "baseline_score": _numeric_score(case),
            "trace_url": case.get("trace_url"),
        }

    def _require_task(self, task_id: str) -> dict[str, Any]:
        task = self.store.read(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    @staticmethod
    def _require_revision(task: dict[str, Any], revision_id: str) -> dict[str, Any]:
        revision = next(
            (row for row in task.get("revisions") or [] if row.get("revision_id") == revision_id),
            None,
        )
        if revision is None:
            raise KeyError(revision_id)
        return revision

    @staticmethod
    def _require_analysis(task: dict[str, Any], analysis_id: str) -> dict[str, Any]:
        analysis = next(
            (row for row in task.get("analyses") or [] if row.get("analysis_id") == analysis_id),
            None,
        )
        if analysis is None:
            raise KeyError(analysis_id)
        return analysis

    def _candidate_path(self, task_id: str, revision_id: str) -> Path:
        path = self.store.revision_root(task_id, revision_id) / "candidate" / DERIVED_SKILL
        if not path.is_dir():
            raise ValueError("candidate Skill is unavailable")
        return path
