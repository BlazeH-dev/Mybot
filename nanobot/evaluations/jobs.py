"""Durable, single-worker evaluation queue used by CLI-backed WebUI jobs."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from nanobot.evaluations.catalog import (
    ROOT,
    TERMINAL_JOB_STATUSES,
    EvaluationCatalog,
    EvaluationRequest,
    benchmark_cache_root,
)

RESUMABLE_JOB_STATUSES = frozenset({"failed", "cancelled", "interrupted"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class EvaluationJobStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or benchmark_cache_root() / "jobs").expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def path(self, job_id: str) -> Path:
        if not job_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in job_id):
            raise ValueError("invalid evaluation job id")
        return self.root / f"{job_id}.json"

    def read(self, job_id: str) -> dict[str, Any] | None:
        path = self.path(job_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def write(self, job: dict[str, Any]) -> dict[str, Any]:
        job = dict(job)
        job["updated_at"] = _now()
        path = self.path(str(job["job_id"]))
        temp = path.with_suffix(".tmp")
        with self._lock:
            temp.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temp, path)
        return job

    def update(self, job_id: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            job = self.read(job_id)
            if job is None:
                raise KeyError(job_id)
            job.update(fields)
            return self.write(job)

    def list(self) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for path in self.root.glob("*.json"):
            if path.name.endswith(".progress.json"):
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and payload.get("job_id"):
                jobs.append(payload)
        return sorted(jobs, key=lambda row: str(row.get("created_at") or ""), reverse=True)


class EvaluationJobService:
    def __init__(
        self,
        catalog: EvaluationCatalog | None = None,
        store: EvaluationJobStore | None = None,
        on_update: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.catalog = catalog or EvaluationCatalog()
        self.store = store or EvaluationJobStore()
        self.on_update = on_update
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[Any]] = {}
        self._reconcile()

    def _notify(self, job: dict[str, Any]) -> None:
        if self.on_update is not None:
            self.on_update(dict(job))

    def _reconcile(self) -> None:
        active_found = False
        for job in reversed(self.store.list()):
            status = str(job.get("status") or "")
            if status not in {"preflight", "preparing", "estimating", "running", "remote_scoring"}:
                continue
            pid = job.get("worker_pid")
            if isinstance(pid, int) and _pid_alive(pid) and not active_found:
                active_found = True
                threading.Thread(target=self._watch_existing, args=(job["job_id"], pid), daemon=True).start()
                continue
            self.store.update(
                job["job_id"],
                status="interrupted",
                phase="interrupted",
                finished_at=_now(),
                error="evaluation worker is no longer running",
                resumable=job.get("action") == "run" and job.get("profile") != "ci",
            )
        if not active_found:
            self._start_next()

    def _watch_existing(self, job_id: str, pid: int) -> None:
        last_updated_at: str | None = None
        while _pid_alive(pid):
            job = self.store.read(job_id)
            updated_at = str(job.get("updated_at")) if job else None
            if job and updated_at != last_updated_at:
                last_updated_at = updated_at
                self._notify(job)
            time.sleep(0.5)
        job = self.store.read(job_id)
        if job and job.get("status") not in TERMINAL_JOB_STATUSES:
            status = "cancelled" if job.get("cancel_requested") else "interrupted"
            self._notify(self.store.update(
                job_id,
                status=status,
                phase=status,
                finished_at=_now(),
                resumable=job.get("action") == "run" and job.get("profile") != "ci",
            ))
        self._start_next()

    def submit(self, request: EvaluationRequest) -> dict[str, Any]:
        preflight = self.catalog.preflight(request)
        if not preflight.ready:
            raise ValueError("; ".join(preflight.blockers))
        job_id = uuid.uuid4().hex[:20]
        job = {
            "schema_version": 2,
            "job_id": job_id,
            "suite_id": request.suite_id,
            "profile": request.profile,
            "action": request.action,
            "status": "queued",
            "phase": "queued",
            "request": request.payload(),
            "estimated_tokens": preflight.estimate.get("estimated_tokens", {}),
            "total_cases": int(preflight.estimate.get("skill_runs", 0)),
            "completed_cases": 0,
            "remaining_cases": int(preflight.estimate.get("skill_runs", 0)),
            "resumed_cases": 0,
            "current_case": None,
            "current_variant": None,
            "cases": [],
            "dataset_run_ids": [],
            "langfuse_links": [],
            "aggregate_scores": {},
            "review_status": "not_started",
            "cancel_requested": False,
            "worker_pid": None,
            "progress_offset": 0,
            "resume_token": job_id,
            "resume_count": 0,
            "resume_history": [],
            "resumable": request.action == "run" and request.profile != "ci",
            "error": None,
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
        }
        job = self.store.write(job)
        self._notify(job)
        self._start_next()
        return self.store.read(job_id) or job

    def retry(self, job_id: str) -> dict[str, Any]:
        original = self.store.read(job_id)
        if original is None:
            raise KeyError(job_id)
        request = EvaluationRequest.from_payload(dict(original.get("request") or {}))
        return self.submit(request)

    def resume(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.store.read(job_id)
            if job is None:
                raise KeyError(job_id)
            status = str(job.get("status") or "")
            if status not in RESUMABLE_JOB_STATUSES:
                raise ValueError(f"evaluation job in status {status!r} cannot be resumed")
            if job.get("action") != "run" or job.get("profile") == "ci":
                raise ValueError("only interrupted model evaluation jobs can resume by case")
            request = EvaluationRequest.from_payload(dict(job.get("request") or {}))
            preflight = self.catalog.preflight(request)
            if not preflight.ready:
                raise ValueError("; ".join(preflight.blockers))
            resumed_at = _now()
            history = list(job.get("resume_history") or [])
            history.append({
                "attempt": int(job.get("resume_count") or 0) + 1,
                "from_status": status,
                "requested_at": resumed_at,
                "completed_cases": int(job.get("completed_cases") or 0),
            })
            updated = self.store.update(
                job_id,
                status="queued",
                phase="queued",
                cancel_requested=False,
                worker_pid=None,
                current_case=None,
                current_variant=None,
                error=None,
                finished_at=None,
                resume_count=int(job.get("resume_count") or 0) + 1,
                resume_history=history,
                resume_requested_at=resumed_at,
            )
            self._notify(updated)
            self._start_next()
            return self.store.read(job_id) or updated

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.store.read(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.get("status") in TERMINAL_JOB_STATUSES:
                return job
            if job.get("status") == "queued":
                updated = self.store.update(job_id, status="cancelled", phase="cancelled", cancel_requested=True, finished_at=_now())
                self._notify(updated)
                return updated
            updated = self.store.update(job_id, cancel_requested=True)
            pid = updated.get("worker_pid")
            if isinstance(pid, int) and _pid_alive(pid):
                try:
                    if os.name == "posix":
                        os.killpg(pid, signal.SIGTERM)
                    else:
                        os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            self._notify(updated)
            return updated

    def list(self) -> list[dict[str, Any]]:
        return self.store.list()

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self.store.read(job_id)

    def cases(self, job_id: str) -> list[dict[str, Any]]:
        job = self.store.read(job_id)
        if job is None:
            raise KeyError(job_id)
        raw = job.get("cases")
        return raw if isinstance(raw, list) else []

    def _active(self) -> bool:
        return any(
            str(job.get("status")) not in TERMINAL_JOB_STATUSES | {"queued"}
            for job in self.store.list()
        )

    def _start_next(self) -> None:
        with self._lock:
            if self._active():
                return
            queued = [job for job in reversed(self.store.list()) if job.get("status") == "queued"]
            if not queued:
                return
            job = queued[0]
            job_id = str(job["job_id"])
            command = [sys.executable, "-m", "nanobot.evaluations.worker", "--job", str(self.store.path(job_id))]
            log_path = self.store.root / f"{job_id}.log"
            log_handle = log_path.open("ab")
            try:
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=(os.name == "posix"),
                )
            finally:
                log_handle.close()
            self._processes[job_id] = process
            updated = self.store.update(
                job_id,
                status="preflight",
                phase="preflight",
                worker_pid=process.pid,
                started_at=job.get("started_at") or _now(),
                last_started_at=_now(),
                log_path=str(log_path),
            )
            self._notify(updated)
            threading.Thread(target=self._watch_child, args=(job_id, process), daemon=True).start()

    def _watch_child(self, job_id: str, process: subprocess.Popen[Any]) -> None:
        last_updated_at: str | None = None
        while process.poll() is None:
            job = self.store.read(job_id)
            updated_at = str(job.get("updated_at")) if job else None
            if job and updated_at != last_updated_at:
                last_updated_at = updated_at
                self._notify(job)
            time.sleep(0.5)
        with self._lock:
            self._processes.pop(job_id, None)
            job = self.store.read(job_id)
            if job and job.get("status") not in TERMINAL_JOB_STATUSES:
                if job.get("cancel_requested"):
                    job = self.store.update(
                        job_id,
                        status="cancelled",
                        phase="cancelled",
                        finished_at=_now(),
                        resumable=job.get("action") == "run" and job.get("profile") != "ci",
                    )
                else:
                    job = self.store.update(
                        job_id,
                        status="interrupted",
                        phase="interrupted",
                        finished_at=_now(),
                        error=f"worker exited with code {process.returncode}",
                        resumable=job.get("action") == "run" and job.get("profile") != "ci",
                    )
                self._notify(job)
        self._start_next()
