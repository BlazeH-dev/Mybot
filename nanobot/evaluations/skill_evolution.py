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
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import json_repair
import yaml

from nanobot.config.loader import load_config, resolve_config_env_vars, save_config
from nanobot.evaluations.catalog import ROOT, benchmark_cache_root
from nanobot.providers.factory import make_provider

BASE_SKILL = "officecli"
DERIVED_SKILL = "officecli-evolved"
OPTIMIZER_PRESET = "gpt-5-6-sol"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_ALLOWED_FILE = re.compile(r"^(SKILL\.md|(?:scripts|references|assets)/[^/].*)$")
_REDACT_KEYS = re.compile(r"(api.?key|authorization|secret|token|password|cookie)", re.I)


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
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _case_key(case: dict[str, Any]) -> str:
    return "\0".join(
        str(case.get(name) or "")
        for name in ("benchmark", "skill", "model_preset", "case_id")
    )


def _redact(value: Any, *, key: str = "") -> Any:
    if _REDACT_KEYS.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+", "Bearer [REDACTED]", value)
        value = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
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

    def read(self, task_id: str) -> dict[str, Any] | None:
        try:
            return json.loads(self.task_path(task_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def write(self, task: dict[str, Any]) -> dict[str, Any]:
        task = dict(task)
        task["updated_at"] = _now()
        with self._lock:
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

    def generate(self, run_id: str, selected_keys: list[str], threshold: float = 0.6) -> dict[str, Any]:
        if not selected_keys:
            raise ValueError("select at least one bad Case")
        if len(selected_keys) > 20:
            raise ValueError("select at most 20 bad Cases per evolution round")
        run, cases = self._run_and_cases(run_id)
        selected_set = set(selected_keys)
        selected = [case for case in cases if _case_key(case) in selected_set]
        if len(selected) != len(selected_set):
            raise ValueError("one or more selected Cases are not in the evaluation run")
        for case in selected:
            score = _numeric_score(case)
            if score is None or score >= threshold:
                raise ValueError("selected Cases must have a score below the threshold")

        task_id = uuid.uuid4().hex[:20]
        revision_id = "r1"
        task = {
            "schema_version": 1,
            "task_id": task_id,
            "title": "OfficeCLI evaluation-driven evolution",
            "source_run_id": run_id,
            "source_profile": str(run.get("profile") or ""),
            "base_skill": BASE_SKILL,
            "derived_skill": DERIVED_SKILL,
            "optimizer_model": OPTIMIZER_PRESET,
            "threshold": threshold,
            "status": "generating",
            "selected_cases": [self._case_summary(case) for case in selected],
            "revisions": [],
            "active_revision_id": revision_id,
            "created_at": _now(),
        }
        self.store.write(task)
        try:
            revision = asyncio.run(self._generate_revision(task, revision_id, selected))
            task["revisions"] = [revision]
            task["status"] = "ready_for_review"
            task["error"] = None
        except Exception as exc:
            task["status"] = "failed"
            task["error"] = str(exc)[:1000]
        return self.store.write(task)

    def revise(self, task_id: str) -> dict[str, Any]:
        task = self._require_task(task_id)
        parent = self._require_revision(task, str(task.get("active_revision_id") or ""))
        if parent.get("status") not in {"ready_for_review", "tested", "test_failed"}:
            raise ValueError("active revision is not ready for another improvement round")
        _run, cases = self._run_and_cases(str(task["source_run_id"]))
        selected_keys = {str(row.get("case_key") or "") for row in task["selected_cases"]}
        selected = [case for case in cases if _case_key(case) in selected_keys]
        if len(selected) != len(selected_keys):
            raise ValueError("selected baseline Cases are no longer available")
        revision_id = f"r{len(task.get('revisions') or []) + 1}"
        task["status"] = "generating"
        self.store.write(task)
        try:
            revision = asyncio.run(
                self._generate_revision(
                    task,
                    revision_id,
                    selected,
                    parent_revision_id=str(parent["revision_id"]),
                    source_candidate=self._candidate_path(task_id, str(parent["revision_id"])),
                )
            )
            task.setdefault("revisions", []).append(revision)
            task["active_revision_id"] = revision_id
            task["status"] = "ready_for_review"
            task["error"] = None
        except Exception as exc:
            task["status"] = "failed"
            task["error"] = str(exc)[:1000]
        return self.store.write(task)

    def start_test(self, task_id: str, revision_id: str) -> dict[str, Any]:
        task = self._require_task(task_id)
        revision = self._require_revision(task, revision_id)
        if revision.get("status") not in {"ready_for_review", "tested", "test_failed"}:
            raise ValueError("revision is not ready to test")
        if task_id in self._threads and self._threads[task_id].is_alive():
            raise ValueError("this Skill evolution task is already running")
        revision["status"] = "testing"
        revision["test_results"] = []
        task["status"] = "testing"
        self.store.write(task)
        thread = threading.Thread(
            target=self._run_tests,
            args=(task_id, revision_id),
            daemon=True,
            name=f"skill-evolution-{task_id}",
        )
        self._threads[task_id] = thread
        thread.start()
        return task

    def apply(self, task_id: str, revision_id: str) -> dict[str, Any]:
        task = self._require_task(task_id)
        revision = self._require_revision(task, revision_id)
        if revision.get("status") not in {"ready_for_review", "tested", "test_failed"}:
            raise ValueError("revision is not reviewable")
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

    async def _generate_revision(
        self,
        task: dict[str, Any],
        revision_id: str,
        selected: list[dict[str, Any]],
        parent_revision_id: str | None = None,
        source_candidate: Path | None = None,
    ) -> dict[str, Any]:
        base = ROOT / "nanobot" / "skills" / BASE_SKILL
        source_root = source_candidate or base
        revision_root = self.store.revision_root(task["task_id"], revision_id)
        candidate = revision_root / "candidate" / DERIVED_SKILL
        candidate.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            source_root,
            candidate,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        evidence = [self._build_evidence(task, case) for case in selected]
        response = await self._call_optimizer(source_root, evidence)
        changes = response.get("files")
        if not isinstance(changes, list) or not changes:
            raise ValueError("optimizer returned no Skill file changes")
        changed_paths: list[str] = []
        for item in changes:
            if not isinstance(item, dict):
                raise ValueError("optimizer files must be objects")
            relative = str(item.get("path") or "")
            content = item.get("content")
            if not _ALLOWED_FILE.fullmatch(relative) or ".." in Path(relative).parts:
                raise ValueError(f"optimizer returned disallowed path: {relative}")
            if not isinstance(content, str) or len(content.encode()) > 200_000:
                raise ValueError(f"optimizer returned invalid content for {relative}")
            target = (candidate / relative).resolve()
            target.relative_to(candidate.resolve())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            changed_paths.append(relative)

        self._normalize_derived_metadata(candidate)
        checks = self._validate_candidate(base, candidate)
        if not checks["valid"]:
            raise ValueError("candidate validation failed: " + "; ".join(checks["errors"]))
        digest = _tree_digest(candidate)
        diff = self._candidate_diff(source_root, candidate)
        (revision_root / "evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return {
            "revision_id": revision_id,
            "parent_revision_id": parent_revision_id,
            "status": "ready_for_review",
            "summary": str(response.get("summary") or "Improved from selected bad Cases"),
            "rationale": str(response.get("rationale") or ""),
            "changed_paths": sorted(set(changed_paths)),
            "candidate_digest": digest,
            "diff": diff,
            "validation": checks,
            "created_at": _now(),
            "test_results": [],
        }

    async def _call_optimizer(self, base: Path, evidence: list[dict[str, Any]]) -> dict[str, Any]:
        config = resolve_config_env_vars(load_config())
        preset = config.resolve_preset(OPTIMIZER_PRESET)
        provider = make_provider(config, preset_name=OPTIMIZER_PRESET)
        source_files: dict[str, str] = {}
        for path in sorted(item for item in base.rglob("*") if item.is_file()):
            if path.stat().st_size > 80_000 or "__pycache__" in path.parts:
                continue
            try:
                source_files[path.relative_to(base).as_posix()] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
        prompt = {
            "task": "Improve OfficeCLI Skill using only the supplied bad-case evidence.",
            "derived_skill_name": DERIVED_SKILL,
            "constraints": [
                "Return JSON only with summary, rationale, and files.",
                "files may contain SKILL.md or files under scripts/, references/, assets/.",
                "Do not return skill.yaml or change provider, permissions, evaluator, benchmark, or runtime policy.",
                "Preserve generally useful behavior; encode transferable lessons, not case IDs or gold answers.",
                "Keep SKILL.md concise and use references or deterministic scripts for detailed reusable guidance.",
            ],
            "current_files": source_files,
            "bad_cases": evidence,
        }
        response = await provider.chat_with_retry(
            messages=[
                {"role": "system", "content": "You are a Skill optimizer. Output one strict JSON object."},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            model=preset.model,
            max_tokens=preset.max_tokens,
            temperature=0.1,
            reasoning_effort=preset.reasoning_effort,
        )
        if response.finish_reason == "error":
            raise RuntimeError(response.content or "optimizer model failed")
        parsed = json_repair.loads(response.content or "")
        if not isinstance(parsed, dict):
            raise ValueError("optimizer response is not a JSON object")
        return parsed

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

    @staticmethod
    def _validate_candidate(base: Path, candidate: Path) -> dict[str, Any]:
        errors: list[str] = []
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
            if path.is_symlink():
                errors.append(f"symbolic links are not allowed: {path.relative_to(candidate)}")
            if path.is_file() and path.suffix == ".py":
                try:
                    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                except (OSError, SyntaxError, UnicodeError) as exc:
                    errors.append(str(exc))
        return {"valid": not errors, "errors": errors}

    @staticmethod
    def _candidate_diff(base: Path, candidate: Path) -> str:
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
        return "\n".join(chunks)[:200_000]

    def _run_tests(self, task_id: str, revision_id: str) -> None:
        task = self._require_task(task_id)
        revision = self._require_revision(task, revision_id)
        candidate = self._candidate_path(task_id, revision_id)
        results: list[dict[str, Any]] = []
        try:
            for index, case in enumerate(task["selected_cases"], start=1):
                progress = self.store.revision_root(task_id, revision_id) / f"test-{index}.jsonl"
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
                completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
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
                    "error": completed.stderr[-1000:] if completed.returncode else None,
                })
                revision["test_results"] = results
                self.store.write(task)
            deltas = [row["delta"] for row in results if row.get("delta") is not None]
            revision["recommendation"] = {
                "recommended": bool(deltas) and min(deltas) >= 0 and max(deltas) > 0,
                "no_selected_regressions": bool(deltas) and min(deltas) >= 0,
                "at_least_one_improvement": bool(deltas) and max(deltas) > 0,
                "disclaimer": "Only selected bad Cases were rerun; this is not a full no-regression guarantee.",
            }
            revision["status"] = "tested" if all(row["status"] == "completed" for row in results) else "test_failed"
            task["status"] = "tested" if revision["status"] == "tested" else "test_failed"
        except Exception as exc:
            revision["status"] = "test_failed"
            revision["test_error"] = str(exc)[:1000]
            task["status"] = "test_failed"
        self.store.write(task)

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

    def _candidate_path(self, task_id: str, revision_id: str) -> Path:
        path = self.store.revision_root(task_id, revision_id) / "candidate" / DERIVED_SKILL
        if not path.is_dir():
            raise ValueError("candidate Skill is unavailable")
        return path
