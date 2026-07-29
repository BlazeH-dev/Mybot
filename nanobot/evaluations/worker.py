"""Detached worker that executes one trusted benchmark command and persists progress."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nanobot.evaluations.catalog import EvaluationCatalog, EvaluationRequest


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, job: dict[str, Any]) -> None:
    try:
        persisted = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        persisted = {}
    if persisted.get("cancel_requested") is True:
        job["cancel_requested"] = True
    job["updated_at"] = _now()
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _case_key(case: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(case.get("benchmark") or ""),
        str(case.get("skill") or ""),
        str(case.get("case_id") or ""),
    )


def _upsert_case(job: dict[str, Any], event: dict[str, Any], **fields: Any) -> None:
    identity = {
        "case_id": event.get("case_id"),
        "benchmark": event.get("benchmark"),
        "skill": event.get("skill"),
    }
    key = _case_key(identity)
    cases = job.setdefault("cases", [])
    existing = next((case for case in cases if _case_key(case) == key), None)
    if existing is None:
        existing = identity
        cases.append(existing)
    existing.update(fields)


def _refresh_case_counts(job: dict[str, Any]) -> None:
    terminal = {"completed", "failed"}
    cases = job.get("cases", [])
    completed = sum(1 for case in cases if case.get("status") in terminal)
    resumed = sum(
        1
        for case in cases
        if case.get("status") in terminal and case.get("checkpoint_source") in {"local", "langfuse"}
    )
    total = int(job.get("total_cases") or 0)
    job["completed_cases"] = completed
    job["remaining_cases"] = max(0, total - completed)
    job["resumed_cases"] = resumed


def _consume_progress(path: Path, offset: int, job: dict[str, Any]) -> int:
    if not path.is_file():
        return offset
    with path.open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("event")
            if kind == "run_started":
                job["status"] = "running"
                job["phase"] = "running"
                job["total_cases"] = int(event.get("total_cases") or job.get("total_cases") or 0)
                job["pending_cases"] = int(event.get("pending_cases") or 0)
                job["checkpoint_cases"] = int(event.get("checkpoint_cases") or 0)
            elif kind == "variant_started":
                job["current_variant"] = event.get("variant") or "/".join(
                    filter(None, [str(event.get("benchmark") or ""), str(event.get("skill") or "")])
                )
            elif kind == "case_started":
                job["current_case"] = event.get("case_id")
                job["current_variant"] = event.get("variant")
                _upsert_case(
                    job,
                    event,
                    status="running",
                    score_status="pending",
                    attempt=int(job.get("resume_count") or 0) + 1,
                )
            elif kind == "case_completed":
                _upsert_case(
                    job,
                    event,
                    status=event.get("status", "completed"),
                    score_status="pending",
                    checkpoint_source=event.get("source"),
                )
            elif kind == "case_reconciled":
                _upsert_case(
                    job,
                    event,
                    status=event.get("status", "completed"),
                    score_status=event.get("score_status", "remote"),
                    checkpoint_source=event.get("source", "langfuse"),
                    trace_url=event.get("trace_url"),
                    langfuse_url=event.get("dataset_run_url"),
                )
            elif kind == "variant_completed":
                run_id = event.get("dataset_run_id")
                url = event.get("dataset_run_url")
                if run_id and run_id not in job.setdefault("dataset_run_ids", []):
                    job["dataset_run_ids"].append(run_id)
                if url and url not in job.setdefault("langfuse_links", []):
                    job["langfuse_links"].append(url)
                for case in reversed(job.get("cases", [])):
                    if case.get("benchmark") == event.get("benchmark") and case.get("skill") == event.get("skill") and not case.get("langfuse_url"):
                        case["langfuse_url"] = url
                job["current_case"] = None
            elif kind == "run_completed":
                job["phase"] = "remote_scoring"
                job["status"] = "remote_scoring"
                job["current_case"] = None
                job["current_variant"] = None
            _refresh_case_counts(job)
        return handle.tell()


def run(job_path: Path) -> int:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    request = EvaluationRequest.from_payload(dict(job.get("request") or {}))
    catalog = EvaluationCatalog()
    progress_path = job_path.with_suffix(".progress.jsonl")
    benchmark_command = catalog.command(request)
    if request.action == "run" and request.profile != "ci":
        benchmark_command.extend([
            "--resume-state",
            str(job_path),
            "--resume-token",
            str(job.get("resume_token") or job.get("job_id")),
        ])
    command = [sys.executable, "-m", "nanobot", *benchmark_command]
    env = os.environ.copy()
    env["NANOBOT_EVALUATION_PROGRESS_LOG"] = str(progress_path)
    job.update(
        status="preparing" if request.action == "prepare" else "running",
        phase="preparing" if request.action == "prepare" else "running",
        command_summary=" ".join(benchmark_command),
    )
    log_path = Path(str(job.get("log_path") or job_path.with_suffix(".log")))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    offset = int(job.get("progress_offset") or 0)
    offset = _consume_progress(progress_path, offset, job)
    job["progress_offset"] = offset
    _write(job_path, job)
    with log_path.open("ab") as log_handle:
        process = subprocess.Popen(
            command,
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        while process.poll() is None:
            offset = _consume_progress(progress_path, offset, job)
            job["progress_offset"] = offset
            _write(job_path, job)
            time.sleep(0.5)
    try:
        output = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        output = []
    offset = _consume_progress(progress_path, offset, job)
    job["progress_offset"] = offset
    if process.returncode == 0:
        if request.action == "prepare":
            job.update(status="completed", phase="completed", review_status="not_required")
        elif request.profile == "ci":
            job.update(status="completed", phase="completed", completed_cases=job.get("total_cases", 0), review_status="not_required")
        else:
            job.update(status="awaiting_review", phase="awaiting_review", review_status="pending")
        job["finished_at"] = _now()
        job["output_tail"] = output[-20:]
        _write(job_path, job)
        return 0
    job.update(
        status="failed",
        phase="failed",
        error=(output[-1] if output else f"benchmark exited with code {process.returncode}"),
        output_tail=output[-20:],
        finished_at=_now(),
        resumable=request.action == "run" and request.profile != "ci",
    )
    _write(job_path, job)
    return int(process.returncode or 1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True, type=Path)
    args = parser.parse_args()
    raise SystemExit(run(args.job.expanduser().resolve()))


if __name__ == "__main__":
    main()
